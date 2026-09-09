from dataclasses import dataclass
from pathlib import Path
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
class IBKROrderCheckConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 2
    timeout_seconds: int = 15
    symbol: str = "APLD"
    action: str = "BUY"
    quantity: int = 1
    limit_price: float = 0.01
    exchange: str = "SMART"
    currency: str = "USD"
    what_if: bool = True


def env_bool(name, default):
    value = os.environ.get(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "y"}


def load_config():
    load_env_file()

    return IBKROrderCheckConfig(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_ORDER_CLIENT_ID", "2")),
        timeout_seconds=int(os.environ.get("IBKR_TIMEOUT_SECONDS", "15")),
        symbol=os.environ.get("IBKR_TEST_SYMBOL", "APLD"),
        action=os.environ.get("IBKR_TEST_ACTION", "BUY").upper(),
        quantity=int(os.environ.get("IBKR_TEST_QUANTITY", "1")),
        limit_price=float(os.environ.get("IBKR_TEST_LIMIT_PRICE", "0.01")),
        what_if=env_bool("IBKR_TEST_WHAT_IF", True),
    )


def stock_contract(config):
    contract = Contract()
    contract.symbol = config.symbol
    contract.secType = "STK"
    contract.exchange = config.exchange
    contract.currency = config.currency
    return contract


def limit_order(config):
    order = Order()
    order.action = config.action
    order.orderType = "LMT"
    order.totalQuantity = config.quantity
    order.lmtPrice = config.limit_price
    order.tif = "DAY"
    order.whatIf = config.what_if
    order.transmit = config.what_if
    order.eTradeOnly = False
    order.firmQuoteOnly = False
    order.orderRef = "intraday-paper-order-check"
    return order


class IBKROrderCheck(EWrapper, EClient):
    def __init__(self, config):
        EClient.__init__(self, self)
        self.config = config
        self.ready = threading.Event()
        self.done = threading.Event()
        self.next_order_id = None
        self.order_statuses = []
        self.open_orders = []
        self.order_states = []
        self.errors = []

    def nextValidId(self, orderId):
        self.next_order_id = orderId
        self.ready.set()
        self.placeOrder(
            orderId,
            stock_contract(self.config),
            limit_order(self.config),
        )

    def openOrder(self, orderId, contract, order, orderState):
        self.open_orders.append(
            {
                "order_id": orderId,
                "symbol": contract.symbol,
                "action": order.action,
                "order_type": order.orderType,
                "quantity": order.totalQuantity,
                "limit_price": order.lmtPrice,
                "status": orderState.status,
            }
        )
        self.order_states.append(orderState)
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
                "order_id": orderId,
                "status": status,
                "filled": filled,
                "remaining": remaining,
                "avg_fill_price": avgFillPrice,
                "last_fill_price": lastFillPrice,
            }
        )
        self.done.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.errors.append((reqId, errorCode, errorString))
        if errorCode not in IBKR_INFO_CODES:
            self.done.set()

    def connectionClosed(self):
        self.errors.append((-1, -1, "Connection closed."))
        self.done.set()


def check_order(config):
    app = IBKROrderCheck(config)
    app.connect(config.host, config.port, clientId=config.client_id)

    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()

    ready = app.ready.wait(config.timeout_seconds)

    if ready:
        app.done.wait(config.timeout_seconds)

    result = {
        "connected": app.isConnected(),
        "ready": ready,
        "next_order_id": app.next_order_id,
        "open_orders": app.open_orders,
        "order_statuses": app.order_statuses,
        "order_states": app.order_states,
        "errors": app.errors,
    }

    app.disconnect()
    time.sleep(0.5)
    return result


def print_result(config, result):
    print("IBKR paper order check")
    print("----------------------")
    print("Host:", config.host)
    print("Port:", config.port)
    print("Client ID:", config.client_id)
    print("Connected:", result["connected"])
    print("API ready:", result["ready"])
    print("What-if only:", config.what_if)
    print("Symbol:", config.symbol)
    print("Action:", config.action)
    print("Quantity:", config.quantity)
    print("Limit price:", config.limit_price)
    print("Next order ID:", result["next_order_id"])

    if result["open_orders"]:
        print("\nOrder response:")
        for order in result["open_orders"]:
            print(order)

    if result["order_statuses"]:
        print("\nOrder status:")
        for status in result["order_statuses"]:
            print(status)

    if result["order_states"]:
        print("\nOrder state:")
        for state in result["order_states"]:
            print("Status:", state.status)
            print("Initial margin change:", state.initMarginChange)
            print("Maintenance margin change:", state.maintMarginChange)
            print("Equity with loan change:", state.equityWithLoanChange)
            print("Warning:", state.warningText or "none")

    if result["errors"]:
        print("\nMessages:")
        for _, code, message in result["errors"]:
            print(f"{code}: {message}")

    if result["ready"] and not result["errors"]:
        print("\nStatus: Paper order check reached IBKR.")
    elif result["ready"]:
        print("\nStatus: IBKR responded. Review the messages above.")
    else:
        print("\nStatus: IBKR API was not ready before timeout.")


def main():
    config = load_config()
    result = check_order(config)
    print_result(config, result)


if __name__ == "__main__":
    main()
