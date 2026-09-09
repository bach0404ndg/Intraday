from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.requests import StockQuotesRequest

from intraday import (
    StrategyConfig,
    add_real_trading_simulation,
    create_period_summary_table,
    create_trade_log,
    get_stock_data_client,
    print_period_summary_table,
    run_strategy,
)


TRADE_KEY_COLUMNS = ["date", "symbol", "buy_time", "buy_price", "sell_time", "sell_price"]
QUOTE_CHECK_REQUIRED_COLUMNS = {
    "buy_best_ask_in_window",
    "sell_best_bid_in_window",
    "buy_limit_execution_price",
    "sell_limit_execution_price",
    "buy_limit_visible_shares",
    "sell_limit_visible_shares",
    "buy_window_slippage_per_share",
    "sell_window_slippage_per_share",
    "estimated_slippage_amount",
}


@dataclass(frozen=True)
class QuoteCheckConfig:
    trade_log_path: Path = Path("trade_log.csv")
    output_path: Path = Path("quote_spread_check.csv")
    feed: DataFeed = DataFeed.IEX
    start_date: date | None = None
    end_date: date | None = None
    quote_window_minutes: int = 5
    max_quote_delay_seconds: int = 120
    fallback_notional: float = 10000.0
    quote_size_multiplier: int = 1
    reuse_existing_output: bool = True
    progress_interval: int = 25
    max_workers: int = 5
    summary_period_months: int = 1


def parse_date(value):
    if value is None:
        return None

    return pd.to_datetime(value).date()


def normalize_trade_times(data):
    data = data.copy()
    data["date"] = pd.to_datetime(data["date"]).dt.date
    data["buy_time"] = pd.to_datetime(
        data["buy_time"],
        errors="coerce",
        utc=True,
    )
    data["sell_time"] = pd.to_datetime(
        data["sell_time"],
        errors="coerce",
        utc=True,
    )
    return data


def add_trade_key(data):
    data = normalize_trade_times(data)
    data["_trade_key"] = data[TRADE_KEY_COLUMNS].astype(str).agg("|".join, axis=1)
    return data


def load_trade_log(path, start_date=None, end_date=None):
    trade_log = pd.read_csv(path)

    required_columns = {
        "date",
        "symbol",
        "buy_time",
        "buy_price",
        "sell_time",
        "sell_price",
    }
    missing_columns = required_columns - set(trade_log.columns)

    if missing_columns:
        raise ValueError(
            "Trade log is missing required columns: "
            + ", ".join(sorted(missing_columns))
        )

    trade_log = normalize_trade_times(trade_log)

    if start_date is not None:
        trade_log = trade_log[trade_log["date"] >= start_date]

    if end_date is not None:
        trade_log = trade_log[trade_log["date"] <= end_date]

    if "shares" not in trade_log.columns:
        trade_log["shares"] = np.nan

    trade_log = trade_log[
        trade_log["buy_time"].notna()
        & trade_log["buy_price"].notna()
        & trade_log["sell_time"].notna()
        & trade_log["sell_price"].notna()
    ]

    return add_trade_key(trade_log)


def load_existing_quote_checks(path):
    if not path.exists():
        return pd.DataFrame()

    existing = pd.read_csv(path)
    missing_columns = set(TRADE_KEY_COLUMNS) - set(existing.columns)

    if missing_columns:
        return pd.DataFrame()

    if not QUOTE_CHECK_REQUIRED_COLUMNS.issubset(existing.columns):
        return pd.DataFrame()

    return add_trade_key(existing)


def fetch_quotes_around_event(client, symbol, event_time, config):
    if pd.isna(event_time):
        return pd.DataFrame()

    start = event_time
    end = event_time + timedelta(minutes=config.quote_window_minutes)

    request = StockQuotesRequest(
        symbol_or_symbols=symbol,
        start=start.to_pydatetime(),
        end=end.to_pydatetime(),
        limit=10000,
        feed=config.feed,
    )

    quotes = client.get_stock_quotes(request).df

    if quotes.empty:
        return pd.DataFrame()

    quotes = quotes.reset_index()
    quotes["timestamp"] = pd.to_datetime(quotes["timestamp"], utc=True)
    return quotes.sort_values("timestamp")


