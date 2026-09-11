from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import json
import os
import threading
import time

import pandas as pd

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper
except ImportError:
    print("Missing package: ibapi")
    print("Install it with: python3 -m pip install ibapi")
    raise SystemExit(1)

from intraday import StrategyConfig, load_env_file, run_strategy_on_data


IBKR_INFO_CODES = {
    2104,
    2106,
    2107,
    2108,
    2158,
}
TERMINAL_ORDER_STATUSES = {
    "Filled",
    "Cancelled",
    "Inactive",
    "ApiCancelled",
}
SIGNAL_FIELDNAMES = [
    "event_time",
    "latest_bar_time",
    "latest_close",
    "latest_candle",
    "bars_received",
    "action",
    "signal_time",
    "price",
    "shares",
    "reason",
    "status",
]


def env_bool(name, default):
    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class IBKRLiveStrategyConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 4
    timeout_seconds: int = 20
    symbol: str = "APLD"
    timezone: str = "America/New_York"
    duration: str = "1 D"
    bar_size: str = "5 mins"
    what_to_show: str = "TRADES"
    use_regular_trading_hours: int = 0
    exchange: str = "SMART"
    currency: str = "USD"
    money_per_trade: float = 10000.0
    max_signal_age_minutes: int = 7
    loop: bool = False
    check_interval_seconds: int = 300
    stop_after_checks: int = 0
    place_orders: bool = False
    what_if: bool = True
    cancel_after_seconds: int = 20
    allow_live_port: bool = False
    state_path: Path = Path("ibkr_live_state.json")
    signal_log_path: Path = Path("ibkr_live_signal_log.csv")
    order_log_path: Path = Path("ibkr_live_order_log.csv")


def load_config():
    load_env_file()

    return IBKRLiveStrategyConfig(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_LIVE_CLIENT_ID", "4")),
        timeout_seconds=int(os.environ.get("IBKR_TIMEOUT_SECONDS", "20")),
        symbol=os.environ.get("IBKR_LIVE_SYMBOL", "APLD"),
        timezone=os.environ.get("IBKR_LIVE_TIMEZONE", "America/New_York"),
        duration=os.environ.get("IBKR_LIVE_DURATION", "1 D"),
        bar_size=os.environ.get("IBKR_LIVE_BAR_SIZE", "5 mins"),
        what_to_show=os.environ.get("IBKR_LIVE_WHAT_TO_SHOW", "TRADES"),
        use_regular_trading_hours=int(os.environ.get("IBKR_LIVE_USE_RTH", "0")),
        money_per_trade=float(os.environ.get("IBKR_LIVE_MONEY_PER_TRADE", "10000")),
        max_signal_age_minutes=int(
            os.environ.get("IBKR_LIVE_MAX_SIGNAL_AGE_MINUTES", "7")
        ),
        loop=env_bool("IBKR_LIVE_LOOP", False),
        check_interval_seconds=int(
            os.environ.get("IBKR_LIVE_CHECK_INTERVAL_SECONDS", "300")
        ),
        stop_after_checks=int(os.environ.get("IBKR_LIVE_STOP_AFTER_CHECKS", "0")),
        place_orders=env_bool("IBKR_LIVE_PLACE_ORDERS", False),
        what_if=env_bool("IBKR_LIVE_WHAT_IF", True),
        cancel_after_seconds=int(os.environ.get("IBKR_LIVE_CANCEL_AFTER_SECONDS", "20")),
        allow_live_port=env_bool("IBKR_ALLOW_LIVE_PORT", False),
        state_path=Path(os.environ.get("IBKR_LIVE_STATE_PATH", "ibkr_live_state.json")),
        signal_log_path=Path(
            os.environ.get("IBKR_LIVE_SIGNAL_LOG_PATH", "ibkr_live_signal_log.csv")
        ),
        order_log_path=Path(
            os.environ.get("IBKR_LIVE_ORDER_LOG_PATH", "ibkr_live_order_log.csv")
        ),
    )


def validate_config(config):
    if config.port in {4001, 7496} and not config.allow_live_port:
        raise ValueError(
            "This file is for paper trading first. Refusing to use a live IBKR port. "
            "Use paper port 4002 for IB Gateway or 7497 for TWS."
        )

    if config.money_per_trade <= 0:
        raise ValueError("IBKR_LIVE_MONEY_PER_TRADE must be greater than 0.")

    if config.check_interval_seconds <= 0:
        raise ValueError("IBKR_LIVE_CHECK_INTERVAL_SECONDS must be greater than 0.")

    if config.stop_after_checks < 0:
        raise ValueError("IBKR_LIVE_STOP_AFTER_CHECKS cannot be negative.")

    if config.place_orders and config.what_if:
        print("Note: IBKR_LIVE_WHAT_IF=true, so orders are validation-only.")


