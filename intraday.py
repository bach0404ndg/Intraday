from dataclasses import dataclass, replace
from datetime import date, time, timedelta
from pathlib import Path
import os
import shutil
import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

os.environ.setdefault("MPLCONFIGDIR", str(Path(".matplotlib-cache")))

ENV_FILE = Path(".env")

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "APLD"
    start: str = "2025-01-01"
    end: str = "2026-09-07"
    timezone: str = "America/New_York"
    bar_minutes: int = 5

    buy_regression_bars: int = 7
    body_average_bars: int = 7
    range_quantile: float = 0.3
    doji_body_range_ratio: float = 0.15
    latest_buy_time: time = time(14, 0)

    near_high_pct: float = 0.002
    sell_regression_bars: int = 12
    sell_slope_slowdown_pct: float = 0
    force_exit_start_time: time = time(15, 40)
    late_sell_start_time: time = time(14, 0)
    final_exit_time: time = time(15, 40)

    output_folder: Path = Path("trade graph")
    summary_graph_folder: Path = Path("summary trade graph")
    negative_return_folder: Path = Path("negative trade graph")
    non_executed_setup_folder: Path = Path("non_executed trade graph")
    update_graphs: bool = True
    clear_existing_graphs: bool = True
    plot_all_dates: bool = False
    plot_summary_dates: bool = True
    plot_filtered_folders: bool = True
    show_plots: bool = False
    save_trade_log: bool = True
    trade_log_path: Path = Path("trade_log.csv")
    print_period_summary: bool = True
    summary_period_months: int = 1
    include_quote_check_in_summary: bool = True
    quote_check_path: Path = Path("quote_spread_check.csv")
    simulate_real_trading: bool = True
    starting_money: float = 10000.0
    money_per_trade: float = 10000.0
    trading_cost: float = 2.0

    summary_start: date = date(2026, 7, 25)
    summary_end: date = date(2026, 8, 10)
    fetch_lookback_days: int = 10


def get_stock_data_client():
    load_env_file()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret_key = os.environ.get("ALPACA_SECRET_KEY")

    if not api_key or not secret_key:
        raise RuntimeError(
            "Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env or your terminal."
        )

    return StockHistoricalDataClient(api_key, secret_key)


def fetch_bars(config):
    request = StockBarsRequest(
        symbol_or_symbols=config.symbol,
        timeframe=TimeFrame(config.bar_minutes, TimeFrameUnit.Minute),
        start=config.start,
        end=config.end,
    )

    bars = get_stock_data_client().get_stock_bars(request)
    return bars.df.reset_index()


def label_session(timestamp):
    current_time = timestamp.time()

    if time(4, 0) <= current_time < time(9, 30):
        return "pre_market"

    if time(9, 30) <= current_time < time(16, 0):
        return "regular"

    if time(16, 0) <= current_time < time(20, 0):
        return "after_hours"

    return "overnight"


def get_session_data(data, session="regular"):
    valid_sessions = {
        "pre_market",
        "regular",
        "after_hours",
        "overnight",
    }

    if session not in valid_sessions:
        raise ValueError(f"Invalid session: {session}")

    return data[data["session"] == session].copy()


def clock_times(timestamps):
    return timestamps.map(
        lambda timestamp: timestamp.time() if pd.notna(timestamp) else np.nan
    )


def clock_time_before(timestamps, cutoff):
    return timestamps.map(
        lambda timestamp: pd.notna(timestamp) and timestamp.time() < cutoff
    )


def clock_time_on_or_after(timestamps, cutoff):
    return timestamps.map(
        lambda timestamp: pd.notna(timestamp) and timestamp.time() >= cutoff
    )


def add_base_features(data, config):
    data = data.copy()
    data["timestamp"] = data["timestamp"].dt.tz_convert(config.timezone)
    data["session"] = data["timestamp"].apply(label_session)
    data["body"] = (data["close"] - data["open"]).abs()
    data["range"] = data["high"] - data["low"]
    data["body_pct"] = data["body"] / data["open"]
    data["range_pct"] = data["range"] / data["open"]
    data["date"] = data["timestamp"].dt.date

    data["candle_color"] = np.where(
        data["close"] > data["open"],
        "green",
        np.where(data["close"] < data["open"], "red", "orange"),
    )

    return data


def add_gap_features(data):
    data = data.copy()
    data["clock_time"] = clock_times(data["timestamp"])

    open_930 = (
        data[data["clock_time"] == time(9, 30)]
        .set_index("date")["open"]
    )
    close_4 = (
        data[data["clock_time"] == time(15, 55)]
        .set_index("date")["close"]
    )

    previous_close_4 = close_4.shift(1)
    opening_gap = (open_930 - previous_close_4) / previous_close_4

    data["opening_gap"] = data["date"].map(opening_gap)
    data["regular_open"] = data["date"].map(open_930)
    data["prev_close"] = data["date"].map(previous_close_4)
    data["current_gap"] = (data["close"] - data["regular_open"]) / data["regular_open"]
    data["gap_change"] = data.groupby("date")["current_gap"].diff()

    data["recent_low"] = (
        data.groupby("date")["low"]
        .transform(lambda x: x.shift(1).cummin())
    )

    data["recent_high"] = (
        data.groupby("date")["high"]
        .transform(lambda x: x.shift(1).cummax())
    )

    return data