def nearest_quote(quotes, event_time, config):
    empty_quote = {
        "quote_time": pd.NaT,
        "quote_delay_seconds": np.nan,
        "stale_quote": True,
        "bid": np.nan,
        "bid_size": np.nan,
        "ask": np.nan,
        "ask_size": np.nan,
        "mid": np.nan,
        "spread": np.nan,
        "spread_pct": np.nan,
    }

    if quotes.empty or pd.isna(event_time):
        return empty_quote

    before = quotes[quotes["timestamp"] <= event_time]

    if before.empty:
        quote = quotes.iloc[0]
    else:
        quote = before.iloc[-1]

    quote_time = quote["timestamp"]
    bid = quote["bid_price"]
    bid_size = quote["bid_size"]
    ask = quote["ask_price"]
    ask_size = quote["ask_size"]
    mid = (bid + ask) / 2
    spread = ask - bid
    delay_seconds = abs((event_time - quote_time).total_seconds())

    return {
        "quote_time": quote_time,
        "quote_delay_seconds": delay_seconds,
        "stale_quote": delay_seconds > config.max_quote_delay_seconds,
        "bid": bid,
        "bid_size": bid_size,
        "ask": ask,
        "ask_size": ask_size,
        "mid": mid,
        "spread": spread,
        "spread_pct": spread / mid * 100 if mid else np.nan,
    }


def quote_size_to_shares(raw_size, config):
    if pd.isna(raw_size):
        return np.nan

    return raw_size * config.quote_size_multiplier


def enough_visible_size(shares, visible_shares):
    if pd.isna(shares) or pd.isna(visible_shares):
        return np.nan

    return shares <= visible_shares


def uncovered_shares(shares, visible_shares):
    if pd.isna(shares) or pd.isna(visible_shares):
        return np.nan

    return max(shares - visible_shares, 0)


def visible_share_coverage_pct(shares, visible_shares):
    if pd.isna(shares) or pd.isna(visible_shares) or shares <= 0:
        return np.nan

    return min(visible_shares, shares) / shares * 100


def is_true(value):
    return bool(value) if pd.notna(value) else False


def buy_window_slippage_per_share(buy_price, best_ask):
    if pd.isna(buy_price) or pd.isna(best_ask):
        return np.nan

    if best_ask <= buy_price:
        return buy_price - best_ask

    return np.nan


def sell_window_slippage_per_share(sell_price, best_bid):
    if pd.isna(sell_price) or pd.isna(best_bid):
        return np.nan

    if best_bid >= sell_price:
        return best_bid - sell_price

    return np.nan


def total_slippage_amount(shares, buy_slippage, sell_slippage):
    if pd.isna(shares) or shares <= 0:
        return np.nan

    if pd.isna(buy_slippage) or pd.isna(sell_slippage):
        return np.nan

    return (buy_slippage + sell_slippage) * shares


def min_quote_value(quotes, column):
    if quotes.empty or column not in quotes.columns:
        return np.nan

    return quotes[column].min()


def max_quote_value(quotes, column):
    if quotes.empty or column not in quotes.columns:
        return np.nan

    return quotes[column].max()


def first_executable_limit_quote(
    quotes,
    limit_price,
    price_column,
    size_column,
    config,
    side,
):
    empty_execution = {
        "price": np.nan,
        "visible_shares": np.nan,
        "time": pd.NaT,
    }

    if (
        quotes.empty
        or pd.isna(limit_price)
        or price_column not in quotes.columns
        or size_column not in quotes.columns
    ):
        return empty_execution

    if side == "buy":
        eligible_quotes = quotes[quotes[price_column] <= limit_price]
    elif side == "sell":
        eligible_quotes = quotes[quotes[price_column] >= limit_price]
    else:
        raise ValueError("side must be 'buy' or 'sell'.")

    if eligible_quotes.empty:
        return empty_execution

    execution_quote = eligible_quotes.iloc[0]
    return {
        "price": execution_quote[price_column],
        "visible_shares": quote_size_to_shares(execution_quote[size_column], config),
        "time": execution_quote["timestamp"],
    }


