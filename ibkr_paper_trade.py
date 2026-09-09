from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import csv
import os
import threading
import time

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.order import Order
    from ibapi.wrapper import EWrapper
except ImportError:
    print("Missing package: ibapi")
    print("Install it with: python3 -m pip install ibapi")
    raise SystemExit(1)


ENV_FILE = Path(".env")
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


def load_env_file(path=ENV_FILE):
    if not path.exists():
        return

    for line in path.read_text().splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def env_bool(name, default):
    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y"}


@dataclass(frozen=True)
class IBKRPaperTradeConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 3
    timeout_seconds: int = 20
    symbol: str = "APLD"
    action: str = "BUY"
    quantity: int = 1
    order_type: str = "LMT"
    limit_price: float = 0.01
    tif: str = "DAY"
    what_if: bool = True
    transmit: bool = False
    cancel_after_seconds: int = 20
    exchange: str = "SMART"
    currency: str = "USD"
    order_log_path: Path = Path("ibkr_order_log.csv")
    allow_live_port: bool = False


def load_config():
    load_env_file()

    return IBKRPaperTradeConfig(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_PAPER_CLIENT_ID", "3")),
        timeout_seconds=int(os.environ.get("IBKR_TIMEOUT_SECONDS", "20")),
        symbol=os.environ.get("IBKR_PAPER_SYMBOL", "APLD"),
        action=os.environ.get("IBKR_PAPER_ACTION", "BUY").upper(),
        quantity=int(os.environ.get("IBKR_PAPER_QUANTITY", "1")),
        order_type=os.environ.get("IBKR_PAPER_ORDER_TYPE", "LMT").upper(),
        limit_price=float(os.environ.get("IBKR_PAPER_LIMIT_PRICE", "0.01")),
        tif=os.environ.get("IBKR_PAPER_TIF", "DAY").upper(),
        what_if=env_bool("IBKR_PAPER_WHAT_IF", True),
        transmit=env_bool("IBKR_PAPER_TRANSMIT", False),
        cancel_after_seconds=int(os.environ.get("IBKR_PAPER_CANCEL_AFTER_SECONDS", "20")),
        order_log_path=Path(os.environ.get("IBKR_ORDER_LOG_PATH", "ibkr_order_log.csv")),
        allow_live_port=env_bool("IBKR_ALLOW_LIVE_PORT", False),
    )


def validate_config(config):
    if config.port in {4001, 7496} and not config.allow_live_port:
        raise ValueError(
            "This script is for paper trading. Refusing to use a live IBKR port. "
            "Use paper port 4002 for IB Gateway or 7497 for TWS."
        )

    if config.action not in {"BUY", "SELL"}:
        raise ValueError("IBKR_PAPER_ACTION must be BUY or SELL.")

    if config.quantity <= 0:
        raise ValueError("IBKR_PAPER_QUANTITY must be greater than 0.")

    if config.order_type not in {"LMT", "MKT"}:
        raise ValueError("IBKR_PAPER_ORDER_TYPE must be LMT or MKT.")

    if config.order_type == "LMT" and config.limit_price <= 0:
        raise ValueError("IBKR_PAPER_LIMIT_PRICE must be greater than 0 for LMT orders.")

    if config.transmit and config.what_if:
        print("Note: IBKR_PAPER_WHAT_IF=true, so this is still only an order check.")


def stock_contract(config):
    contract = Contract()
    contract.symbol = config.symbol
    contract.secType = "STK"
    contract.exchange = config.exchange
    contract.currency = config.currency
    return contract


def build_order(config):
    order = Order()
    order.action = config.action
    order.orderType = config.order_type
    order.totalQuantity = config.quantity
    order.tif = config.tif
    order.whatIf = config.what_if
    order.transmit = config.transmit or config.what_if
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    order.orderRef = "intraday-paper-trade"

    if config.order_type == "LMT":
        order.lmtPrice = config.limit_price

    return order


