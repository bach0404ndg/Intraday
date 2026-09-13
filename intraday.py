from dataclasses import dataclass
from datetime import time
from pathlib import Path
import os
import shutil
import numpy as np
import pandas as pd

import threading
import time as time_module

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper


os.environ.setdefault(
    "MPLCONFIGDIR",
    str(Path(".matplotlib-cache"))
)

ENV_FILE = Path(".env")

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)


# ============================================================
# CONFIG
# ============================================================

@dataclass(frozen=True)
class StrategyConfig:
    symbol: str = "APLD"
    start: str = "2026-05-01"
    end: str = "2026-08-30"
    timezone: str = "America/New_York"
    bar_minutes: int = 5

    # Buy
    buy_regression_bars: int = 7
    body_average_bars: int = 7
    doji_body_range_ratio: float = 0.1
    latest_buy_time: time = time(13, 0)

    #Avoid Bad day
    avoid_downstream_buy: bool = True
    downstream_decline_pct: float = 0.08
    downstream_trend_r2_min: float = 0.5

    # Buy Winning
    use_buy_winning: bool = True
    buy_winning_pct: float = 0.05
    earliest_buy_winning_time = time(9, 45)
    latest_buy_winning_time: time = time(10, 30)

    # Sell
    near_high_pct: float = 0.001
    near_high_bars: int = 3
    sell_regression_bars: int = 12
    sell_regression_bars_winning: int = 6
    sell_slope_slowdown_pct: float = 0

    use_early_take_profit: bool = False
    early_take_profit_pct: float = 0.03

    use_stop_loss: bool = True
    support_bars = 20
    stop_loss_pct: float = 0.01
    support_break_pct: float = 0.005
    winning_stop_loss_from_high_pct: float = 0.01
    stop_loss_bars = 3
    force_exit_time: time = time(15, 00)

    # Trading simulation
    starting_money: float = 10000.0
    money_per_trade: float = 10000.0
    trading_cost: float = 2.0

    # Output
    output_folder: Path = Path("trade graph")
    save_negative_graphs: bool = True
    negative_graph_folder: Path = Path("negative trade graph")
    save_non_traded_graphs: bool = True
    non_traded_graph_folder: Path = Path("non traded graph")
    trade_log_path: Path = Path("trade_log.csv")
    clear_old_graphs: bool = True


# ============================================================
# IBKR
# ============================================================

def load_env_file(path=ENV_FILE):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)

        os.environ.setdefault(
            key.strip(),
            value.strip().strip("\"'")
        )


IBKR_INFO_CODES = {
    2104,
    2106,
    2107,
    2108,
    2158,
}


def stock_contract(config):
    contract = Contract()
    contract.symbol = config.symbol
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract


class IBKRHistoricalClient(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)

        self.ready = threading.Event()
        self.request_done = threading.Event()

        self.bars = {}
        self.messages = []

    def nextValidId(self, orderId):
        self.ready.set()

    def historicalData(self, reqId, bar):
        self.bars.setdefault(reqId, []).append({
            "timestamp": pd.to_datetime(
                int(bar.date),
                unit="s",
                utc=True,
            ),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        })

    def historicalDataEnd(
        self,
        reqId,
        start,
        end,
    ):
        self.request_done.set()

    def error(
        self,
        reqId,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        self.messages.append(
            (
                reqId,
                errorCode,
                errorString,
            )
        )

        if errorCode not in IBKR_INFO_CODES:
            self.request_done.set()


def fetch_bars(config):
    app = IBKRHistoricalClient()

    app.connect(
        "127.0.0.1",
        4002,
        clientId=5,
    )

    api_thread = threading.Thread(
        target=app.run,
        daemon=True,
    )

    api_thread.start()

    if not app.ready.wait(10):
        app.disconnect()
        raise RuntimeError(
            "IBKR Gateway connection was not ready."
        )

    start_date = pd.Timestamp(
        config.start,
        tz=config.timezone,
    )

    end_date = pd.Timestamp(
        config.end,
        tz=config.timezone,
    )

    all_bars = []

    request_id = 1
    cursor = end_date

    while cursor > start_date:
        app.request_done.clear()

        end_utc = (
            cursor
            .tz_convert("UTC")
            .strftime("%Y%m%d %H:%M:%S UTC")
        )

        app.reqHistoricalData(
            request_id,
            stock_contract(config),
            end_utc,
            "1 W",
            f"{config.bar_minutes} mins",
            "TRADES",
            1,
            2,
            False,
            [],
        )

        if not app.request_done.wait(30):
            app.disconnect()

            raise RuntimeError(
                f"IBKR historical request "
                f"{request_id} timed out."
            )

        bars = app.bars.get(
            request_id,
            [],
        )

        if bars:
            chunk = pd.DataFrame(bars)

            chunk["symbol"] = (
                config.symbol
            )

            all_bars.append(chunk)

            earliest_time = (
                chunk["timestamp"].min()
            )

            cursor = (
                earliest_time
                .tz_convert(config.timezone)
                - pd.Timedelta(seconds=1)
            )

        else:
            cursor -= pd.Timedelta(
                days=7
            )

        request_id += 1

        time_module.sleep(0.25)

    app.disconnect()

    if not all_bars:
        message_text = "; ".join(
            f"{code}: {message}"
            for _, code, message in app.messages
        )

        raise RuntimeError(
            "IBKR returned no historical bars for "
            f"{config.symbol} from {config.start} to {config.end}. "
            "Check that IB Gateway is open, paper API is enabled, "
            "the socket port is 4002, and your market data permission is active. "
            f"IBKR messages: {message_text or 'none'}"
        )

    data = pd.concat(
        all_bars,
        ignore_index=True,
    )

    start_utc = (
        start_date.tz_convert("UTC")
    )

    end_utc = (
        end_date.tz_convert("UTC")
    )

    data = data[
        (data["timestamp"] >= start_utc)
        & (data["timestamp"] < end_utc)
    ]

    return (
        data
        .drop_duplicates(
            subset=[
                "symbol",
                "timestamp",
            ]
        )
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

# ============================================================
# TIME / SESSION HELPERS
# ============================================================

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
        raise ValueError(
            f"Invalid session: {session}"
        )

    return data[
        data["session"] == session
    ].copy()


def clock_times(timestamps):
    return timestamps.map(
        lambda timestamp:
        timestamp.time()
        if pd.notna(timestamp)
        else np.nan
    )


def clock_time_before(timestamps, cutoff):
    return timestamps.map(
        lambda timestamp:
        pd.notna(timestamp)
        and timestamp.time() <= cutoff
    )


def clock_time_after (timestamps, cutoff):
    return timestamps.map(
        lambda timestamp:
        pd.notna(timestamp)
        and timestamp.time() >= cutoff
    )

# ============================================================
# BASE FEATURES
# ============================================================

def add_base_features(data, config):
    data = data.copy()

    data = (
        data
        .sort_values("timestamp")
        .reset_index(drop=True)
    )

    data["timestamp"] = (
        data["timestamp"]
        .dt.tz_convert(config.timezone)
    )

    data["session"] = (
        data["timestamp"]
        .apply(label_session)
    )

    data["date"] = (
        data["timestamp"]
        .dt.date
    )

    data["body"] = (
        data["close"] - data["open"]
    ).abs()

    data["range"] = (
        data["high"] - data["low"]
    )

    data["body_pct"] = (
        data["body"] / data["open"]
    )

    data["range_pct"] = (
        data["range"] / data["open"]
    )

    data["candle_color"] = np.where(
        data["close"] > data["open"],
        "green",
        np.where(
            data["close"] < data["open"],
            "red",
            "orange",
        ),
    )

    return data


# ============================================================
# GAP / INTRADAY FEATURES
# ============================================================

def add_gap_features(data, config):
    data = data.copy()

    clock_time = clock_times(
        data["timestamp"]
    )

    open_930 = (
        data[clock_time == time(9, 30)]
        .set_index("date")["open"]
    )

    close_4 = (
        data[clock_time == time(15, 55)]
        .set_index("date")["close"]
    )

    previous_close_4 = close_4.shift(1)

    opening_gap = (
        (open_930 - previous_close_4)
        / previous_close_4
    )

    data["opening_gap"] = (
        data["date"].map(opening_gap)
    )

    data["regular_open"] = (
        data["date"].map(open_930)
    )

    data["prev_close"] = (
        data["date"].map(previous_close_4)
    )

    data["current_gap"] = (
        (data["close"] - data["regular_open"])
        / data["regular_open"]
    )

    data["gap_change"] = (
        data.groupby("date")["current_gap"]
        .diff()
    )

    data["recent_low"] = (
        data.groupby("date")["low"]
        .transform(
            lambda x:
            x.shift(1).cummin()
        )
    )

    data["recent_high"] = (
        data.groupby("date")["high"]
        .transform(
            lambda x:
            x.shift(1).cummax()
        )
    )

    red_bar_lows = (
        data["low"]
        .where(data["candle_color"] == "red")
    )

    data["recent_support"] = (
        red_bar_lows.groupby(data["date"])
        .transform(
            lambda x: x.shift(1)
            .rolling(
                config.support_bars,
                min_periods=1,
            )
            .min()
        )
    )

    return data


# ============================================================
# REGRESSION HELPERS
# ============================================================

def price_slope_pct(values):
    time_index = np.arange(
        len(values)
    )

    slope = np.polyfit(
        time_index,
        np.log(values),
        1,
    )[0]

    return np.exp(slope) - 1


def price_r2(values):
    time_index = np.arange(
        len(values)
    )

    log_values = np.log(values)

    slope, intercept = np.polyfit(
        time_index,
        log_values,
        1,
    )

    fitted = (
        slope * time_index
        + intercept
    )

    residual_sum = np.sum(
        (log_values - fitted) ** 2
    )

    total_sum = np.sum(
        (log_values - np.mean(log_values)) ** 2
    )

    if total_sum == 0:
        return 0

    return 1 - residual_sum / total_sum


def rolling_slope_pct_by_day(
    data,
    column,
    bars,
):
    return (
        data.groupby("date")[column]
        .transform(
            lambda x:
            x.shift(1)
            .rolling(bars)
            .apply(
                price_slope_pct,
                raw=True,
            )
        )
    )


def expanding_regression_by_day(
    data,
    column,
    function,
):
    return (
        data.groupby("date")[column]
        .transform(
            lambda x:
            x.expanding(
                min_periods=2,
            )
            .apply(
                function,
                raw=True,
            )
        )
    )


# ============================================================
# BUY SIGNAL
# ============================================================

def add_buy_signals(data, config):
    data = data.copy()

    data["avg_range_pct"] = (
        data.groupby("date")["range_pct"]
        .transform(
            lambda x:
            x.shift(1)
            .rolling(
                config.body_average_bars
            )
            .mean()
        )
    )

    data["doji_body"] = (
        data["body_pct"]
        < config.doji_body_range_ratio
        * data["avg_range_pct"]
    )

    data["slope"] = (
        rolling_slope_pct_by_day(
            data,
            column="close",
            bars=config.buy_regression_bars,
        )
    )

    data["downstream_decline"] = (
        (
            data["regular_open"]
            - data["recent_low"]
        )
        / data["regular_open"]
    )

    data["downstream_trend_slope"] = (
        expanding_regression_by_day(
            data,
            column="close",
            function=price_slope_pct,
        )
    )

    data["downstream_trend_r2"] = (
        expanding_regression_by_day(
            data,
            column="close",
            function=price_r2,
        )
    )

    data["downstream_no_buy"] = (
        config.avoid_downstream_buy
        & (
            data["downstream_decline"]
            >= config.downstream_decline_pct
        )
        & (
            data["downstream_trend_slope"]
            < 0
        )
        & (
            data["downstream_trend_r2"]
            >= config.downstream_trend_r2_min
        )
    )

    by_day = data.groupby("date")

    data["confirmation_candle_color"] = (
        by_day["candle_color"]
        .shift(-1)
    )

    # ============================================================
    # NORMAL BUY EXECUTION: 2 BARS LATER
    # ============================================================

    data["execution_time"] = (
        by_day["timestamp"]
        .shift(-2)
    )

    data["execution_price"] = (
        by_day["open"]
        .shift(-2)
    )

    data["before_latest_buy_time"] = (
        clock_time_before(
            data["execution_time"],
            config.latest_buy_time,
        )
    )

    # ============================================================
    # WINNING BUY EXECUTION: NEXT OPEN
    # ============================================================

    data["winning_execution_time"] = (
        by_day["timestamp"]
        .shift(-1)
    )

    data["winning_execution_price"] = (
        by_day["open"]
        .shift(-1)
    )

    # ============================================================
    # NORMAL BUY
    # ============================================================

    data["setup_signal"] = (
            (data["slope"] < 0)
            & data["doji_body"]
            & (data["confirmation_candle_color"] == "green")
    )

    candidate_buy = (
            data["setup_signal"]
            & data["execution_price"].notna()
            & data["before_latest_buy_time"]
            & ~data["downstream_no_buy"]
    )

    # ============================================================
    # WINNING BUY
    # ============================================================
    if config.use_buy_winning:
        candidate_buy_winning = (
                (data["current_gap"] > config.buy_winning_pct)
                & data["winning_execution_price"].notna()
                & clock_time_after(
                data["winning_execution_time"],
                config.earliest_buy_winning_time)
                & clock_time_before(
            data["winning_execution_time"],
            config.latest_buy_winning_time
        )
                & ~data["downstream_no_buy"]
        )
    else:
        candidate_buy_winning = pd.Series(
            False,
            index=data.index,
        )

    # ============================================================
    # FIRST BUY OF EITHER TYPE
    # ============================================================

    candidate_buy_any = (
            candidate_buy
            | candidate_buy_winning
    )

    data["buy_signal"] = (
            candidate_buy_any
            & (
                    candidate_buy_any
                    .groupby(data["date"])
                    .cumsum()
                    == 1
            )
    )

    data["buy_winning_signal"] = (
            data["buy_signal"]
            & candidate_buy_winning
    )

    data["buy_normal_signal"] = (
            data["buy_signal"]
            & candidate_buy
            & ~data["buy_winning_signal"]
    )

    # ============================================================
    # BUY TYPE
    # winning gets priority if both conditions happen
    # on the same row
    # ============================================================

    data["buy_type"] = np.select(
        [
            data["buy_signal"]
            & candidate_buy_winning,

            data["buy_signal"]
            & candidate_buy,
        ],
        [
            "winning",
            "normal",
        ],
        default=None,
    )

    # ============================================================
    # BUY TIME / PRICE
    # ============================================================

    data["buy_time"] = (
        data["winning_execution_time"]
        .where(
            data["buy_type"] == "winning",
            data["execution_time"],
        )
        .where(data["buy_signal"])
    )

    data["buy_price"] = (
        data["winning_execution_price"]
        .where(
            data["buy_type"] == "winning",
            data["execution_price"],
        )
        .where(data["buy_signal"])
    )

    # ============================================================
    # PROPAGATE TRADE INFORMATION THROUGH THE DAY
    # ============================================================

    data["buy_time"] = (
        data["buy_time"]
        .groupby(data["date"])
        .transform("first")
    )

    data["buy_price"] = (
        data["buy_price"]
        .groupby(data["date"])
        .transform("first")
    )

    data["buy_type"] = (
        data["buy_type"]
        .groupby(data["date"])
        .transform("first")
    )

    return data


# ============================================================
# AFTER-BUY FEATURES
# ============================================================

def add_high_after_buy(data):
    data = data.copy()

    data["after_buy"] = (
        data["timestamp"]
        > data["buy_time"]
    )

    data["high_since_buy"] = (
        data["high"]
        .where(data["after_buy"])
        .groupby(data["date"])
        .cummax()
    )

    new_high_after_buy = (
        data["after_buy"]
        & (
            data["high"]
            == data["high_since_buy"]
        )
    )

    data["open_at_high_since_buy"] = (
        data["open"]
        .where(new_high_after_buy)
        .groupby(data["date"])
        .ffill()
    )

    data["previous_high"] = (
        data.groupby("date")["high"]
        .shift(1)
    )

    data["previous_high_since_buy"] = (
        data.groupby("date")[
            "high_since_buy"
        ]
        .shift(1)
    )

    data["previous_open_at_high_since_buy"] = (
        data.groupby("date")[
            "open_at_high_since_buy"
        ]
        .shift(1)
    )

    return data


# ============================================================
# SELL SIGNAL
# ============================================================
def add_sell_signals(data, config):
    data = data.copy()

    clock_time = clock_times(
        data["timestamp"]
    )

    # ============================================================
    # NORMAL SELL FEATURES
    # ============================================================

    data["recent_previous_high"] = (
        data.groupby("date")["previous_high"]
        .transform(
            lambda x:
            x.rolling(
                config.near_high_bars,
                min_periods=1,
            )
            .max()
        )
    )

    data["near_high"] = (
        data["recent_previous_high"]
        >= data["previous_high_since_buy"]
        * (1 - config.near_high_pct)
    )

    data["slope_sell_normal"] = (
        rolling_slope_pct_by_day(
            data,
            column="close",
            bars=config.sell_regression_bars,
        )
    )

    data["slope_sell_winning"] = (
        rolling_slope_pct_by_day(
            data,
            column="close",
            bars=config.sell_regression_bars_winning,
        )
    )

    data["slope_sell"] = (
        data["slope_sell_winning"]
        .where(
            data["buy_type"] == "winning",
            data["slope_sell_normal"],
        )
    )

    data["previous_slope_sell"] = (
        data.groupby("date")["slope_sell"]
        .shift(1)
    )

    data["slope_slowing_enough"] = (
        data["slope_sell"]
        < data["previous_slope_sell"]
        * (1 - config.sell_slope_slowdown_pct)
    )

    normal_regression_ready_time = (
        data["buy_time"]
        + pd.Timedelta(
            minutes=(
                config.bar_minutes
                * config.sell_regression_bars
            )
        )
    )

    winning_regression_ready_time = (
        data["buy_time"]
        + pd.Timedelta(
            minutes=(
                config.bar_minutes
                * config.sell_regression_bars_winning
            )
        )
    )

    regression_ready_time = (
        winning_regression_ready_time
        .where(
            data["buy_type"] == "winning",
            normal_regression_ready_time,
        )
    )

    data["sell_regression_ready"] = (
        data["timestamp"]
        >= regression_ready_time
    )

    # ============================================================
    # EARLY TAKE-PROFIT LIMIT ORDER
    # ============================================================

    early_take_profit_price = (
        data["buy_price"]
        * (1 + config.early_take_profit_pct)
    )

    if config.use_early_take_profit:
        early_take_profit_candidate = (
            data["buy_time"].notna()
            & (data["timestamp"] > data["buy_time"])
            & (data["low"] <= early_take_profit_price)
            & (data["high"] >= early_take_profit_price)
        )

        data["early_take_profit_signal"] = (
            early_take_profit_candidate
            & (
                early_take_profit_candidate
                .groupby(data["date"])
                .cumsum()
                == 1
            )
        )

    else:
        data["early_take_profit_signal"] = pd.Series(
            False,
            index=data.index,
        )

    early_already_sold = (
        data["early_take_profit_signal"]
        .groupby(data["date"])
        .cumsum()
        > 0
    )

    # ============================================================
    # STOP LOSS
    # ============================================================

    data["support_level"] = (
        data["recent_support"]
        .where(data["buy_signal"])
        .groupby(data["date"])
        .transform("first")
    )

    stop_loss_price = (
        data["buy_price"]
        * (1 - config.stop_loss_pct)
    )

    support_break_price = (
        data["support_level"]
        * (1 - config.support_break_pct)
    )

    winning_stop_loss_price = (
        data["previous_open_at_high_since_buy"]
        * (1 - config.winning_stop_loss_from_high_pct)
    )

    data["active_stop_loss_price"] = (
        winning_stop_loss_price
        .where(
            data["buy_type"] == "winning",
            stop_loss_price,
        )
    )

    if config.use_stop_loss:
        stop_loss_ready = (
                data["buy_time"].notna()
                & (data["timestamp"] > data["buy_time"]
                   + pd.Timedelta(
                    minutes=config.bar_minutes * config.stop_loss_bars))
                & ~early_already_sold
        )

        normal_stop_loss_candidate = (
                stop_loss_ready
                & (data["buy_type"] != "winning")
                & (data["low"] <= stop_loss_price)
                & (
                    data["support_level"].isna()
                    | (data["low"] <= support_break_price)
                )
        )

        winning_stop_loss_candidate = (
                stop_loss_ready
                & (data["buy_type"] == "winning")
                & data["previous_open_at_high_since_buy"].notna()
                & (data["low"] <= winning_stop_loss_price)
        )

        stop_loss_candidate = (
            normal_stop_loss_candidate
            | winning_stop_loss_candidate
        )

        data["stop_loss_signal"] = (
            stop_loss_candidate
            & (
                stop_loss_candidate
                .groupby(data["date"])
                .cumsum()
                == 1
            )
        )

    else:
        data["stop_loss_signal"] = pd.Series(
            False,
            index=data.index,
        )

    stop_already_sold = (
        data["stop_loss_signal"]
        .groupby(data["date"])
        .cumsum()
        > 0
    )

    # ============================================================
    # NORMAL REGRESSION SELL
    # ============================================================

    candidate_sell = (
        data["sell_regression_ready"]
        & (data["slope_sell"] > 0)
        & data["slope_slowing_enough"]
        & data["near_high"]
        & ~early_already_sold
        & ~stop_already_sold
    )

    data["profit_sell_signal"] = (
        candidate_sell
        & (
            candidate_sell
            .groupby(data["date"])
            .cumsum()
            == 1
        )
    )

    # ============================================================
    # CHECK WHETHER POSITION HAS ALREADY BEEN SOLD
    # ============================================================

    already_sold = (
        (
            data["early_take_profit_signal"]
            | data["stop_loss_signal"]
            | data["profit_sell_signal"]
        )
        .groupby(data["date"])
        .cumsum()
        > 0
    )

    # ============================================================
    # TIME FORCE EXIT
    # ============================================================

    data["force_exit"] = (
        data["buy_time"].notna()
        & (clock_time == config.force_exit_time)
        & ~already_sold
    )

    # ============================================================
    # FINAL SELL SIGNAL
    # ============================================================

    sell_candidate = (
        data["early_take_profit_signal"]
        | data["stop_loss_signal"]
        | data["profit_sell_signal"]
        | data["force_exit"]
    )

    # Extra safeguard: exactly one exit row per trading day.
    data["sell_signal"] = (
        sell_candidate
        & (
            sell_candidate
            .groupby(data["date"])
            .cumsum()
            == 1
        )
    )

    # Normal regression sell / time force exit:
    # sell at the current candle open.
    data["sell_price"] = (
        data["open"]
        .where(data["sell_signal"])
    )

    # Early take-profit:
    # fill exactly at the standing limit price.
    data.loc[
        data["early_take_profit_signal"]
        & data["sell_signal"],
        "sell_price"
    ] = early_take_profit_price[
        data["early_take_profit_signal"]
        & data["sell_signal"]
    ]

    # Stop loss:
    # fill at the stop price unless the candle opened below it.
    data.loc[
        data["stop_loss_signal"]
        & data["sell_signal"],
        "sell_price"
    ] = np.minimum(
        data["open"],
        data["active_stop_loss_price"],
    )[
        data["stop_loss_signal"]
        & data["sell_signal"]
    ]

    data["sell_time"] = (
        data["timestamp"]
        .where(data["sell_signal"])
    )

    return data


# ============================================================
# RETURNS
# ============================================================

def add_trade_returns(data):
    data = data.copy()

    data["daily_high_after_buy"] = (
        data["high_since_buy"]
        .groupby(data["date"])
        .transform("max")
    )

    data["max_return_pct"] = (
        (
            data["daily_high_after_buy"]
            - data["buy_price"]
        )
        / data["buy_price"]
    )

    data["trade_return_pct"] = (
        (
            data["sell_price"]
            - data["buy_price"]
        )
        / data["buy_price"]
    ).where(
        data["sell_signal"]
    )

    return data


# ============================================================
# MAIN DATA PIPELINE
# ============================================================

def run_strategy_on_data(data, config):
    if data.empty:
        raise RuntimeError(
            "No bar data was loaded. Check the data connection "
            "and the configured date range."
        )

    data = add_base_features(
        data,
        config,
    )

    data = get_session_data(
        data,
        "regular",
    )

    data = add_gap_features(
        data,
        config,
    )

    data = add_buy_signals(
        data,
        config,
    )

    data = add_high_after_buy(data)

    data = add_sell_signals(
        data,
        config,
    )

    data = add_trade_returns(data)

    return data


def load_data(config):
    data = fetch_bars(config)

    return run_strategy_on_data(
        data,
        config,
    )


# ============================================================
# TRADE LOG
# ============================================================

def create_trade_log(
    data,
    start_date=None,
    end_date=None,
):
    data = data.copy()

    if start_date is not None:
        data = data[
            data["date"] >= start_date
        ]

    if end_date is not None:
        data = data[
            data["date"] <= end_date
        ]

    trade_rows = []

    for trading_date, day_data in data.groupby("date"):

        buy_rows = day_data[
            day_data["buy_signal"]
        ]

        if buy_rows.empty:
            continue

        buy_row = buy_rows.iloc[0]

        sell_rows = day_data[
            day_data["sell_signal"]
        ]

        if sell_rows.empty:
            sell_time = pd.NaT
            sell_price = np.nan
            trade_return_pct = np.nan
            slope_sell = np.nan
            previous_slope_sell = np.nan
            near_high = np.nan
            active_stop_loss_price = np.nan
            sell_type = "no_sell"

        else:
            sell_row = sell_rows.iloc[0]

            sell_time = sell_row["sell_time"]
            sell_price = sell_row["sell_price"]
            trade_return_pct = sell_row["trade_return_pct"]

            slope_sell = sell_row["slope_sell"]
            previous_slope_sell = (
                sell_row["previous_slope_sell"]
            )
            near_high = sell_row["near_high"]
            active_stop_loss_price = sell_row["active_stop_loss_price"]

            if sell_row["early_take_profit_signal"]:
                sell_type = "early_take_profit"

            elif sell_row["stop_loss_signal"]:
                sell_type = "stop_loss"

            elif sell_row["force_exit"]:
                sell_type = "force_exit"

            else:
                sell_type = "profit"

        # Candles held after entry and before the exit candle.
        # This avoids using the low of a normal/forced exit candle,
        # because those exits happen at that candle's open.
        if pd.isna(sell_time):
            holding_data = day_data[
                day_data["timestamp"] >= buy_row["buy_time"]
            ]
        else:
            holding_data = day_data[
                (day_data["timestamp"] >= buy_row["buy_time"])
                & (day_data["timestamp"] < sell_time)
            ]

        pct_decrease_list = (
            (
                buy_row["buy_price"]
                - holding_data["low"]
            )
            / buy_row["buy_price"]
        ).tolist()

        # For a stop-loss exit, the exit candle itself is what touched
        # the threshold. Record that known adverse move as the last value
        # without using the candle's later full low.
        if (
            not sell_rows.empty
            and sell_row["stop_loss_signal"]
        ):
            pct_decrease_list.append(
                (
                    buy_row["buy_price"]
                    - sell_price
                )
                / buy_row["buy_price"]
            )

        trade_rows.append({
            "date": trading_date,

            # Trade
            "buy_type": buy_row["buy_type"],
            "buy_time": buy_row["buy_time"],
            "buy_price": buy_row["buy_price"],
            "sell_time": sell_time,
            "sell_price": sell_price,
            "sell_type": sell_type,

            # Outcomes
            "trade_return_pct": trade_return_pct,
            "max_return_pct": buy_row["max_return_pct"],
            "pct_decrease_list": pct_decrease_list,

            # Buy/setup features
            "buy_slope": buy_row["slope"],
            "body_pct": buy_row["body_pct"],
            "range_pct": buy_row["range_pct"],
            "opening_gap": buy_row["opening_gap"],
            "current_gap": buy_row["current_gap"],
            "gap_change": buy_row["gap_change"],
            "recent_low": buy_row["recent_low"],
            "recent_high": buy_row["recent_high"],
            "downstream_decline": buy_row["downstream_decline"],
            "downstream_trend_slope": buy_row["downstream_trend_slope"],
            "downstream_trend_r2": buy_row["downstream_trend_r2"],
            "downstream_no_buy": buy_row["downstream_no_buy"],
            "support_level": buy_row["support_level"],
            "active_stop_loss_price": active_stop_loss_price,

            # Sell features
            "slope_sell": slope_sell,
            "previous_slope_sell": previous_slope_sell,
            "near_high": near_high,
        })

    return pd.DataFrame(trade_rows)



# ============================================================
# TRADING SIMULATION
# ============================================================

def trading_simulation(
    trade_log,
    config,
    start_date=None,
    end_date=None,
):
    trade_log = trade_log.copy()

    if start_date is not None:
        trade_log = trade_log[
            trade_log["date"] >= start_date
        ]

    if end_date is not None:
        trade_log = trade_log[
            trade_log["date"] <= end_date
        ]

    current_money = (
        config.starting_money
    )

    for row_index, row in trade_log.iterrows():

        trade_log.at[
            row_index,
            "starting_money"
        ] = current_money

        if (
            current_money <= 0
            or pd.isna(row["buy_price"])
        ):
            trade_log.at[
                row_index,
                "ending_money"
            ] = current_money

            continue

        available_money = min(
            config.money_per_trade,
            current_money,
        )

        shares = int(
            available_money
            / row["buy_price"]
        )

        if shares <= 0:
            trade_log.at[
                row_index,
                "ending_money"
            ] = current_money

            continue

        money_used = (
            shares
            * row["buy_price"]
        )

        trade_log.at[
            row_index,
            "shares"
        ] = shares

        trade_log.at[
            row_index,
            "money_used"
        ] = money_used

        if pd.isna(
            row["trade_return_pct"]
        ):
            trade_log.at[
                row_index,
                "ending_money"
            ] = current_money

            continue

        gross_pnl = (
            money_used
            * row["trade_return_pct"]
        )

        net_pnl = (
            gross_pnl
            - config.trading_cost
        )

        current_money += net_pnl

        trade_log.at[
            row_index,
            "gross_pnl"
        ] = gross_pnl

        trade_log.at[
            row_index,
            "trading_cost"
        ] = config.trading_cost

        trade_log.at[
            row_index,
            "net_pnl"
        ] = net_pnl

        trade_log.at[
            row_index,
            "ending_money"
        ] = current_money

    return trade_log


# ============================================================
# SUMMARY
# ============================================================
def summarize_trading(
    trade_log,
    data,
    config,
    start_date=None,
    end_date=None,
):
    trade_log = trade_log.copy()
    data = data.copy()

    if start_date is not None:
        trade_log = trade_log[
            trade_log["date"] >= start_date
        ]
        data = data[
            data["date"] >= start_date
        ]

    if end_date is not None:
        trade_log = trade_log[
            trade_log["date"] <= end_date
        ]
        data = data[
            data["date"] <= end_date
        ]

    completed_trades = trade_log[
        trade_log["trade_return_pct"].notna()
    ]

    total_days = data["date"].nunique()
    completed_count = len(completed_trades)

    win_days = (
        completed_trades["trade_return_pct"] > 0
    ).sum()

    negative_days = (
        completed_trades["trade_return_pct"] < 0
    ).sum()

    if completed_trades.empty:
        return {
            "starting_money": config.starting_money,
            "ending_money": config.starting_money,
            "total_net_pnl": 0.0,
            "net_return_pct": 0.0,

            "median_return_pct": np.nan,

            "completed_trades": 0,
            "total_days": total_days,
            "completed_trade_rate": 0.0,

            "win_days": 0,
            "win_day_rate": 0.0,

            "negative_days": 0,
            "negative_day_rate": 0.0,

            "best_win_pct": np.nan,
            "best_loss_pct": np.nan,

            "total_trading_cost": 0.0,
        }

    starting_money = (
        completed_trades["starting_money"]
        .iloc[0]
    )

    ending_money = (
        completed_trades["ending_money"]
        .iloc[-1]
    )

    total_net_pnl = (
        completed_trades["net_pnl"]
        .sum()
    )

    total_trading_cost = (
        completed_trades["trading_cost"]
        .sum()
    )

    median_return_pct = (
        completed_trades["trade_return_pct"]
        .median()
    )

    best_win_pct = (
        completed_trades["trade_return_pct"]
        .max()
    )

    best_loss_pct = (
        completed_trades["trade_return_pct"]
        .min()
    )

    return {
        "starting_money": starting_money,
        "ending_money": ending_money,

        "total_net_pnl": total_net_pnl,

        "net_return_pct": (
            ending_money - starting_money
        ) / starting_money,

        "median_return_pct": median_return_pct,

        "completed_trades": completed_count,
        "total_days": total_days,

        "completed_trade_rate": (
            completed_count / total_days
            if total_days > 0
            else 0.0
        ),

        "win_days": win_days,

        "win_day_rate": (
            win_days / total_days
            if total_days > 0
            else 0.0
        ),

        "negative_days": negative_days,

        "negative_day_rate": (
            negative_days / total_days
            if total_days > 0
            else 0.0
        ),

        "best_win_pct": best_win_pct,
        "best_loss_pct": best_loss_pct,

        "total_trading_cost": total_trading_cost,
    }


def print_trading_summary(summary):
    print("\n===== TRADING SUMMARY =====")

    print(
        f"Starting money:      "
        f"${summary['starting_money']:,.2f}"
    )

    print(
        f"Ending money:        "
        f"${summary['ending_money']:,.2f}"
    )

    print(
        f"Net P&L:             "
        f"${summary['total_net_pnl']:,.2f}"
    )

    print(
        f"Net return:          "
        f"{summary['net_return_pct']:.2%}"
    )

    print(
        f"Median trade return: "
        f"{summary['median_return_pct']:.2%}"
    )

    print(
        f"Completed trades:    "
        f"{summary['completed_trades']} / "
        f"{summary['total_days']} "
        f"({summary['completed_trade_rate']:.2%})"
    )

    print(
        f"Win days:            "
        f"{summary['win_days']} / "
        f"{summary['total_days']} "
        f"({summary['win_day_rate']:.2%})"
    )

    print(
        f"Negative days:       "
        f"{summary['negative_days']} / "
        f"{summary['total_days']} "
        f"({summary['negative_day_rate']:.2%})"
    )

    print(
        f"Best win:            "
        f"{summary['best_win_pct']:.2%}"
    )

    print(
        f"Best loss:           "
        f"{summary['best_loss_pct']:.2%}"
    )

    print(
        f"Trading costs:       "
        f"${summary['total_trading_cost']:,.2f}"
    )


# ============================================================
# SAVE TRADE LOG
# ============================================================

def save_trade_log(
    trade_log,
    config,
):
    trade_log.to_csv(
        config.trade_log_path,
        index=False,
    )


# ============================================================
# PLOT TRADE DAY
# ============================================================

def plot_trade_day(
    data,
    selected_date,
    config,
    output_folder=None,
    show=True,
):
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    from matplotlib.patches import Rectangle

    day_data = data[
        data["date"].astype(str)
        == str(selected_date)
    ].copy()

    if day_data.empty:
        print(
            "No data for this date."
        )
        return

    output_folder = (
        output_folder
        or config.output_folder
    )

    output_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    _, ax = plt.subplots(
        figsize=(20, 12)
    )

    # Candles
    for _, row in day_data.iterrows():

        x = mdates.date2num(
            row["timestamp"]
        )

        color = row["candle_color"]

        ax.plot(
            [x, x],
            [
                row["low"],
                row["high"],
            ],
            color=color,
            linewidth=1,
        )

        body_bottom = min(
            row["open"],
            row["close"],
        )

        body_height = max(
            row["body"],
            0.001,
        )

        ax.add_patch(
            Rectangle(
                (
                    x - 0.0012,
                    body_bottom,
                ),
                0.0024,
                body_height,
                facecolor=color,
                edgecolor=color,
            )
        )

    # Buy
    for _, row in day_data[
        day_data["buy_signal"]
    ].iterrows():

        buy_time = row["buy_time"]
        buy_price = row["buy_price"]

        if row["buy_type"] == "winning":
            buy_color = "purple"
        else:
            buy_color = "black"

        buy_candle_low = (
            day_data.loc[
                day_data["timestamp"]
                == buy_time,
                "low",
            ]
            .iloc[0]
        )

        ax.scatter(
            buy_time,
            buy_candle_low - 0.05,
            marker="^",
            s=150,
            zorder=5,
            color=buy_color,
        )

        ax.annotate(
            (
                f"{buy_time.strftime('%H:%M')}\n"
                f"${buy_price:.2f}"
            ),
            (
                buy_time,
                buy_candle_low - 0.05,
            ),
            xytext=(0, -10),
            textcoords="offset points",
            ha="center",
            va="top",
        )

        high_after_buy_price = (
            row["daily_high_after_buy"]
        )

        if pd.notna(
            high_after_buy_price
        ):
            high_after_buy_time = (
                day_data.loc[
                    (
                        day_data["timestamp"]
                        >= buy_time
                    )
                    & (
                        day_data["high"]
                        == high_after_buy_price
                    ),
                    "timestamp",
                ]
                .iloc[0]
            )

            ax.scatter(
                high_after_buy_time,
                high_after_buy_price + 0.05,
                marker="v",
                s=150,
                zorder=5,
                color="black",
            )

            ax.annotate(
                (
                    f"{high_after_buy_time.strftime('%H:%M')}\n"
                    f"${high_after_buy_price:.2f}"
                ),
                (
                    high_after_buy_time,
                    high_after_buy_price + 0.05,
                ),
                xytext=(0, 10),
                textcoords="offset points",
                ha="center",
                va="bottom",
            )

    # Sell
    for _, row in day_data[
        day_data["sell_signal"]
    ].iterrows():

        sell_time = row["sell_time"]
        sell_price = row["sell_price"]

        if row["early_take_profit_signal"]:
            sell_color = "green"
        elif row["stop_loss_signal"]:
            sell_color = "red"
        elif row["force_exit"]:
            sell_color = "orange"
        else:
            sell_color = "blue"

        sell_candle_high = (
            day_data.loc[
                day_data["timestamp"]
                == sell_time,
                "high",
            ]
            .iloc[0]
        )

        ax.scatter(
            sell_time,
            sell_candle_high + 0.05,
            marker="v",
            s=150,
            zorder=5,
            color=sell_color,
        )

        ax.annotate(
            (
                f"{sell_time.strftime('%H:%M')}\n"
                f"${sell_price:.2f}"
            ),
            (
                sell_time,
                sell_candle_high + 0.05,
            ),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            va="bottom",
        )

    ax.xaxis.set_major_formatter(
        mdates.DateFormatter(
            "%H:%M",
            tz=config.timezone,
        )
    )

    ax.xaxis.set_major_locator(
        mdates.MinuteLocator(
            byminute=[0, 30],
            tz=config.timezone,
        )
    )

    plt.xticks(
        rotation=45
    )

    ax.set_title(
        f"{config.symbol} {selected_date}"
    )

    ax.set_xlabel("Time")
    ax.set_ylabel("Price")

    plt.tight_layout()

    output_path = (
        output_folder
        / (
            f"{config.symbol}_"
            f"{selected_date}.png"
        )
    )

    plt.savefig(
        output_path,
        dpi=150,
        bbox_inches="tight",
    )

    if show:
        plt.show()
    else:
        plt.close()


def plot_all_trade_days(data, config, show=False):

    if config.clear_old_graphs and config.output_folder.exists():
        for graph_path in config.output_folder.glob(
                f"{config.symbol}_*.png"
        ):
            graph_path.unlink()

    if config.save_non_traded_graphs:
        trade_dates = (
            data["date"]
            .unique()
        )
    else:
        trade_dates = data.loc[
            data["buy_signal"],
            "date"
        ].unique()

    for trade_date in trade_dates:
        plot_trade_day(
            data,
            trade_date,
            config,
            show=show,
        )


def clear_graph_files(folder, config):
    if not folder.exists():
        return

    for graph_path in folder.glob(
            f"{config.symbol}_*.png"
    ):
        graph_path.unlink()


def copy_trade_graphs_to_folder(dates, output_folder, config):
    output_folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    if config.clear_old_graphs:
        clear_graph_files(
            output_folder,
            config,
        )

    for trade_date in dates:
        file_name = (
            f"{config.symbol}_"
            f"{trade_date}.png"
        )

        source_path = (
            config.output_folder
            / file_name
        )

        output_path = (
            output_folder
            / file_name
        )

        if source_path.exists():
            shutil.copy2(
                source_path,
                output_path,
            )
        else:
            print(
                "Missing graph:",
                source_path,
            )


def save_negative_trade_graphs(trade_log, config):
    if not config.save_negative_graphs:
        return

    negative_dates = (
        trade_log.loc[
            trade_log["trade_return_pct"] < 0,
            "date",
        ]
        .astype(str)
        .unique()
    )

    copy_trade_graphs_to_folder(
        negative_dates,
        config.negative_graph_folder,
        config,
    )


def save_non_traded_graphs(data, config):
    if not config.save_non_traded_graphs:
        return

    buy_days = (
        data.groupby("date")["buy_signal"]
        .any()
    )

    non_traded_dates = (
        buy_days[
            ~buy_days
        ]
        .index
        .astype(str)
        .unique()
    )

    copy_trade_graphs_to_folder(
        non_traded_dates,
        config.non_traded_graph_folder,
        config,
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    config = StrategyConfig()

    data = load_data(config)

    trade_log = create_trade_log(
        data
    )

    trade_log = trading_simulation(
        trade_log,
        config,
    )

    summary = summarize_trading(
        trade_log,
        data,
        config,
    )

    save_trade_log(
        trade_log,
        config,
    )

    plot_all_trade_days(
        data,
        config,
        show=False,
    )

    save_negative_trade_graphs(
        trade_log,
        config,
    )

    save_non_traded_graphs(
        data,
        config,
    )

    print(trade_log)
    print_trading_summary(summary)