def weighted_share_coverage_pct(trades, visible_shares_column):
    required_columns = {"estimated_shares", visible_shares_column}

    if not required_columns.issubset(trades.columns):
        return np.nan

    trades = trades[
        trades["estimated_shares"].notna()
        & trades[visible_shares_column].notna()
        & (trades["estimated_shares"] > 0)
    ]

    if trades.empty:
        return np.nan

    covered_shares = np.minimum(
        trades["estimated_shares"],
        trades[visible_shares_column],
    ).sum()
    return covered_shares / trades["estimated_shares"].sum() * 100


def check_trade_quotes(client, trade, config):
    try:
        buy_quotes = fetch_quotes_around_event(
            client,
            trade["symbol"],
            trade["buy_time"],
            config,
        )
        sell_quotes = fetch_quotes_around_event(
            client,
            trade["symbol"],
            trade["sell_time"],
            config,
        )
        buy_quote = nearest_quote(buy_quotes, trade["buy_time"], config)
        sell_quote = nearest_quote(sell_quotes, trade["sell_time"], config)
        buy_best_ask = min_quote_value(buy_quotes, "ask_price")
        sell_best_bid = max_quote_value(sell_quotes, "bid_price")
        buy_limit_execution = first_executable_limit_quote(
            buy_quotes,
            trade["buy_price"],
            price_column="ask_price",
            size_column="ask_size",
            config=config,
            side="buy",
        )
        sell_limit_execution = first_executable_limit_quote(
            sell_quotes,
            trade["sell_price"],
            price_column="bid_price",
            size_column="bid_size",
            config=config,
            side="sell",
        )
        quote_error = ""
    except Exception as error:
        buy_quote = nearest_quote(pd.DataFrame(), pd.NaT, config)
        sell_quote = nearest_quote(pd.DataFrame(), pd.NaT, config)
        buy_best_ask = np.nan
        sell_best_bid = np.nan
        buy_limit_execution = {
            "price": np.nan,
            "visible_shares": np.nan,
            "time": pd.NaT,
        }
        sell_limit_execution = {
            "price": np.nan,
            "visible_shares": np.nan,
            "time": pd.NaT,
        }
        quote_error = str(error)

    checked_trade = dict(trade)

    for prefix, quote in [("buy", buy_quote), ("sell", sell_quote)]:
        for key, value in quote.items():
            checked_trade[f"{prefix}_{key}"] = value

    buy_price = trade["buy_price"]
    sell_price = trade["sell_price"]
    shares = trade["shares"]

    if pd.isna(shares) and pd.notna(buy_price) and buy_price > 0:
        shares = config.fallback_notional / buy_price

    buy_visible_ask_shares = buy_limit_execution["visible_shares"]
    sell_visible_bid_shares = sell_limit_execution["visible_shares"]

    checked_trade["buy_market_price"] = buy_quote["ask"]
    checked_trade["sell_market_price"] = sell_quote["bid"]
    checked_trade["buy_best_ask_in_window"] = buy_best_ask
    checked_trade["sell_best_bid_in_window"] = sell_best_bid
    checked_trade["buy_limit_execution_price"] = buy_limit_execution["price"]
    checked_trade["sell_limit_execution_price"] = sell_limit_execution["price"]
    checked_trade["buy_limit_execution_time"] = buy_limit_execution["time"]
    checked_trade["sell_limit_execution_time"] = sell_limit_execution["time"]
    checked_trade["buy_limit_visible_shares"] = buy_visible_ask_shares
    checked_trade["sell_limit_visible_shares"] = sell_visible_bid_shares
    checked_trade["buy_price_available_in_window"] = pd.notna(
        buy_limit_execution["price"]
    )
    checked_trade["sell_price_available_in_window"] = pd.notna(
        sell_limit_execution["price"]
    )
    checked_trade["estimated_shares"] = shares
    checked_trade["buy_visible_ask_shares"] = buy_visible_ask_shares
    checked_trade["sell_visible_bid_shares"] = sell_visible_bid_shares
    checked_trade["buy_visible_liquidity_ok"] = (
        enough_visible_size(shares, buy_visible_ask_shares)
    )
    checked_trade["sell_visible_liquidity_ok"] = (
        enough_visible_size(shares, sell_visible_bid_shares)
    )
    checked_trade["buy_uncovered_shares"] = (
        uncovered_shares(shares, buy_visible_ask_shares)
    )
    checked_trade["sell_uncovered_shares"] = (
        uncovered_shares(shares, sell_visible_bid_shares)
    )
    checked_trade["buy_visible_share_coverage_pct"] = (
        visible_share_coverage_pct(shares, buy_visible_ask_shares)
    )
    checked_trade["sell_visible_share_coverage_pct"] = (
        visible_share_coverage_pct(shares, sell_visible_bid_shares)
    )
    checked_trade["buy_slippage_per_share"] = buy_quote["ask"] - buy_price
    checked_trade["sell_slippage_per_share"] = sell_price - sell_quote["bid"]
    checked_trade["round_trip_slippage_per_share"] = (
        checked_trade["buy_slippage_per_share"]
        + checked_trade["sell_slippage_per_share"]
    )
    checked_trade["round_trip_slippage_dollars"] = (
        checked_trade["round_trip_slippage_per_share"] * shares
    )
    checked_trade["buy_window_slippage_per_share"] = buy_window_slippage_per_share(
        buy_price,
        buy_limit_execution["price"],
    )
    checked_trade["sell_window_slippage_per_share"] = (
        sell_window_slippage_per_share(
            sell_price,
            sell_limit_execution["price"],
        )
    )
    checked_trade["estimated_slippage_amount"] = total_slippage_amount(
        shares,
        checked_trade["buy_window_slippage_per_share"],
        checked_trade["sell_window_slippage_per_share"],
    )

    if is_true(checked_trade["buy_price_available_in_window"]) and is_true(
        checked_trade["sell_price_available_in_window"]
    ):
        quote_based_return = (
            checked_trade["sell_limit_execution_price"]
            - checked_trade["buy_limit_execution_price"]
        ) / checked_trade["buy_limit_execution_price"]
        checked_trade["quote_based_return"] = quote_based_return
        checked_trade["quote_based_return_pct"] = quote_based_return * 100
        checked_trade["return_lost_to_quotes_pct"] = (
            trade["trade_return_pct"] - checked_trade["quote_based_return_pct"]
        )
    else:
        checked_trade["quote_based_return"] = np.nan
        checked_trade["quote_based_return_pct"] = np.nan
        checked_trade["return_lost_to_quotes_pct"] = np.nan

    checked_trade["quote_error"] = quote_error
    return checked_trade