class IBKRPaperTrade(EWrapper, EClient):
    def __init__(self, config):
        EClient.__init__(self, self)
        self.config = config
        self.ready = threading.Event()
        self.done = threading.Event()
        self.next_order_id = None
        self.order_id = None
        self.order_statuses = []
        self.open_orders = []
        self.executions = []
        self.commissions = []
        self.messages = []

    def nextValidId(self, orderId):
        self.next_order_id = orderId
        self.order_id = orderId
        self.ready.set()
        self.placeOrder(
            orderId,
            stock_contract(self.config),
            build_order(self.config),
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
                "limit_price": getattr(order, "lmtPrice", None),
                "status": orderState.status,
                "what_if": order.whatIf,
                "transmit": order.transmit,
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

    def commissionReport(self, commissionReport):
        self.commissions.append(
            {
                "event_time": datetime.now().isoformat(timespec="seconds"),
                "exec_id": commissionReport.execId,
                "commission": commissionReport.commission,
                "currency": commissionReport.currency,
                "realized_pnl": commissionReport.realizedPNL,
            }
        )

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.messages.append((reqId, errorCode, errorString))
        if errorCode not in IBKR_INFO_CODES:
            self.done.set()

    def connectionClosed(self):
        self.messages.append((-1, -1, "Connection closed."))
        self.done.set()


def latest_remaining(statuses):
    if not statuses:
        return None

    return statuses[-1]["remaining"]


def run_paper_trade(config):
    app = IBKRPaperTrade(config)
    app.connect(config.host, config.port, clientId=config.client_id)

    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()

    ready = app.ready.wait(config.timeout_seconds)

    if ready:
        if config.what_if or not config.transmit:
            app.done.wait(config.timeout_seconds)
        else:
            app.done.wait(config.cancel_after_seconds)

            remaining = latest_remaining(app.order_statuses)
            if remaining is None or remaining > 0:
                app.cancelOrder(app.order_id, "")
                app.done.wait(config.timeout_seconds)

    result = {
        "connected": app.isConnected(),
        "ready": ready,
        "next_order_id": app.next_order_id,
        "order_id": app.order_id,
        "open_orders": app.open_orders,
        "order_statuses": app.order_statuses,
        "executions": app.executions,
        "commissions": app.commissions,
        "messages": app.messages,
    }

    app.disconnect()
    time.sleep(0.5)
    return result


def write_order_log(config, result):
    fieldnames = [
        "event_time",
        "order_id",
        "symbol",
        "action",
        "order_type",
        "quantity",
        "limit_price",
        "status",
        "filled",
        "remaining",
        "avg_fill_price",
        "last_fill_price",
        "what_if",
        "transmit",
    ]
    rows = []

    for order in result["open_orders"]:
        rows.append({key: order.get(key, "") for key in fieldnames})

    for status in result["order_statuses"]:
        row = {key: "" for key in fieldnames}
        row.update(status)
        rows.append(row)

    if not rows:
        return

    file_exists = config.order_log_path.exists()

    with config.order_log_path.open("a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerows(rows)


def print_result(config, result):
    print("IBKR paper trade runner")
    print("-----------------------")
    print("Host:", config.host)
    print("Port:", config.port)
    print("Client ID:", config.client_id)
    print("Connected:", result["connected"])
    print("API ready:", result["ready"])
    print("What-if:", config.what_if)
    print("Transmit:", config.transmit)
    print("Symbol:", config.symbol)
    print("Action:", config.action)
    print("Order type:", config.order_type)
    print("Quantity:", config.quantity)

    if config.order_type == "LMT":
        print("Limit price:", config.limit_price)

    print("Order ID:", result["order_id"])

    if result["open_orders"]:
        print("\nOrder response:")
        for order in result["open_orders"]:
            print(order)

    if result["order_statuses"]:
        print("\nOrder status:")
        for status in result["order_statuses"]:
            print(status)

    if result["executions"]:
        print("\nExecutions:")
        for execution in result["executions"]:
            print(execution)

    if result["commissions"]:
        print("\nCommissions:")
        for commission in result["commissions"]:
            print(commission)

    if result["messages"]:
        print("\nMessages:")
        for _, code, message in result["messages"]:
            print(f"{code}: {message}")

    if result["ready"] and result["executions"]:
        print("\nStatus: Paper order reached IBKR and received execution details.")
    elif result["ready"] and result["open_orders"]:
        print("\nStatus: Paper order reached IBKR.")
    elif result["ready"]:
        print("\nStatus: IBKR responded. Review messages above.")
    else:
        print("\nStatus: IBKR API was not ready before timeout.")

    if result["open_orders"] or result["order_statuses"]:
        print("Order log:", config.order_log_path)


def main():
    config = load_config()
    validate_config(config)
    result = run_paper_trade(config)
    write_order_log(config, result)
    print_result(config, result)


if __name__ == "__main__":
    main()
