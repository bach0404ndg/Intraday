# Intraday Project

Python research script for testing an intraday trading setup on APLD using 5-minute Alpaca market data. The script labels market sessions, calculates candle and gap metrics, creates buy/sell signals, plots daily candlestick charts, and prints basic trade performance statistics.

## Project Structure

```text
.
├── intraday.py
└── trade graph/
    └── APLD_YYYY-MM-DD.png
```

## Requirements

- Python 3.10+
- Alpaca market data access
- Python packages:
  - `alpaca-py`
  - `pandas`
  - `numpy`
  - `matplotlib`
  - `ibapi`

Install dependencies:

```bash
python3 -m pip install alpaca-py pandas numpy matplotlib ibapi
```

## Usage

Save your Alpaca credentials in a local `.env` file:

```text
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
IBKR_ORDER_CLIENT_ID=2
IBKR_TIMEOUT_SECONDS=10
IBKR_TEST_SYMBOL=APLD
IBKR_TEST_ACTION=BUY
IBKR_TEST_QUANTITY=1
IBKR_TEST_LIMIT_PRICE=0.01
IBKR_TEST_WHAT_IF=true
IBKR_PAPER_CLIENT_ID=3
IBKR_PAPER_SYMBOL=APLD
IBKR_PAPER_ACTION=BUY
IBKR_PAPER_QUANTITY=1
IBKR_PAPER_ORDER_TYPE=LMT
IBKR_PAPER_LIMIT_PRICE=0.01
IBKR_PAPER_TIF=DAY
IBKR_PAPER_WHAT_IF=true
IBKR_PAPER_TRANSMIT=false
IBKR_PAPER_CANCEL_AFTER_SECONDS=20
IBKR_ORDER_LOG_PATH=ibkr_order_log.csv
IBKR_ALLOW_LIVE_PORT=false
IBKR_LIVE_CLIENT_ID=4
IBKR_LIVE_SYMBOL=APLD
IBKR_LIVE_TIMEZONE=America/New_York
IBKR_LIVE_DURATION=3 D
IBKR_LIVE_BAR_SIZE=5 mins
IBKR_LIVE_WHAT_TO_SHOW=TRADES
IBKR_LIVE_USE_RTH=1
IBKR_LIVE_MONEY_PER_TRADE=10000
IBKR_LIVE_MAX_SIGNAL_AGE_MINUTES=7
IBKR_LIVE_LOOP=false
IBKR_LIVE_CHECK_INTERVAL_SECONDS=300
IBKR_LIVE_STOP_AFTER_CHECKS=0
IBKR_LIVE_PLACE_ORDERS=false
IBKR_LIVE_WHAT_IF=true
IBKR_LIVE_CANCEL_AFTER_SECONDS=20
IBKR_LIVE_STATE_PATH=ibkr_live_state.json
IBKR_LIVE_SIGNAL_LOG_PATH=ibkr_live_signal_log.csv
IBKR_LIVE_ORDER_LOG_PATH=ibkr_live_order_log.csv
```

The `.env` file is ignored by Git, so it stays on your computer and does not get pushed to GitHub.

## IBKR Paper Gateway Check

Use `ibkr_gateway_check.py` as the first safe IBKR connection test. It connects to IB Gateway or TWS and prints connection status, account IDs, server time, and the next order ID. It does not place trades.

For IB Gateway paper trading, use:

```text
IBKR_HOST=127.0.0.1
IBKR_PORT=4002
IBKR_CLIENT_ID=1
```

Before running it, open IB Gateway, log into paper trading, and make sure socket API access is enabled.

Run:

```bash
python3 ibkr_gateway_check.py
```

After the connection check works, run the safe paper order check:

```bash
python3 ibkr_paper_order_check.py
```

This sends a what-if limit order request to IBKR paper trading. By default it uses 1 share of APLD with a very low buy limit price and `IBKR_TEST_WHAT_IF=true`, so it is for order validation rather than trading.

The connection check can run while `Read-Only API` is checked. The paper order check needs `Read-Only API` unchecked, even for a what-if order request.

Run the paper trade runner:

```bash
python3 ibkr_paper_trade.py
```

By default, this is also a what-if request:

```text
IBKR_PAPER_WHAT_IF=true
IBKR_PAPER_TRANSMIT=false
```

To place a real paper order, set:

```text
IBKR_PAPER_WHAT_IF=false
IBKR_PAPER_TRANSMIT=true
```