def add_quote_checks(trade_log, config):
    client = get_stock_data_client()
    checked_rows = []
    total_trades = len(trade_log)

    if total_trades == 0:
        return pd.DataFrame()

    trades = trade_log.to_dict("records")

    with ThreadPoolExecutor(max_workers=config.max_workers) as executor:
        future_to_order = {
            executor.submit(check_trade_quotes, client, trade, config): order
            for order, trade in enumerate(trades)
        }

        for completed_count, future in enumerate(as_completed(future_to_order), start=1):
            checked_trade = future.result()
            checked_trade["_original_order"] = future_to_order[future]
            checked_rows.append(checked_trade)

            if (
                config.progress_interval
                and completed_count % config.progress_interval == 0
            ):
                print(f"Checked {completed_count} of {total_trades} new trades...")

    return (
        pd.DataFrame(checked_rows)
        .sort_values("_original_order")
        .drop(columns=["_original_order"])
    )


def merge_existing_quote_checks(existing_checks, new_checks, requested_keys):
    frames = []

    if not existing_checks.empty:
        frames.append(existing_checks[existing_checks["_trade_key"].isin(requested_keys)])

    if not new_checks.empty:
        frames.append(new_checks)

    if not frames:
        return pd.DataFrame()

    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates("_trade_key", keep="last")
    merged = merged[merged["_trade_key"].isin(requested_keys)]
    merged = merged.drop(columns=["_trade_key"])
    return merged


