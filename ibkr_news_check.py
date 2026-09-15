from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import csv
import os
import threading
import time

try:
    from ibapi.client import EClient
    from ibapi.contract import Contract
    from ibapi.wrapper import EWrapper
except ImportError:
    print("Missing package: ibapi")
    print("Install it with: python3 -m pip install ibapi")
    raise SystemExit(1)

from intraday import load_env_file


IBKR_INFO_CODES = {
    2104,
    2106,
    2107,
    2108,
    2158,
}


@dataclass(frozen=True)
class IBKRNewsConfig:
    host: str = "127.0.0.1"
    port: int = 4002
    client_id: int = 6
    timeout_seconds: int = 20
    symbol: str = "APLD"
    exchange: str = "SMART"
    currency: str = "USD"
    provider_codes: str = ""
    lookback_days: int = 7
    max_headlines: int = 20
    log_path: Path = Path("ibkr_news_log.csv")


def load_config():
    load_env_file()

    return IBKRNewsConfig(
        host=os.environ.get("IBKR_HOST", "127.0.0.1"),
        port=int(os.environ.get("IBKR_PORT", "4002")),
        client_id=int(os.environ.get("IBKR_NEWS_CLIENT_ID", "6")),
        timeout_seconds=int(os.environ.get("IBKR_TIMEOUT_SECONDS", "20")),
        symbol=os.environ.get("IBKR_NEWS_SYMBOL", "APLD"),
        exchange=os.environ.get("IBKR_NEWS_EXCHANGE", "SMART"),
        currency=os.environ.get("IBKR_NEWS_CURRENCY", "USD"),
        provider_codes=os.environ.get("IBKR_NEWS_PROVIDER_CODES", ""),
        lookback_days=int(os.environ.get("IBKR_NEWS_LOOKBACK_DAYS", "7")),
        max_headlines=int(os.environ.get("IBKR_NEWS_MAX_HEADLINES", "20")),
        log_path=Path(os.environ.get("IBKR_NEWS_LOG_PATH", "ibkr_news_log.csv")),
    )


def stock_contract(config):
    contract = Contract()
    contract.symbol = config.symbol
    contract.secType = "STK"
    contract.exchange = config.exchange
    contract.currency = config.currency

    return contract


def ibkr_news_time(value):
    if value is None:
        return ""

    if isinstance(value, str):
        return value

    return datetime.fromtimestamp(
        int(value),
        tz=timezone.utc,
    ).isoformat()


def historical_start_time(config):
    start = datetime.now(timezone.utc) - timedelta(
        days=config.lookback_days,
    )

    return start.strftime("%Y%m%d %H:%M:%S UTC")


class IBKRNewsCheck(EWrapper, EClient):
    def __init__(self, config):
        EClient.__init__(self, self)

        self.config = config
        self.ready = threading.Event()
        self.providers_done = threading.Event()
        self.contract_done = threading.Event()
        self.news_done = threading.Event()

        self.providers = []
        self.contract_details = []
        self.headlines = []
        self.messages = []

    def nextValidId(self, orderId):
        self.ready.set()
        self.reqNewsProviders()
        self.reqContractDetails(
            1,
            stock_contract(self.config),
        )

    def newsProviders(self, newsProviders):
        self.providers = [
            {
                "provider_code": getattr(
                    provider,
                    "providerCode",
                    provider.code,
                ),
                "provider_name": getattr(
                    provider,
                    "providerName",
                    provider.name,
                ),
            }
            for provider in newsProviders
        ]
        self.providers_done.set()

    def contractDetails(self, reqId, contractDetails):
        self.contract_details.append(contractDetails)

    def contractDetailsEnd(self, reqId):
        self.contract_done.set()

    def historicalNews(
        self,
        reqId,
        time_,
        providerCode,
        articleId,
        headline,
    ):
        self.headlines.append({
            "news_time": ibkr_news_time(time_),
            "provider_code": providerCode,
            "article_id": articleId,
            "headline": headline,
        })

    def historicalNewsEnd(
        self,
        reqId,
        hasMore,
    ):
        self.news_done.set()

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
            self.news_done.set()

    def connectionClosed(self):
        self.messages.append(
            (
                -1,
                -1,
                "Connection closed.",
            )
        )
        self.news_done.set()


def selected_provider_codes(app, config):
    if config.provider_codes.strip():
        return config.provider_codes.strip()

    return ",".join(
        provider["provider_code"]
        for provider in app.providers
        if provider["provider_code"]
    )


def request_historical_news(app, config):
    if not app.contract_details:
        return False

    provider_codes = selected_provider_codes(
        app,
        config,
    )

    if not provider_codes:
        return False

    contract_id = (
        app.contract_details[0]
        .contract
        .conId
    )

    app.reqHistoricalNews(
        2,
        contract_id,
        provider_codes,
        historical_start_time(config),
        "",
        config.max_headlines,
        [],
    )

    return True


def save_headlines(config, headlines):
    if not headlines:
        return

    fieldnames = [
        "news_time",
        "provider_code",
        "article_id",
        "headline",
    ]

    file_exists = config.log_path.exists()

    with config.log_path.open(
        "a",
        newline="",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
        )

        if not file_exists:
            writer.writeheader()

        writer.writerows(headlines)


def run_news_check(config):
    app = IBKRNewsCheck(config)
    app.connect(
        config.host,
        config.port,
        clientId=config.client_id,
    )

    api_thread = threading.Thread(
        target=app.run,
        daemon=True,
    )
    api_thread.start()

    if not app.ready.wait(config.timeout_seconds):
        app.disconnect()
        return app, False, "IBKR Gateway connection was not ready."

    app.providers_done.wait(config.timeout_seconds)
    app.contract_done.wait(config.timeout_seconds)

    requested_news = request_historical_news(
        app,
        config,
    )

    if requested_news:
        app.news_done.wait(config.timeout_seconds)

    save_headlines(
        config,
        app.headlines,
    )

    app.disconnect()
    time.sleep(0.5)

    if not requested_news:
        return app, False, (
            "Could not request historical news. Check that the contract "
            "resolved and at least one API news provider is available."
        )

    return app, True, "News check completed."


def print_result(config, app, requested_news, status):
    print("IBKR news check")
    print("---------------")
    print("Host:", config.host)
    print("Port:", config.port)
    print("Client ID:", config.client_id)
    print("Symbol:", config.symbol)
    print("Requested historical news:", requested_news)
    print("Log path:", config.log_path)

    print("\nNews providers:")
    if app.providers:
        for provider in app.providers:
            print(
                f"{provider['provider_code']}: "
                f"{provider['provider_name']}"
            )
    else:
        print("none received")

    print("\nContract:")
    if app.contract_details:
        contract = app.contract_details[0].contract
        print("conId:", contract.conId)
        print("symbol:", contract.symbol)
        print("exchange:", contract.exchange)
        print("primaryExchange:", contract.primaryExchange)
    else:
        print("not resolved")

    print("\nHeadlines:")
    if app.headlines:
        for headline in app.headlines:
            print(
                f"{headline['news_time']} "
                f"[{headline['provider_code']}] "
                f"{headline['headline']}"
            )
    else:
        print("none received")

    if app.messages:
        print("\nIBKR messages:")
        for _, code, message in app.messages:
            print(f"{code}: {message}")

    print("\nStatus:", status)


def main():
    config = load_config()
    app, requested_news, status = run_news_check(config)
    print_result(
        config,
        app,
        requested_news,
        status,
    )


if __name__ == "__main__":
    main()