def stock_contract(config):
    contract = Contract()
    contract.symbol = config.symbol
    contract.secType = "STK"
    contract.exchange = config.exchange
    contract.currency = config.currency
    return contract


def limit_order(action, quantity, limit_price, what_if):
    order = Order()
    order.action = action
    order.orderType = "LMT"
    order.totalQuantity = int(quantity)
    order.lmtPrice = round(float(limit_price), 2)
    order.tif = "DAY"
    order.whatIf = what_if
    order.transmit = True
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    order.orderRef = "intraday-live-strategy"
    return order


class IBKRHistoricalBars(EWrapper, EClient):
    def __init__(self, config):
        EClient.__init__(self, self)
        self.config = config
        self.ready = threading.Event()
        self.done = threading.Event()
        self.bars = []
        self.messages = []

    def nextValidId(self, orderId):
        self.ready.set()
        self.reqMarketDataType(1)
        self.reqHistoricalData(
            1,
            stock_contract(self.config),
            "",
            self.config.duration,
            self.config.bar_size,
            self.config.what_to_show,
            self.config.use_regular_trading_hours,
            2,
            False,
            [],
        )

    def historicalData(self, reqId, bar):
        self.bars.append(
            {
                "timestamp": parse_ibkr_bar_time(bar.date),
                "symbol": self.config.symbol,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            }
        )

    def historicalDataEnd(self, reqId, start, end):
        self.done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.messages.append((reqId, errorCode, errorString))
        if errorCode not in IBKR_INFO_CODES:
            self.done.set()

    def connectionClosed(self):
        self.messages.append((-1, -1, "Connection closed."))
        self.done.set()


class IBKROrderClient(EWrapper, EClient):
    def __init__(self, config, action, quantity, limit_price):
        EClient.__init__(self, self)
        self.config = config
        self.action = action
        self.quantity = quantity
        self.limit_price = limit_price
        self.ready = threading.Event()
        self.done = threading.Event()
        self.order_id = None
        self.open_orders = []
        self.order_statuses = []
        self.executions = []
        self.messages = []

    def nextValidId(self, orderId):
        self.order_id = orderId
        self.ready.set()
        self.placeOrder(
            orderId,
            stock_contract(self.config),
            limit_order(
                self.action,
                self.quantity,
                self.limit_price,
                self.config.what_if,
            ),
        )

    def openOrder(self, orderId, contract, order, orderState):
        self.open_orders.append(
            {
                "event_time": datetime.now().isoformat(timespec="seconds"),
                "order_id": orderId,
                "symbol": contract.symbol,
                "action": order.action,
                "order_type": order.orderType,
                "quantity": order.totalQuantity,
                "limit_price": order.lmtPrice,
                "status": orderState.status,
                "what_if": order.whatIf,
            }
        )
        if self.config.what_if:
            self.done.set()

    def orderStatus(
        self,
        orderId,
        status,
        filled,
        remaining,
        avgFillPrice,
        permId,
        parentId,
        lastFillPrice,
        clientId,
        whyHeld,
        mktCapPrice,
    ):
        self.order_statuses.append(
            {
                "event_time": datetime.now().isoformat(timespec="seconds"),
                "order_id": orderId,
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avg_fill_price": avgFillPrice,
                "last_fill_price": lastFillPrice,
                "why_held": whyHeld,
            }
        )
        if status in TERMINAL_ORDER_STATUSES:
            self.done.set()

    def execDetails(self, reqId, contract, execution):
        self.executions.append(
            {
                "event_time": datetime.now().isoformat(timespec="seconds"),
                "order_id": execution.orderId,
                "symbol": contract.symbol,
                "side": execution.side,
                "shares": execution.shares,
                "price": execution.price,
                "avg_price": execution.avgPrice,
                "execution_time": execution.time,
                "exchange": execution.exchange,
            }
        )
        self.done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.messages.append((reqId, errorCode, errorString))
        if errorCode not in IBKR_INFO_CODES:
            self.done.set()

    def connectionClosed(self):
        self.messages.append((-1, -1, "Connection closed."))
        self.done.set()