def add_missing_derived_columns(checked_trades):
    checked_trades = checked_trades.copy()

    if {
        "estimated_shares",
        "buy_window_slippage_per_share",
        "sell_window_slippage_per_share",
    }.issubset(
        checked_trades.columns
    ):
        checked_trades["estimated_slippage_amount"] = checked_trades.apply(
            lambda row: total_slippage_amount(
                row["estimated_shares"],
                row["buy_window_slippage_per_share"],
                row["sell_window_slippage_per_share"],
            ),
            axis=1,
        )

    if (
        "buy_visible_share_coverage_pct" not in checked_trades.columns
        and {"estimated_shares", "buy_visible_ask_shares"}.issubset(checked_trades.columns)
    ):
        checked_trades["buy_visible_share_coverage_pct"] = checked_trades.apply(
            lambda row: visible_share_coverage_pct(
                row["estimated_shares"],
                row["buy_visible_ask_shares"],
            ),
            axis=1,
        )

    if (
        "sell_visible_share_coverage_pct" not in checked_trades.columns
        and {"estimated_shares", "sell_visible_bid_shares"}.issubset(checked_trades.columns)
    ):
        checked_trades["sell_visible_share_coverage_pct"] = checked_trades.apply(
            lambda row: visible_share_coverage_pct(
                row["estimated_shares"],
                row["sell_visible_bid_shares"],
            ),
            axis=1,
        )

    return checked_trades


def print_quote_summary(checked_trades):
    if checked_trades.empty:
        print("\nQuote/spread check")
        print("------------------")
        print("No trades checked.")
        return

    completed_trades = checked_trades[checked_trades["trade_return"].notna()]

    print("\nQuote/spread check")
    print("------------------")
    print("Trades checked:", len(checked_trades))
    print("Buy quotes found:", checked_trades["buy_bid"].notna().sum())
    print("Sell quotes found:", checked_trades["sell_bid"].notna().sum())

    if "quote_error" in checked_trades.columns:
        quote_errors = checked_trades["quote_error"].fillna("").ne("").sum()
        print("Quote errors:", quote_errors)

    if completed_trades.empty:
        return

    print("Average buy spread %:", round(completed_trades["buy_spread_pct"].mean(), 4))
    print("Median buy spread %:", round(completed_trades["buy_spread_pct"].median(), 4))
    print("Average sell spread %:", round(completed_trades["sell_spread_pct"].mean(), 4))
    print("Median sell spread %:", round(completed_trades["sell_spread_pct"].median(), 4))
    print(
        "Average backtest return %:",
        round(completed_trades["trade_return_pct"].mean(), 4),
    )
    print(
        "Average quote-based return %:",
        round(completed_trades["quote_based_return_pct"].mean(), 4),
    )
    print(
        "Average return lost to quotes %:",
        round(completed_trades["return_lost_to_quotes_pct"].mean(), 4),
    )
    print(
        "Total estimated slippage amount $:",
        round(completed_trades.get("estimated_slippage_amount", pd.Series()).sum(), 2),
    )
    print(
        "Buy visible share coverage %:",
        round(
            weighted_share_coverage_pct(completed_trades, "buy_visible_ask_shares"),
            2,
        ),
    )
    print(
        "Sell visible share coverage %:",
        round(
            weighted_share_coverage_pct(completed_trades, "sell_visible_bid_shares"),
            2,
        ),
    )
    print(
        "Buy size covered by visible ask:",
        completed_trades["buy_visible_liquidity_ok"].sum(),
        "of",
        len(completed_trades),
    )
    print(
        "Sell size covered by visible bid:",
        completed_trades["sell_visible_liquidity_ok"].sum(),
        "of",
        len(completed_trades),
    )