def price_slope_pct(values):
    time_index = np.arange(len(values))

    slope = np.polyfit(
        time_index,
        np.log(values),
        1
    )[0]

    return np.exp(slope) - 1


def price_r2(values):
    time_index = np.arange(len(values))

    slope, intercept = np.polyfit(time_index, values, 1)

    fitted = slope * time_index + intercept

    ss_res = np.sum((values - fitted) ** 2)
    ss_tot = np.sum((values - np.mean(values)) ** 2)

    if ss_tot == 0:
        return 0

    return 1 - ss_res / ss_tot


def rolling_slope_pct_by_day(data, column, bars):
    return (
        data.groupby("date")[column]
        .transform(lambda x: x.shift(1).rolling(bars).apply(price_slope_pct, raw=True))
    )


def rolling_r2_by_day(data, column, bars):
    return (
        data.groupby("date")[column]
        .transform(lambda x: x.shift(1).rolling(bars).apply(price_r2, raw=True))
    )


def add_buy_signals(data, config):
    data = data.copy()

    data["avg_range_pct"] = (
        data.groupby("date")["range_pct"]
        .transform(lambda x: x.shift(1).rolling(config.body_average_bars).mean())
    )
    data["doji_body"] = (
        data["body_pct"] < config.doji_body_range_ratio * data["avg_range_pct"]
    )
    data["range_quantile"] = (
        data.groupby("date")["range_pct"]
        .transform(
            lambda x: x.shift(1)
            .rolling(config.body_average_bars)
            .quantile(config.range_quantile)
        )
    )
    data["small_range"] = data["range_pct"] < data["range_quantile"]
    data["slope"] = rolling_slope_pct_by_day(
        data,
        column="close",
        bars=config.buy_regression_bars,
    )
    by_day = data.groupby("date")
    data["confirmation_candle_color"] = by_day["candle_color"].shift(-1)
    data["execution_time"] = by_day["timestamp"].shift(-2)
    data["execution_price"] = by_day["open"].shift(-2)

    data["before_latest_buy_time"] = clock_time_before(
        data["execution_time"],
        config.latest_buy_time,
    )

    data["setup_signal"] = (
            (data["slope"] < 0)
            & data["doji_body"]
            & (data["confirmation_candle_color"] == "green")
    )

    candidate_buy = (
            data["setup_signal"]
            & data["execution_price"].notna()
            & data["before_latest_buy_time"]
    )

    data["buy_signal"] = (
            candidate_buy
            & (candidate_buy.groupby(data["date"]).cumsum() == 1)
    )

    data["buy_price"] = data["execution_price"].where(data["buy_signal"])
    data["buy_time"] = data["execution_time"].where(data["buy_signal"])

    return data