Keep `IBKR_PAPER_ORDER_TYPE=LMT` while testing. If the order is not filled, the script cancels it after `IBKR_PAPER_CANCEL_AFTER_SECONDS`. Order responses and statuses are saved to `ibkr_order_log.csv`.

Run the live strategy bridge:

```bash
python3 ibkr_live_strategy.py
```

This pulls recent 5-minute bars from IBKR, runs the same strategy pipeline as `intraday.py`, and checks whether there is a fresh buy or sell signal. By default it only logs signals:

```text
IBKR_LIVE_LOOP=false
IBKR_LIVE_PLACE_ORDERS=false
IBKR_LIVE_WHAT_IF=true
```

To let it keep checking every 5 minutes, use:

```text
IBKR_LIVE_LOOP=true
IBKR_LIVE_CHECK_INTERVAL_SECONDS=300
IBKR_LIVE_STOP_AFTER_CHECKS=0
```

For a shorter test loop, set `IBKR_LIVE_STOP_AFTER_CHECKS=2`.

To allow paper order requests after the signal-only check is working, use:

```text
IBKR_LIVE_PLACE_ORDERS=true
IBKR_LIVE_WHAT_IF=true
```

That still sends what-if order checks. Only use `IBKR_LIVE_WHAT_IF=false` after paper signal timing is behaving correctly. If a real paper order is transmitted and not filled, the bridge cancels it after `IBKR_LIVE_CANCEL_AFTER_SECONDS`. The live bridge saves signal checks to `ibkr_live_signal_log.csv`, order responses to `ibkr_live_order_log.csv`, and position state to `ibkr_live_state.json`.

Tomorrow morning paper-trading flow:

- Open IB Gateway and log into paper trading.
- Keep `IBKR_PORT=4002`.
- Keep `IBKR_LIVE_PLACE_ORDERS=false` for the first run.
- Run `python3 ibkr_live_strategy.py` once to confirm bars are coming in.
- Set `IBKR_LIVE_LOOP=true` to keep checking every 5 minutes.
- After signal timing looks right, set `IBKR_LIVE_PLACE_ORDERS=true` with `IBKR_LIVE_WHAT_IF=true`.
- Only set `IBKR_LIVE_WHAT_IF=false` when you are ready for actual paper orders.

Default IBKR API ports:

```text
TWS paper:          7497
TWS live:           7496
IB Gateway paper:   4002
IB Gateway live:    4001
```

Run the script from the project root:

```bash
python3 intraday.py
```

The script currently:

- Requests 5-minute APLD stock bars from Alpaca.
- Converts timestamps to `America/New_York`.
- Separates pre-market, regular, after-hours, and overnight sessions.
- Calculates candle body/range metrics, opening gaps, slopes, buy signals, sell signals, and trade returns.
- Optionally saves daily chart images to graph folders.
- Prints summary statistics such as execution rate, sell hit rate, win rate, average return, best trade, and worst trade.

## Strategy Summary

Buy setup:

- Use only regular-session candles.
- Calculate the recent close-price slope using the previous `buy_regression_bars` candles.
- Mark a setup when the recent slope is negative.
- Require the setup candle body to be smaller than `doji_body_range_ratio` times the recent average range.
- Require the next candle after the setup candle to be green.

Buy execution:

- Execute two candles after the setup candle.
- Use that execution candle's open as `buy_price`.
- Only execute if the execution time is before `latest_buy_time`.

Profit sell:

- Wait until at least `sell_regression_bars` candles have passed after the buy.
- Calculate the recent close-price slope.
- Require the slope to still be positive.
- Require the slope to be weaker than the previous slope by at least `sell_slope_slowdown_pct`.
- Require the previous candle high to be within `near_high_pct` of the highest price since the buy.
- Sell at the current candle's open.

Force exit:

- Start checking after `force_exit_start_time`.
- If a candle low touches or falls below the first buy price, sell at the first buy price.
- The strategy assumes this fill is valid based on the 5-minute candle range.

## Strategy Parameters

Key parameters are defined in `StrategyConfig` at the top of `intraday.py`:

```python
symbol = "APLD"
start = "2025-01-01"
end = "2026-09-07"
buy_regression_bars = 7
body_average_bars = 12
range_quantile = 0.3
doji_body_range_ratio = 0.15
near_high_pct = 0.002
sell_regression_bars = 7
sell_slope_slowdown_pct = 0
force_exit_start_time = time(15, 0)
update_graphs = False
clear_existing_graphs = True
save_trade_log = True
trade_log_path = Path("trade_log.csv")
print_period_summary = True
summary_period_months = 1
include_quote_check_in_summary = True
quote_check_path = Path("quote_spread_check.csv")
simulate_real_trading = True
starting_money = 10000.0
money_per_trade = 10000.0
trading_cost = 2.0
```