def print_intraday_period_summary(quote_log, quote_config):
    strategy_config = StrategyConfig(
        summary_start=quote_config.start_date,
        summary_end=quote_config.end_date,
        summary_period_months=quote_config.summary_period_months,
    )
    regular_data = run_strategy(strategy_config)
    trade_log = create_trade_log(
        regular_data,
        start_date=strategy_config.summary_start,
        end_date=strategy_config.summary_end,
    )

    if strategy_config.simulate_real_trading:
        trade_log = add_real_trading_simulation(
            trade_log,
            strategy_config,
            start_date=strategy_config.summary_start,
            end_date=strategy_config.summary_end,
        )

    period_summary = create_period_summary_table(
        regular_data,
        start_date=strategy_config.summary_start,
        end_date=strategy_config.summary_end,
        months=strategy_config.summary_period_months,
        trade_log=trade_log if strategy_config.simulate_real_trading else None,
        config=strategy_config if strategy_config.simulate_real_trading else None,
        quote_log=quote_log,
    )
    print_period_summary_table(period_summary)


def parse_args():
    strategy_config = StrategyConfig()
    parser = argparse.ArgumentParser(
        description="Check trade log entries against Alpaca IEX quotes."
    )
    parser.add_argument(
        "--trade-log",
        type=Path,
        default=QuoteCheckConfig.trade_log_path,
    )
    parser.add_argument("--output", type=Path, default=QuoteCheckConfig.output_path)
    parser.add_argument("--start", default=strategy_config.summary_start.isoformat())
    parser.add_argument("--end", default=strategy_config.summary_end.isoformat())
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=QuoteCheckConfig.quote_window_minutes,
    )
    parser.add_argument(
        "--max-delay-seconds",
        type=int,
        default=QuoteCheckConfig.max_quote_delay_seconds,
    )
    parser.add_argument(
        "--fallback-notional",
        type=float,
        default=QuoteCheckConfig.fallback_notional,
    )
    parser.add_argument(
        "--quote-size-multiplier",
        type=int,
        default=QuoteCheckConfig.quote_size_multiplier,
        help="Use 1 when quote sizes are already shares. Use 100 for round-lot sizes.",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Fetch every selected trade again instead of reusing existing output rows.",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=QuoteCheckConfig.max_workers,
        help="Number of Alpaca quote requests to run at the same time.",
    )
    parser.add_argument(
        "--period-months",
        type=int,
        default=strategy_config.summary_period_months,
        help="Number of months per quote summary table row.",
    )
    parser.add_argument("--limit", type=int, default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    config = QuoteCheckConfig(
        trade_log_path=args.trade_log,
        output_path=args.output,
        start_date=parse_date(args.start),
        end_date=parse_date(args.end),
        quote_window_minutes=args.window_minutes,
        max_quote_delay_seconds=args.max_delay_seconds,
        fallback_notional=args.fallback_notional,
        quote_size_multiplier=args.quote_size_multiplier,
        reuse_existing_output=not args.no_cache,
        max_workers=args.max_workers,
        summary_period_months=args.period_months,
    )

    trade_log = load_trade_log(
        config.trade_log_path,
        start_date=config.start_date,
        end_date=config.end_date,
    )

    if args.limit is not None:
        trade_log = trade_log.head(args.limit)

    requested_keys = set(trade_log["_trade_key"])
    existing_checks = (
        load_existing_quote_checks(config.output_path)
        if config.reuse_existing_output
        else pd.DataFrame()
    )
    existing_keys = (
        set(existing_checks["_trade_key"])
        if not existing_checks.empty
        else set()
    )
    unchecked_trades = trade_log[~trade_log["_trade_key"].isin(existing_keys)]

    print("Selected trades:", len(trade_log))
    print("Reused existing checks:", len(trade_log) - len(unchecked_trades))
    print("New Alpaca checks needed:", len(unchecked_trades))
    print("Parallel workers:", config.max_workers)

    if unchecked_trades.empty:
        new_checks = pd.DataFrame()
    else:
        new_checks = add_quote_checks(unchecked_trades, config)

    checked_trades = merge_existing_quote_checks(
        existing_checks,
        new_checks,
        requested_keys,
    )
    checked_trades = add_missing_derived_columns(checked_trades)
    checked_trades.to_csv(config.output_path, index=False)

    print_intraday_period_summary(checked_trades, config)
    print("Saved:", config.output_path)


if __name__ == "__main__":
    main()