def parse_ibkr_bar_time(value):
    try:
        return pd.to_datetime(int(value), unit="s", utc=True)
    except (TypeError, ValueError):
        return pd.to_datetime(value, utc=True)


def fetch_recent_bars(config):
    app = IBKRHistoricalBars(config)
    app.connect(config.host, config.port, clientId=config.client_id)

    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()

    ready = app.ready.wait(config.timeout_seconds)

    if ready:
        app.done.wait(config.timeout_seconds)

    result = {
        "connected": app.isConnected(),
        "ready": ready,
        "bars": pd.DataFrame(app.bars),
        "messages": app.messages,
    }

    app.disconnect()
    time.sleep(0.5)
    return result


def empty_state():
    return {
        "position": "flat",
        "trade_date": None,
        "buy_time": None,
        "buy_price": None,
        "shares": 0,
        "last_signal_id": None,
    }


def load_state(path):
    if not path.exists():
        return empty_state()

    return {**empty_state(), **json.loads(path.read_text())}


def save_state(path, state):
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def signal_id(action, timestamp, price):
    return f"{action}|{timestamp}|{price}"


def bar_minutes_from_size(bar_size):
    parts = bar_size.strip().split()

    if len(parts) >= 2 and parts[1].lower().startswith("min"):
        return int(parts[0])

    return 5


def is_fresh_signal(signal_time, config):
    if pd.isna(signal_time):
        return False

    now = pd.Timestamp.now(tz=config.timezone)
    age_minutes = (now - signal_time).total_seconds() / 60
    return 0 <= age_minutes <= config.max_signal_age_minutes