Adjust these values to test different signal behavior.

`sell_slope_slowdown_pct` controls how much the upward slope must weaken before selling. For example, `0.20` means the current sell slope must be at least 20% lower than the previous sell slope. A value of `0` sells as soon as the current positive slope is lower than the previous positive slope.

The sell logic has two paths:

- Profit sell: price is near the high since buying and upward momentum has slowed enough.
- Force exit: after the configured force-exit time, if a candle low touches the first buy price, the trade exits at the buy price.

Set `update_graphs = True` when you want to regenerate chart files. Set it to `False` when you only want the strategy stats to run faster.

Keep `clear_existing_graphs = True` if you want graph folders to match the current run. This removes old `APLD_*.png` files from each graph folder before saving the new current set.

Keep `save_trade_log = True` to save one row per executed trade to `trade_log.csv`.

Run `quote_spread_check.py` after the trade log is created to compare each trade against Alpaca IEX quote data:

```bash
python3 quote_spread_check.py
```

That creates `quote_spread_check.csv` with bid/ask quotes from a tight 5-minute window around each buy and sell time, spread %, quote size, whether the backtest limit price appeared available inside the quote window, estimated slippage, and whether the visible quote size covered your estimated share amount. It then prints the same period summary table as `intraday.py`, plus the three quote-check columns.

The quote checker treats quote size as shares by default. If your feed returns quote size in round lots, run it with `--quote-size-multiplier 100`.

The quote checker reuses existing rows in `quote_spread_check.csv`. Use `--max-workers 8` to try a faster run, or lower it if Alpaca gives rate-limit errors.

If `include_quote_check_in_summary = True`, the period summary in `intraday.py` will also include three quote-check columns after `quote_spread_check.csv` has been created: `estimated_slippage_amount`, `buy_visible_share_coverage_pct`, and `sell_visible_share_coverage_pct`.

Keep `print_period_summary = True` to print the same performance stats in a table split by `summary_period_months`. For example, `summary_period_months = 1` prints one row per month, while `summary_period_months = 2` prints two-month periods.

Keep `simulate_real_trading = True` to add dollar-based results to the trade log and print a real trading simulation summary for the configured summary period. `starting_money` is the account size, `money_per_trade` is the amount used on each trade, and `trading_cost` is the cost subtracted from each completed trade.

When real trading simulation is on, the period summary table also includes net P/L, ending money, and total trading cost for each period.

The real trading simulation starts from `starting_money` at `summary_start`, then carries the balance forward through each trade in the summary period.

## Code Layout

The strategy is split into focused steps:

- `fetch_bars()` loads Alpaca data.
- `add_base_features()` and `add_gap_features()` calculate reusable columns.
- `add_buy_signals()` defines the setup, confirmation candle, and execution candle.
- `add_sell_signals()` defines the exit logic.
- `add_trade_returns()` calculates returns.
- `create_trade_log()` converts the candle data into one row per summary-period day.
- `add_real_trading_simulation()` adds money used, shares, trading cost, net P/L, and ending money to the trade log.
- `summarize_performance()` returns performance stats that can be reused in comparisons.
- `create_period_summary_table()` summarizes the same stats across repeated month-based periods.
- `plot_trade_day()` and `plot_all_trade_days()` save candlestick charts.
- `copy_trade_days_to_folder()` copies already-created chart files into extra folders for negative-return trades and unexecuted setups.

## Output

Chart files are saved as:

```text
trade graph/APLD_YYYY-MM-DD.png
```

Filtered chart folders are also created when matching dates exist:

```text
negative trade graph/APLD_YYYY-MM-DD.png
non_executed trade graph/APLD_YYYY-MM-DD.png
```

The unexecuted setup folder includes setup days that did not produce a buy price, plus setup days that bought but did not produce a sell.

The trade log is saved as:

```text
trade_log.csv
```

It includes date, setup time, buy time, buy price, sell time, sell price, return, exit type, max possible return after buy, and daily high after buy.

Each chart marks:

- Candlesticks for the regular trading session.
- First buy signal.
- Highest price after the buy signal.
- First sell signal.

## Notes

Keep real Alpaca credentials out of Git. Use `.env.example` as a template, but do not commit a real `.env` file.