# def add_high_after_buy(data):
#     data = data.copy()
#     data["daily_high_after_buy"] = np.nan
#
#     for row_index, row in data[data["buy_signal"]].iterrows():
#         later_prices = data.loc[
#             (data["date"] == row["date"])
#             & (data["timestamp"] > row["buy_time"]),
#             "high",
#         ]
#         data.at[row_index, "daily_high_after_buy"] = later_prices.max()
#
#     data["daily_first_buy_time"] = data.groupby("date")["buy_time"].transform("first")
#     data["daily_first_buy_price"] = data.groupby("date")["buy_price"].transform("first")
#     data["after_buy"] = data["timestamp"] >= data["daily_first_buy_time"]
#     data["high_since_buy"] = (
#         data["high"]
#         .where(data["after_buy"])
#         .groupby(data["date"])
#         .cummax()
#     )
#     data["previous_high"] = data.groupby("date")["high"].shift(1)
#     data["previous_high_since_buy"] = (
#         data.groupby("date")["high_since_buy"].shift(1)
#     )
#
#     return data
#
#
# def add_sell_signals(data, config):
#     data = data.copy()
#     data["clock_time"] = clock_times(data["timestamp"])
#     data["force_exit_time_ready"] = clock_time_on_or_after(
#         data["timestamp"],
#         config.force_exit_start_time,
#     )
#     data["late_sell_time_ready"] = clock_time_on_or_after(
#         data["timestamp"],
#         config.late_sell_start_time,
#     )
#     data["final_exit_time_ready"] = data["clock_time"] == config.final_exit_time
#     by_day = data.groupby("date")
#     data["next_open"] = by_day["open"].shift(-1)
#     data["next_time"] = by_day["timestamp"].shift(-1)
#
#     data["near_high"] = (
#         data["previous_high"]
#         >= data["previous_high_since_buy"] * (1 - config.near_high_pct)
#     )
#     data["slope_sell"] = rolling_slope_pct_by_day(
#         data,
#         column="close",
#         bars=config.sell_regression_bars,
#     )
#     data["previous_slope_sell"] = data.groupby("date")["slope_sell"].shift(1)
#     data["slope_slowing_enough"] = (
#         data["slope_sell"]
#         < data["previous_slope_sell"] * (1 - config.sell_slope_slowdown_pct)
#     )
#     data["force_exit"] = (
#         (data["timestamp"] > data["daily_first_buy_time"])
#         & data["force_exit_time_ready"]
#         & (data["low"] <= data["daily_first_buy_price"])
#         & (data["high"] >= data["daily_first_buy_price"])
#     )
#     data["sell_regression_ready"] = (
#         data["timestamp"]
#         >= data["daily_first_buy_time"]
#         + pd.Timedelta(minutes=config.bar_minutes * config.sell_regression_bars + 1)
#     )
#     data["profit_sell_signal"] = (
#         data["sell_regression_ready"]
#         & (data["slope_sell"] > 0)
#         & data["slope_slowing_enough"]
#         & data["near_high"]
#     )
#     data["late_sell_signal"] = (
#         data["sell_regression_ready"]
#         & (data["slope_sell"] > 0)
#         & data["slope_slowing_enough"]
#         & data["late_sell_time_ready"]
#     )
#     data["final_exit_signal"] = (
#         (data["timestamp"] > data["daily_first_buy_time"])
#         & data["final_exit_time_ready"]
#         & ~data["profit_sell_signal"]
#         & ~data["late_sell_signal"]
#         & ~data["force_exit"]
#     )
#
#     data["sell_signal"] = (
#         data["profit_sell_signal"]
#         | data["late_sell_signal"]
#         | data["force_exit"]
#         | data["final_exit_signal"]
#     )
#
#     data["sell_price"] = data["open"]
#     data["sell_price"] = data["sell_price"].where(
#         ~data["force_exit"],
#         data["daily_first_buy_price"],
#     )
#     data["sell_price"] = pd.Series(data["sell_price"], index=data.index).where(
#         data["sell_signal"]
#     )
#     data["sell_time"] = data["timestamp"].where(data["sell_signal"])
#     data["first_sell_signal"] = (
#         data["sell_signal"] & (data.groupby("date")["sell_signal"].cumsum() == 1)
#     )
#
#     return data
#
#
# def add_trade_returns(data):
#     data = data.copy()
#     data["max_return"] = (
#         (data["daily_high_after_buy"] - data["buy_price"]) / data["buy_price"]
#     ).where(data["first_buy_signal"])
#
#     data["first_buy_price"] = data.groupby("date")["buy_price"].transform("first")
#     data["trade_return"] = (
#         (data["sell_price"] - data["first_buy_price"]) / data["first_buy_price"]
#     ).where(data["first_sell_signal"])
#
#     return data
#
#
# GAP_TRADE_LOG_COLUMNS = [
#     "opening_gap",
#     "current_gap"
# ]
#
# EXTRA_TRADE_LOG_COLUMNS = []
#
#
# def gap_values_for_trade_log(row):
#     return {
#         column: row[column] if column in row.index else np.nan
#         for column in GAP_TRADE_LOG_COLUMNS
#     }
#
#
# def extra_values_for_trade_log(row):
#     return {
#         column: row[column] if column in row.index else np.nan
#         for column in EXTRA_TRADE_LOG_COLUMNS
#     }
#
#
# def create_trade_log(data, start_date=None, end_date=None):
#     data = data.copy()
#
#     if start_date is not None:
#         data = data[data["date"] >= start_date]
#
#     if end_date is not None:
#         data = data[data["date"] <= end_date]
#
#     trade_rows = []
#
#     for trading_date, day_data in data.groupby("date"):
#         buy_rows = day_data[day_data["first_buy_signal"]]
#
#         if buy_rows.empty:
#             setup_rows = day_data[day_data["setup_signal"]]
#             reference_row = (
#                 setup_rows.iloc[0]
#                 if not setup_rows.empty
#                 else day_data.iloc[0]
#             )
#             trade_rows.append(
#                 {
#                     "date": trading_date,
#                     "symbol": data["symbol"].iloc[0] if "symbol" in data.columns else "",
#                     "setup_time": (
#                         reference_row["timestamp"]
#                         if not setup_rows.empty
#                         else pd.NaT
#                     ),
#                     "buy_time": pd.NaT,
#                     "buy_price": np.nan,
#                     "sell_time": pd.NaT,
#                     "sell_price": np.nan,
#                     "trade_return": 0,
#                     "trade_return_pct": 0,
#                     "exit_type": "no_trade",
#                     "max_return": np.nan,
#                     "max_return_pct": np.nan,
#                     "daily_high_after_buy": np.nan,
#                     **gap_values_for_trade_log(reference_row),
#                     **extra_values_for_trade_log(reference_row),
#                 }
#             )
#             continue
#
#         buy_row = buy_rows.iloc[0]
#         sell_rows = day_data[day_data["first_sell_signal"]]
#
#         if sell_rows.empty:
#             sell_row = None
#             exit_type = "no_sell"
#             sell_time = pd.NaT
#             sell_price = np.nan
#             trade_return = np.nan
#         else:
#             sell_row = sell_rows.iloc[0]
#             if sell_row["force_exit"]:
#                 exit_type = "force_exit"
#             elif sell_row["late_sell_signal"]:
#                 exit_type = "late_sell"
#             elif sell_row["final_exit_signal"]:
#                 exit_type = "final_exit"
#             else:
#                 exit_type = "profit_sell"
#             sell_time = sell_row["sell_time"]
#             sell_price = sell_row["sell_price"]
#             trade_return = sell_row["trade_return"]
#
#         trade_rows.append(
#             {
#                 "date": trading_date,
#                 "symbol": data["symbol"].iloc[0] if "symbol" in data.columns else "",
#                 "setup_time": buy_row["timestamp"],
#                 "buy_time": buy_row["buy_time"],
#                 "buy_price": buy_row["buy_price"],
#                 "sell_time": sell_time,
#                 "sell_price": sell_price,
#                 "trade_return": trade_return,
#                 "trade_return_pct": trade_return * 100,
#                 "exit_type": exit_type,
#                 "max_return": buy_row["max_return"],
#                 "max_return_pct": buy_row["max_return"] * 100,
#                 "daily_high_after_buy": buy_row["daily_high_after_buy"],
#                 **gap_values_for_trade_log(buy_row),
#                 **extra_values_for_trade_log(buy_row),
#             }
#         )
#
#     return pd.DataFrame(trade_rows)
#
#
# def save_trade_log(trade_log, config):
#     trade_log.to_csv(config.trade_log_path, index=False)
#     print("Trade log saved:", config.trade_log_path)
#
#
# def load_trade_log(path=None):
#     if path is None:
#         path = StrategyConfig().trade_log_path
#
#     trade_log = pd.read_csv(path)
#
#     for column in ["date", "setup_time", "buy_time", "sell_time"]:
#         if column in trade_log.columns:
#             trade_log[column] = pd.to_datetime(trade_log[column], errors="coerce")
#
#     return trade_log
#
#
# def add_real_trading_simulation(trade_log, config, start_date=None, end_date=None):
#     trade_log = trade_log.copy()
#     current_money = config.starting_money
#
#     trade_log["starting_money"] = np.nan
#     trade_log["money_used"] = np.nan
#     trade_log["shares"] = np.nan
#     trade_log["gross_pnl"] = np.nan
#     trade_log["trading_cost"] = np.nan
#     trade_log["net_pnl"] = np.nan
#     trade_log["ending_money"] = np.nan
#
#     simulation_dates = pd.Series(True, index=trade_log.index)
#
#     if start_date is not None:
#         simulation_dates &= trade_log["date"] >= start_date
#
#     if end_date is not None:
#         simulation_dates &= trade_log["date"] <= end_date
#
#     for row_index, row in trade_log.iterrows():
#         if not simulation_dates.at[row_index]:
#             continue
#
#         trade_log.at[row_index, "starting_money"] = current_money
#
#         if current_money <= 0 or pd.isna(row["buy_price"]):
#             trade_log.at[row_index, "ending_money"] = current_money
#             continue
#
#         money_used = min(config.money_per_trade, current_money)
#         shares = money_used / row["buy_price"]
#
#         trade_log.at[row_index, "money_used"] = money_used
#         trade_log.at[row_index, "shares"] = shares
#
#         if pd.isna(row["trade_return"]):
#             trade_log.at[row_index, "ending_money"] = current_money
#             continue
#
#         gross_pnl = money_used * row["trade_return"]
#         net_pnl = gross_pnl - config.trading_cost
#         current_money += net_pnl
#
#         trade_log.at[row_index, "gross_pnl"] = gross_pnl
#         trade_log.at[row_index, "trading_cost"] = config.trading_cost
#         trade_log.at[row_index, "net_pnl"] = net_pnl
#         trade_log.at[row_index, "ending_money"] = current_money
#
#     return trade_log
#
#
# def summarize_real_trading(trade_log, config, start_date=None, end_date=None):
#     trade_log = trade_log.copy()
#
#     if start_date is not None:
#         trade_log = trade_log[trade_log["date"] >= start_date]
#
#     if end_date is not None:
#         trade_log = trade_log[trade_log["date"] <= end_date]
#
#     completed_trades = trade_log[trade_log["net_pnl"].notna()]
#     period_starting_money = (
#         completed_trades["starting_money"].iloc[0]
#         if not completed_trades.empty
#         else config.starting_money
#     )
#     ending_money = (
#         completed_trades["ending_money"].iloc[-1]
#         if not completed_trades.empty
#         else period_starting_money
#     )
#     total_net_pnl = completed_trades["net_pnl"].sum()
#
#     return {
#         "starting_money": period_starting_money,
#         "ending_money": ending_money,
#         "total_net_pnl": total_net_pnl,
#         "net_return_pct": safe_pct(total_net_pnl, period_starting_money),
#         "completed_trades": len(completed_trades),
#         "total_trading_cost": completed_trades["trading_cost"].sum(),
#     }
#
#
# def print_real_trading_summary(summary):
#     print("\nReal trading simulation:")
#     print("Starting money:", round(summary["starting_money"], 2))
#     print("Ending money:", round(summary["ending_money"], 2))
#     print("Total net P/L:", round(summary["total_net_pnl"], 2))
#     print("Net return:", format_pct(summary["net_return_pct"], digits=3))
#     print("Completed trades:", summary["completed_trades"])
#     print("Total trading cost:", round(summary["total_trading_cost"], 2))
#
#
# def run_strategy_on_data(raw_data, config):
#     featured_data = add_base_features(raw_data, config)
#
#     if "symbol" in featured_data.columns:
#         strategy_data = featured_data[featured_data["symbol"] == config.symbol].copy()
#     else:
#         strategy_data = featured_data.copy()
#
#     strategy_data = add_gap_features(strategy_data)
#
#     regular_data = strategy_data[strategy_data["session"] == "regular"].copy()
#     regular_data = add_buy_signals(regular_data, config)
#     regular_data = add_high_after_buy(regular_data)
#     regular_data = add_sell_signals(regular_data, config)
#     regular_data = add_trade_returns(regular_data)
#
#     return regular_data
#
#
# def run_strategy(config):
#     raw_data = fetch_bars(config)
#     return run_strategy_on_data(raw_data, config)
#
#
# def main_run_config(config):
#     if config.plot_all_dates or config.summary_start is None:
#         return config
#
#     fetch_start = config.summary_start - timedelta(days=config.fetch_lookback_days)
#     fetch_end = (
#         config.summary_end + timedelta(days=1)
#         if config.summary_end is not None
#         else config.end
#     )
#     return replace(
#         config,
#         start=fetch_start.isoformat(),
#         end=fetch_end.isoformat() if hasattr(fetch_end, "isoformat") else fetch_end,
#     )
#
#
# def plot_trade_day(data, selected_date, config, output_folder=None, show=True):
#     import matplotlib.dates as mdates
#     import matplotlib.pyplot as plt
#     from matplotlib.patches import Rectangle
#
#     day_data = data[data["date"].astype(str) == str(selected_date)].copy()
#
#     if day_data.empty:
#         print("No data for this date.")
#         return
#
#     output_folder = output_folder or config.output_folder
#     output_folder.mkdir(parents=True, exist_ok=True)
#
#     _, ax = plt.subplots(figsize=(20, 12))
#
#     for _, row in day_data.iterrows():
#         x = mdates.date2num(row["timestamp"])
#         color = row["candle_color"]
#
#         ax.plot([x, x], [row["low"], row["high"]], color=color, linewidth=1)
#
#         body_bottom = min(row["open"], row["close"])
#         body_height = max(row["body"], 0.001)
#         ax.add_patch(
#             Rectangle(
#                 (x - 0.0012, body_bottom),
#                 0.0024,
#                 body_height,
#                 facecolor=color,
#                 edgecolor=color,
#             )
#         )
#
#     for _, row in day_data[day_data["first_buy_signal"]].iterrows():
#         buy_time = row["buy_time"]
#         buy_price = row["buy_price"]
#         buy_candle_low = day_data.loc[
#             day_data["timestamp"] == buy_time,
#             "low",
#         ].iloc[0]
#
#         ax.scatter(
#             buy_time,
#             buy_candle_low - 0.05,
#             marker="^",
#             s=150,
#             zorder=5,
#             color="black",
#         )
#         ax.annotate(
#             f"{buy_time.strftime('%H:%M')}\n${buy_price:.2f}",
#             (buy_time, buy_candle_low - 0.05),
#             xytext=(0, -10),
#             textcoords="offset points",
#             ha="center",
#             va="top",
#         )
#
#         high_after_buy_price = row["daily_high_after_buy"]
#         if pd.notna(high_after_buy_price):
#             high_after_buy_time = day_data.loc[
#                 (day_data["timestamp"] > buy_time)
#                 & (day_data["high"] == high_after_buy_price),
#                 "timestamp",
#             ].iloc[0]
#
#             ax.scatter(
#                 high_after_buy_time,
#                 high_after_buy_price + 0.05,
#                 marker="v",
#                 s=150,
#                 zorder=5,
#                 color="black",
#             )
#             ax.annotate(
#                 f"{high_after_buy_time.strftime('%H:%M')}\n"
#                 f"${high_after_buy_price:.2f}",
#                 (high_after_buy_time, high_after_buy_price + 0.05),
#                 xytext=(0, 10),
#                 textcoords="offset points",
#                 ha="center",
#                 va="bottom",
#             )
#
#     for _, row in day_data[day_data["first_sell_signal"]].iterrows():
#         sell_time = row["sell_time"]
#         sell_price = row["sell_price"]
#         candle_high = day_data.loc[
#             day_data["timestamp"] == sell_time,
#             "high",
#         ].iloc[0]
#
#         ax.scatter(
#             sell_time,
#             candle_high + 0.05,
#             marker="v",
#             s=150,
#             zorder=5,
#             color="blue",
#         )
#         ax.annotate(
#             f"{sell_time.strftime('%H:%M')}\n${sell_price:.2f}",
#             (sell_time, candle_high + 0.05),
#             xytext=(0, 10),
#             textcoords="offset points",
#             ha="center",
#             va="bottom",
#         )
#
#     ax.xaxis.set_major_formatter(
#         mdates.DateFormatter("%H:%M", tz=config.timezone)
#     )
#     ax.xaxis.set_major_locator(
#         mdates.MinuteLocator(byminute=[0, 30], tz=config.timezone)
#     )
#
#     plt.xticks(rotation=45)
#     ax.set_title(f"{config.symbol} {selected_date}")
#     ax.set_xlabel("Time")
#     ax.set_ylabel("Price")
#     plt.tight_layout()
#
#     output_path = output_folder / f"{config.symbol}_{selected_date}.png"
#     plt.savefig(output_path, dpi=150, bbox_inches="tight")
#
#     if show:
#         plt.show()
#     else:
#         plt.close()
#
#
# def clear_graph_files(folder, config):
#     if not folder.exists():
#         return
#
#     for graph_file in folder.glob(f"{config.symbol}_*.png"):
#         graph_file.unlink()
#
#
# def plot_all_trade_days(data, config):
#     plot_trade_days_to_folder(data, config.output_folder, config)
#
#
# def plot_trade_days_to_folder(data, output_folder, config):
#     if config.clear_existing_graphs:
#         clear_graph_files(output_folder, config)
#
#     for trading_date in data["date"].unique():
#         plot_trade_day(
#             data=data,
#             selected_date=trading_date,
#             config=config,
#             output_folder=output_folder,
#             show=config.show_plots,
#         )
#
#
# def filter_by_date_range(data, start_date=None, end_date=None):
#     filtered_data = data.copy()
#
#     if start_date is not None:
#         filtered_data = filtered_data[filtered_data["date"] >= start_date]
#
#     if end_date is not None:
#         filtered_data = filtered_data[filtered_data["date"] <= end_date]
#
#     return filtered_data
#
#
# def plot_summary_trade_days(data, config):
#     summary_data = filter_by_date_range(
#         data,
#         start_date=config.summary_start,
#         end_date=config.summary_end,
#     )
#     plot_trade_days_to_folder(summary_data, config.summary_graph_folder, config)
#
#
# def copy_trade_days_to_folder(dates, output_folder, config, source_folder=None):
#     output_folder.mkdir(parents=True, exist_ok=True)
#     source_folder = source_folder or config.output_folder
#
#     if config.clear_existing_graphs:
#         clear_graph_files(output_folder, config)
#
#     for trading_date in dates:
#         file_name = f"{config.symbol}_{trading_date}.png"
#         source_path = source_folder / file_name
#         output_path = output_folder / file_name
#
#         if source_path.exists():
#             shutil.copy2(source_path, output_path)
#         else:
#             print("Missing graph:", source_path)
#
#
# def negative_return_dates(data):
#     negative_trades = data[
#         data["first_sell_signal"] & (data["trade_return"] < 0)
#     ]
#     return negative_trades["date"].unique()
#
#
# def non_executed_setup_dates(data):
#     setup_days = data.groupby("date")["setup_signal"].any()
#     buy_days = data.groupby("date")["buy_signal"].any()
#     non_executed_days = setup_days & ~buy_days
#     return non_executed_days[non_executed_days].index
#
#
# def safe_pct(numerator, denominator):
#     if denominator == 0:
#         return np.nan
#     return numerator / denominator * 100
#
#
# def summarize_performance(data, start_date=None, end_date=None):
#     data = data.copy()
#
#     if start_date is not None:
#         data = data[data["date"] >= start_date]
#
#     if end_date is not None:
#         data = data[data["date"] <= end_date]
#
#     buy_days = data.groupby("date")["buy_signal"].any()
#     sell_days = data.groupby("date")["sell_signal"].any()
#     successful = buy_days & sell_days
#     returns = data["trade_return"].dropna()
#
#     total_days = data["date"].nunique()
#     total_buys = buy_days.sum()
#     total_success = successful.sum()
#     negative_returns = (returns < 0).sum()
#     positive_returns = (returns > 0).sum()
#
#     return {
#         "total_days": total_days,
#         "buy_days": total_buys,
#         "successful_days": total_success,
#         "execution_rate": safe_pct(total_success, total_days),
#         "sell_hit_rate": safe_pct(total_success, total_buys),
#         "win_rate": safe_pct(positive_returns, len(returns)),
#         "negative_rate": safe_pct(negative_returns, len(returns)),
#         "average_return": returns.mean() * 100,
#         "median_return": returns.median() * 100,
#         "average_win": returns[returns > 0].mean() * 100,
#         "average_loss": returns[returns < 0].mean() * 100,
#         "best_trade": returns.max() * 100,
#         "worst_trade": returns.min() * 100,
#     }
#
#
# def format_pct(value, digits=2):
#     if pd.isna(value):
#         return "n/a"
#     return f"{round(value, digits)} %"
#
#
# def print_summary(summary):
#     print("Total days:", summary["total_days"])
#     print("Buy days:", summary["buy_days"])
#     print("Successful days:", summary["successful_days"])
#
#     print("\nExecution rate:", format_pct(summary["execution_rate"]))
#     print("Sell hit rate:", format_pct(summary["sell_hit_rate"]))
#     print("Win rate:", format_pct(summary["win_rate"]))
#     print("Negative rate:", format_pct(summary["negative_rate"]))
#
#     print("\nAverage return:", format_pct(summary["average_return"], digits=3))
#     print("Median return:", format_pct(summary["median_return"], digits=3))
#     print("Average win:", format_pct(summary["average_win"], digits=3))
#     print("Average loss:", format_pct(summary["average_loss"], digits=3))
#     print("Best trade:", format_pct(summary["best_trade"], digits=3))
#     print("Worst trade:", format_pct(summary["worst_trade"], digits=3))
#
#
# def load_quote_check_log(path):
#     if not path.exists():
#         print("\nQuote check summary skipped. Run quote_spread_check.py first.")
#         return None
#
#     quote_log = pd.read_csv(path)
#     required_columns = {
#         "date",
#         "quote_based_return",
#         "buy_price_available_in_window",
#         "sell_price_available_in_window",
#         "estimated_slippage_amount",
#         "estimated_shares",
#         "buy_visible_ask_shares",
#         "sell_visible_bid_shares",
#     }
#     missing_columns = required_columns - set(quote_log.columns)
#
#     if missing_columns:
#         print(
#             "\nQuote check summary skipped. Missing columns:",
#             ", ".join(sorted(missing_columns)),
#         )
#         return None
#
#     quote_log["date"] = pd.to_datetime(quote_log["date"]).dt.date
#     return quote_log
#
#
# def visible_share_coverage_pct(quote_trades, visible_shares_column):
#     required_columns = {"estimated_shares", visible_shares_column}
#
#     if not required_columns.issubset(quote_trades.columns):
#         return np.nan
#
#     valid_trades = quote_trades[
#         quote_trades["estimated_shares"].notna()
#         & quote_trades[visible_shares_column].notna()
#         & (quote_trades["estimated_shares"] > 0)
#     ]
#
#     if valid_trades.empty:
#         return np.nan
#
#     covered_shares = np.minimum(
#         valid_trades["estimated_shares"],
#         valid_trades[visible_shares_column],
#     ).sum()
#     return covered_shares / valid_trades["estimated_shares"].sum() * 100
#
#
# def truthy_series(values):
#     return values.astype(str).str.lower().eq("true")
#
#
# def summarize_quote_checks(quote_log, start_date, end_date):
#     quote_trades = quote_log[
#         (quote_log["date"] >= start_date)
#         & (quote_log["date"] <= end_date)
#     ]
#     quote_confirmed_trades = (
#         truthy_series(quote_trades["buy_price_available_in_window"])
#         & truthy_series(quote_trades["sell_price_available_in_window"])
#     ).sum()
#     buy_price_found_trades = truthy_series(
#         quote_trades["buy_price_available_in_window"]
#     ).sum()
#     sell_price_found_trades = truthy_series(
#         quote_trades["sell_price_available_in_window"]
#     ).sum()
#
#     return {
#         "buy_quote_price_found_rate": safe_pct(
#             buy_price_found_trades,
#             len(quote_trades),
#         ),
#         "sell_quote_price_found_rate": safe_pct(
#             sell_price_found_trades,
#             len(quote_trades),
#         ),
#         "quote_execution_rate": safe_pct(quote_confirmed_trades, len(quote_trades)),
#         "estimated_slippage_amount": quote_trades["estimated_slippage_amount"].sum(),
#         "buy_visible_share_coverage_pct": visible_share_coverage_pct(
#             quote_trades,
#             "buy_visible_ask_shares",
#         ),
#         "sell_visible_share_coverage_pct": visible_share_coverage_pct(
#             quote_trades,
#             "sell_visible_bid_shares",
#         ),
#     }
#
#
# def create_period_summary_table(
#     data,
#     start_date,
#     end_date,
#     months,
#     trade_log=None,
#     config=None,
#     quote_log=None,
# ):
#     if months <= 0:
#         raise ValueError("summary_period_months must be greater than 0.")
#
#     rows = []
#     period_start = pd.Timestamp(start_date)
#     final_date = pd.Timestamp(end_date)
#     running_money = config.starting_money if config is not None else None
#
#     while period_start <= final_date:
#         period_end = period_start + pd.DateOffset(months=months) - pd.Timedelta(days=1)
#         period_end = min(period_end, final_date)
#
#         summary = summarize_performance(
#             data,
#             start_date=period_start.date(),
#             end_date=period_end.date(),
#         )
#         if trade_log is not None and config is not None:
#             period_trades = trade_log[
#                 (trade_log["date"] >= period_start.date())
#                 & (trade_log["date"] <= period_end.date())
#                 & trade_log["net_pnl"].notna()
#             ]
#             net_pnl = period_trades["net_pnl"].sum()
#             ending_money = running_money + net_pnl
#
#             summary["starting_money"] = running_money
#             summary["net_pnl"] = net_pnl
#             summary["ending_money"] = ending_money
#             summary["total_trading_cost"] = period_trades["trading_cost"].sum()
#
#             running_money = ending_money
#
#         if quote_log is not None:
#             summary.update(
#                 summarize_quote_checks(
#                     quote_log,
#                     start_date=period_start.date(),
#                     end_date=period_end.date(),
#                 )
#             )
#
#         rows.append(
#             {
#                 "period": f"{period_start.date()} to {period_end.date()}",
#                 **summary,
#             }
#         )
#
#         period_start = period_end + pd.Timedelta(days=1)
#
#     return pd.DataFrame(rows)
#
#
# def print_period_summary_table(summary_table):
#     display_table = summary_table.copy()
#     pct_columns = [
#         "execution_rate",
#         "sell_hit_rate",
#         "win_rate",
#         "negative_rate",
#         "average_return",
#         "median_return",
#         "average_win",
#         "average_loss",
#         "best_trade",
#         "worst_trade",
#         "buy_quote_price_found_rate",
#         "sell_quote_price_found_rate",
#         "quote_execution_rate",
#         "buy_visible_share_coverage_pct",
#         "sell_visible_share_coverage_pct",
#     ]
#
#     for column in pct_columns:
#         if column in display_table.columns:
#             display_table[column] = display_table[column].round(3)
#
#     if "estimated_slippage_amount" in display_table.columns:
#         display_table["estimated_slippage_amount"] = display_table[
#             "estimated_slippage_amount"
#         ].round(2)
#
#     print("\nPeriod summary:")
#     print(display_table.to_string(index=False))
#
#
# def main():
#     config = StrategyConfig()
#     regular_data = run_strategy(main_run_config(config))
#     trade_log = create_trade_log(
#         regular_data,
#         start_date=config.summary_start,
#         end_date=config.summary_end,
#     )
#
#     if config.simulate_real_trading:
#         trade_log = add_real_trading_simulation(
#             trade_log,
#             config,
#             start_date=config.summary_start,
#             end_date=config.summary_end,
#         )
#
#     if config.update_graphs:
#         summary_data = filter_by_date_range(
#             regular_data,
#             start_date=config.summary_start,
#             end_date=config.summary_end,
#         )
#
#         if config.plot_all_dates:
#             plot_all_trade_days(regular_data, config)
#
#         if config.plot_summary_dates or config.plot_filtered_folders:
#             plot_summary_trade_days(summary_data, config)
#
#         if config.plot_filtered_folders:
#             copy_trade_days_to_folder(
#                 dates=negative_return_dates(summary_data),
#                 output_folder=config.negative_return_folder,
#                 config=config,
#                 source_folder=config.summary_graph_folder,
#             )
#
#             copy_trade_days_to_folder(
#                 dates=non_executed_setup_dates(summary_data),
#                 output_folder=config.non_executed_setup_folder,
#                 config=config,
#                 source_folder=config.summary_graph_folder,
#             )
#
#     if config.save_trade_log:
#         save_trade_log(trade_log, config)
#
#     summary = summarize_performance(
#         regular_data,
#         start_date=config.summary_start,
#         end_date=config.summary_end,
#     )
#     print_summary(summary)
#
#     if config.simulate_real_trading:
#         print_real_trading_summary(
#             summarize_real_trading(
#                 trade_log,
#                 config,
#                 start_date=config.summary_start,
#                 end_date=config.summary_end,
#             )
#         )
#
#     if config.print_period_summary:
#         quote_log = None
#
#         if config.include_quote_check_in_summary:
#             quote_log = load_quote_check_log(config.quote_check_path)
#
#         period_summary = create_period_summary_table(
#             regular_data,
#             start_date=config.summary_start,
#             end_date=config.summary_end,
#             months=config.summary_period_months,
#             trade_log=trade_log if config.simulate_real_trading else None,
#             config=config if config.simulate_real_trading else None,
#             quote_log=quote_log,
#         )
#         print_period_summary_table(period_summary)
#
#
# trade_log = load_trade_log()
# print(trade_log)
#
#
# if __name__ == "__main__":
#     main()
