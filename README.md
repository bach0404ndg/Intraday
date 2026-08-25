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

Install dependencies:

```bash
pip install alpaca-py pandas numpy matplotlib
```

## Usage

Set your Alpaca credentials before running the script:

```bash
export ALPACA_API_KEY="your_alpaca_api_key"
export ALPACA_SECRET_KEY="your_alpaca_secret_key"
```

Run the script from the project root:

```bash
python intraday.py
```

The script currently:

- Requests 5-minute APLD stock bars from Alpaca.
- Converts timestamps to `America/New_York`.
- Separates pre-market, regular, after-hours, and overnight sessions.
- Calculates candle body/range metrics, opening gaps, slopes, buy signals, sell signals, and trade returns.
- Saves daily chart images to `trade graph/`.
- Prints summary statistics such as execution rate, sell hit rate, win rate, average return, best trade, and worst trade.

## Strategy Parameters

Key parameters are defined in `intraday.py`:

```python
REGRESS_NUM = 7
BODY_NUM = 7
QUANTILE = 0.40
NEAR_HIGH_PCT = 0.002
REGRESS_NUM_SELL = 7
```

Adjust these values to test different signal behavior.

## Output

Chart files are saved as:

```text
trade graph/APLD_YYYY-MM-DD.png
```

Each chart marks:

- Candlesticks for the regular trading session.
- First buy signal.
- Highest price after the buy signal.
- First sell signal.

## Notes

Keep real Alpaca credentials out of Git. Use `.env.example` as a template, but do not commit a real `.env` file.
