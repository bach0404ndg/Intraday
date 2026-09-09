from dataclasses import dataclass
from pathlib import Path
import os
import threading
import time

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
except ImportError:
    print("Missing package: ibapi")
    print("Install it with: python3 -m pip install ibapi")
    raise SystemExit(1)


ENV_FILE = Path(".env")


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
class IBKRConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 1
    timeout_seconds: int = 10


def load_config():
    load_env_file()

    return IBKRConfig(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_CLIENT_ID", "1")),
        timeout_seconds=int(os.environ.get("IBKR_TIMEOUT_SECONDS", "10")),
    )


class IBKRConnectionCheck(EWrapper, EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.ready = threading.Event()
        self.accounts_received = threading.Event()
        self.current_time_received = threading.Event()
        self.next_order_id = None
        self.accounts = []
        self.server_time = None
        self.errors = []

    def nextValidId(self, orderId):
        self.next_order_id = orderId
        self.ready.set()
        self.reqManagedAccts()
        self.reqCurrentTime()

    def managedAccounts(self, accountsList):
        self.accounts = [
            account.strip()
            for account in accountsList.split(",")
            if account.strip()
        ]
        self.accounts_received.set()

    def currentTime(self, time_):
        self.server_time = time_
        self.current_time_received.set()

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        self.errors.append((reqId, errorCode, errorString))

    def connectionClosed(self):
        self.errors.append((-1, -1, "Connection closed."))


def check_gateway_connection(config):
    app = IBKRConnectionCheck()
    app.connect(config.host, config.port, clientId=config.client_id)

    api_thread = threading.Thread(target=app.run, daemon=True)
    api_thread.start()

    is_ready = app.ready.wait(config.timeout_seconds)
    app.accounts_received.wait(2)
    app.current_time_received.wait(2)

    result = {
        "connected": app.isConnected(),
        "ready": is_ready,
        "next_order_id": app.next_order_id,
        "accounts": app.accounts,
        "server_time": app.server_time,
        "errors": app.errors,
    }

    app.disconnect()
    time.sleep(0.5)
    return result


def print_result(config, result):
    print("IBKR Gateway connection check")
    print("-----------------------------")
    print("Host:", config.host)
    print("Port:", config.port)
    print("Client ID:", config.client_id)
    print("Connected:", result["connected"])
    print("API ready:", result["ready"])
    print("Next order ID:", result["next_order_id"])
    print("Accounts:", ", ".join(result["accounts"]) or "none received")
    print("Server time:", result["server_time"] or "none received")

    if result["errors"]:
        print("\nMessages:")
        for _, code, message in result["errors"]:
            print(f"{code}: {message}")

    if result["ready"]:
        print("\nStatus: IBKR API connection is working.")
    else:
        print("\nStatus: IBKR API connection was not ready before timeout.")
        print("Check that IB Gateway is open, logged into paper trading, and API access is enabled.")


def main():
    config = load_config()
    result = check_gateway_connection(config)
    print_result(config, result)


if __name__ == "__main__":
    main()