def calculate_order_shares(money_per_trade, price):
    if pd.isna(price) or price <= 0:
        return 0

    return int(money_per_trade // price)


def latest_buy_action(strategy_data, state, live_config):
    buy_rows = strategy_data[
        strategy_data["first_buy_signal"]
        & strategy_data["buy_time"].notna()
        & strategy_data["buy_price"].notna()
    ].copy()

    if buy_rows.empty:
        return None

    buy_rows = buy_rows.sort_values("buy_time")
    row = buy_rows.iloc[-1]
    action_id = signal_id("BUY", row["buy_time"], row["buy_price"])

    if state["position"] != "flat":
        return None

    if state["last_signal_id"] == action_id:
        return None

    if not is_fresh_signal(row["buy_time"], live_config):
        return None

    shares = calculate_order_shares(live_config.money_per_trade, row["buy_price"])

    if shares <= 0:
        return None

    return {
        "action": "BUY",
        "signal_time": row["buy_time"],
        "price": row["buy_price"],
        "shares": shares,
        "signal_id": action_id,
        "reason": "fresh_strategy_buy",
    }


def latest_sell_action(strategy_data, state, live_config):
    if state["position"] != "long":
        return None

    sell_rows = strategy_data[
        strategy_data["first_sell_signal"]
        & strategy_data["sell_time"].notna()
        & strategy_data["sell_price"].notna()
    ].copy()

    if state["buy_time"] is not None:
        sell_rows = sell_rows[sell_rows["sell_time"] > pd.Timestamp(state["buy_time"])]

    if sell_rows.empty:
        return None

    sell_rows = sell_rows.sort_values("sell_time")
    row = sell_rows.iloc[0]
    action_id = signal_id("SELL", row["sell_time"], row["sell_price"])

    if state["last_signal_id"] == action_id:
        return None

    if not is_fresh_signal(row["sell_time"], live_config):
        return None

    shares = int(state["shares"])

    if shares <= 0:
        return None

    if row.get("force_exit", False):
        sell_reason = "force_exit"
    elif row.get("late_sell_signal", False):
        sell_reason = "late_sell"
    elif row.get("final_exit_signal", False):
        sell_reason = "final_exit"
    else:
        sell_reason = "profit_sell"

    return {
        "action": "SELL",
        "signal_time": row["sell_time"],
        "price": row["sell_price"],
        "shares": shares,
        "signal_id": action_id,
        "reason": sell_reason,
    }


def decide_action(strategy_data, state, live_config):
    sell_action = latest_sell_action(strategy_data, state, live_config)

    if sell_action is not None:
        return sell_action

    return latest_buy_action(strategy_data, state, live_config)


def place_strategy_order(config, action):
    app = IBKROrderClient(
        config,
        action=action["action"],
        quantity=action["shares"],
        limit_price=action["price"],
    )
    app.connect(config.host, config.port, clientId=config.client_id + 1)

    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()

    ready = app.ready.wait(config.timeout_seconds)

    if ready:
        app.done.wait(config.cancel_after_seconds)

        if not config.what_if and app.order_id is not None:
            remaining = latest_remaining(app.order_statuses)

            if remaining is None or remaining > 0:
                app.cancelOrder(app.order_id, "")
                app.done.wait(config.timeout_seconds)

    result = {
        "connected": app.isConnected(),
        "ready": ready,
        "order_id": app.order_id,
        "open_orders": app.open_orders,
        "order_statuses": app.order_statuses,
        "executions": app.executions,
        "messages": app.messages,
    }

    app.disconnect()
    time.sleep(0.5)
    return result


def latest_remaining(statuses):
    if not statuses:
        return None

    return statuses[-1]["remaining"]


def filled_shares(order_result):
    status_filled = [
        status["filled"]
        for status in order_result["order_statuses"]
        if status["filled"] is not None
    ]
    execution_filled = [
        execution["shares"]
        for execution in order_result["executions"]
        if execution["shares"] is not None
    ]

    if status_filled:
        return max(status_filled)

    if execution_filled:
        return sum(execution_filled)

    return 0


def update_state_after_order(state, action, order_result, config):
    state = dict(state)

    if config.what_if:
        return state

    filled = filled_shares(order_result)

    if filled <= 0:
        state["last_signal_id"] = action["signal_id"]
        return state

    state["last_signal_id"] = action["signal_id"]

    if action["action"] == "BUY":
        state["position"] = "long"
        state["trade_date"] = action["signal_time"].date().isoformat()
        state["buy_time"] = action["signal_time"].isoformat()
        state["buy_price"] = float(action["price"])
        state["shares"] = int(filled)
    elif action["action"] == "SELL":
        state = empty_state()
        state["last_signal_id"] = action["signal_id"]

    return state


def append_csv(path, rows, fieldnames):
    if not rows:
        return

    file_exists = path.exists()

    if file_exists:
        with path.open(newline="") as file:
            reader = csv.DictReader(file)
            existing_fieldnames = reader.fieldnames or []

            if existing_fieldnames != fieldnames:
                existing_rows = list(reader)

                with path.open("w", newline="") as rewrite_file:
                    writer = csv.DictWriter(rewrite_file, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(existing_rows)

    with path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerows(rows)


def format_log_value(value):
    if pd.isna(value):
        return ""

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def latest_context(strategy_data):
    if strategy_data.empty:
        return {
            "latest_bar_time": "",
            "latest_close": "",
            "latest_candle": "",
        }

    latest = strategy_data.iloc[-1]
    return {
        "latest_bar_time": format_log_value(latest["timestamp"]),
        "latest_close": round(float(latest["close"]), 4),
        "latest_candle": latest["candle_color"],
    }


def log_signal(config, action, status, context=None, bars_received=""):
    context = context or {}
    row = {
        "event_time": datetime.now().isoformat(timespec="seconds"),
        "latest_bar_time": context.get("latest_bar_time", ""),
        "latest_close": context.get("latest_close", ""),
        "latest_candle": context.get("latest_candle", ""),
        "bars_received": bars_received,
        "action": action["action"] if action else "",
        "signal_time": action["signal_time"] if action else "",
        "price": action["price"] if action else "",
        "shares": action["shares"] if action else "",
        "reason": action["reason"] if action else "",
        "status": status,
    }
    append_csv(config.signal_log_path, [row], SIGNAL_FIELDNAMES)


def log_order(config, action, order_result):
    fieldnames = [
        "event_time",
        "signal_action",
        "signal_time",
        "signal_price",
        "signal_shares",
        "order_id",
        "status",
        "filled",
        "remaining",
        "avg_fill_price",
        "last_fill_price",
        "message_code",
        "message",
    ]
    rows = []

    for status in order_result["order_statuses"]:
        rows.append(
            {
                "event_time": status["event_time"],
                "signal_action": action["action"],
                "signal_time": action["signal_time"],
                "signal_price": action["price"],
                "signal_shares": action["shares"],
                "order_id": status["order_id"],
                "status": status["status"],
                "filled": status["filled"],
                "remaining": status["remaining"],
                "avg_fill_price": status["avg_fill_price"],
                "last_fill_price": status["last_fill_price"],
                "message_code": "",
                "message": "",
            }
        )

    for _, code, message in order_result["messages"]:
        rows.append(
            {
                "event_time": datetime.now().isoformat(timespec="seconds"),
                "signal_action": action["action"],
                "signal_time": action["signal_time"],
                "signal_price": action["price"],
                "signal_shares": action["shares"],
                "order_id": order_result["order_id"],
                "status": "",
                "filled": "",
                "remaining": "",
                "avg_fill_price": "",
                "last_fill_price": "",
                "message_code": code,
                "message": message,
            }
        )

    append_csv(config.order_log_path, rows, fieldnames)


def print_messages(messages):
    if not messages:
        return

    print("\nIBKR messages:")
    for _, code, message in messages:
        print(f"{code}: {message}")


def print_latest_context(strategy_data):
    if strategy_data.empty:
        print("No strategy bars available.")
        return latest_context(strategy_data)

    latest = strategy_data.iloc[-1]
    print("Latest bar:", latest["timestamp"])
    print("Latest close:", round(latest["close"], 4))
    print("Latest candle:", latest["candle_color"])
    return latest_context(strategy_data)


def run_one_check(live_config):
    strategy_config = StrategyConfig(
        symbol=live_config.symbol,
        timezone=live_config.timezone,
        bar_minutes=bar_minutes_from_size(live_config.bar_size),
        money_per_trade=live_config.money_per_trade,
    )
    state = load_state(live_config.state_path)

    bars_result = fetch_recent_bars(live_config)
    bars = bars_result["bars"]

    print("IBKR live strategy bridge")
    print("-------------------------")
    print("Connected:", bars_result["connected"])
    print("API ready:", bars_result["ready"])
    print("Bars received:", len(bars))
    print("Place orders:", live_config.place_orders)
    print("What-if:", live_config.what_if)
    print("State position:", state["position"])

    print_messages(bars_result["messages"])

    if not bars_result["ready"] or bars.empty:
        log_signal(live_config, None, "no_bars", bars_received=len(bars))
        print("\nStatus: Could not get enough IBKR bars.")
        return

    strategy_data = run_strategy_on_data(bars, strategy_config)
    context = print_latest_context(strategy_data)

    action = decide_action(strategy_data, state, live_config)

    if action is None:
        log_signal(
            live_config,
            None,
            "no_fresh_signal",
            context,
            bars_received=len(bars),
        )
        print("\nStatus: No fresh buy/sell signal right now.")
        return

    print("\nFresh action:")
    print("Action:", action["action"])
    print("Signal time:", action["signal_time"])
    print("Limit price:", round(action["price"], 4))
    print("Shares:", action["shares"])
    print("Reason:", action["reason"])

    if not live_config.place_orders:
        log_signal(
            live_config,
            action,
            "signal_only",
            context,
            bars_received=len(bars),
        )
        print("\nStatus: Signal found, but order placement is off.")
        print("Set IBKR_LIVE_PLACE_ORDERS=true to allow paper order placement.")
        return

    order_result = place_strategy_order(live_config, action)
    log_order(live_config, action, order_result)
    print_messages(order_result["messages"])

    if order_result["ready"]:
        state = update_state_after_order(state, action, order_result, live_config)
        save_state(live_config.state_path, state)
        log_signal(
            live_config,
            action,
            "order_sent",
            context,
            bars_received=len(bars),
        )
        print("\nStatus: Order request reached IBKR.")
        print("State saved:", live_config.state_path)
        print("Order log:", live_config.order_log_path)
    else:
        log_signal(
            live_config,
            action,
            "order_not_ready",
            context,
            bars_received=len(bars),
        )
        print("\nStatus: IBKR order connection was not ready.")


def run_loop(live_config):
    check_count = 0

    print("Loop mode is on.")
    print("Check interval seconds:", live_config.check_interval_seconds)

    while True:
        check_count += 1
        print("\n========================================")
        print("Check number:", check_count)
        print("Check time:", datetime.now().isoformat(timespec="seconds"))
        print("========================================")

        try:
            run_one_check(live_config)
        except Exception as error:
            log_signal(live_config, None, f"error: {error}")
            print("\nStatus: Check failed.")
            print("Error:", error)

        if (
            live_config.stop_after_checks
            and check_count >= live_config.stop_after_checks
        ):
            print("\nLoop stopped after configured check count.")
            return

        print("\nNext check in seconds:", live_config.check_interval_seconds)
        time.sleep(live_config.check_interval_seconds)


def main():
    live_config = load_config()
    validate_config(live_config)

    if live_config.loop:
        run_loop(live_config)
    else:
        run_one_check(live_config)


if __name__ == "__main__":
    main()
