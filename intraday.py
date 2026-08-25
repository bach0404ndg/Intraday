from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from datetime import time
from datetime import date
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import os

from matplotlib.patches import Rectangle

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    raise RuntimeError(
        "Set ALPACA_API_KEY and ALPACA_SECRET_KEY before running this script."
    )

stock_data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

request = StockBarsRequest(
    symbol_or_symbols="APLD",
    timeframe=TimeFrame(5, TimeFrameUnit.Minute),
    start="2026-06-22",
    end="2026-08-15"
)

bars = stock_data_client.get_stock_bars(request)

#reset special index in SBR
df= bars.df.reset_index()

df["timestamp"] = df["timestamp"].dt.tz_convert("America/New_York")

def label_session(timestamp):
    t = timestamp.time()

    if time(4, 0) <= t < time(9, 30):
        return "pre_market"

    elif time(9, 30) <= t < time(16, 0):
        return "regular"

    elif time(16, 0) <= t < time(20, 0):
        return "after_hours"

    else:
        return "overnight"

#creating new variables: session, body, range,date,red,green

df["session"] = df["timestamp"].apply(label_session)
df["body"] = abs(df["close"] - df["open"])
df["range"] = df["high"] - df["low"]
df["body_pct"] = df["body"] / df["open"]
df["range_pct"] = df["range"] / df["open"]
df["date"] = df["timestamp"].dt.date

df["candle_color"] = np.where(
    df["close"] > df["open"],
    "green",
    np.where(
        df["close"] < df["open"],
        "red",
        "orange"
    )
)

#create variables gap

open_930 = (
    df[df["timestamp"].dt.time == time(9, 30)]
    .set_index("date")["open"]
)

close_4 = (
    df[df["timestamp"].dt.time == time(15, 55)]
    .set_index("date")["close"]
)

daily_change = (
    close_4 - open_930
) / open_930

prev_close_4 = close_4.shift(1)
opening_gap = (open_930 - prev_close_4) / prev_close_4
df["opening_gap"] = df["date"].map(opening_gap)
df["prev_close"] = df["date"].map(prev_close_4)
df["current_gap"] = (df["close"] - df["prev_close"]) / df["prev_close"]
df["gap_change"] = df.groupby("date")["current_gap"].diff()
df["daily_change"] = df["date"].map(daily_change)


# DEFINE BUY SIGNAL

REGRESS_NUM = 7
BODY_NUM = 7
QUANTILE = 0.40

#10 = 10:20 start
regular_df = df[df["session"] == "regular"].copy()

regular_df["daily_high"] = (
    regular_df.groupby("date")["high"].transform(lambda x: x.max())
)
#small body by doji
regular_df["avg_range_pct"] = (
    regular_df.groupby("date")["range_pct"]
      .transform(lambda x: x.shift(1).rolling(BODY_NUM).mean())
)

regular_df["doji_body"] = (
    regular_df["body_pct"] < 0.10 * regular_df["avg_range_pct"]
)

#small range by quantile
regular_df["range_quantile"] = (
    regular_df.groupby("date")["range_pct"]
      .transform(lambda x: x.shift(1).rolling(BODY_NUM).quantile(QUANTILE))
)

regular_df["small_range"] = (
    regular_df["range_pct"] < regular_df["range_quantile"]
)

# SLOPE OF PREVIOU X ENTRY

def price_slope(x):
    t = np.arange(len(x))
    return np.polyfit(t, x, 1)[0]

regular_df["slope"] = (
    regular_df.groupby("date")["close"]
      .transform(
          lambda x: x.shift(1).rolling(REGRESS_NUM).apply(price_slope, raw=True)
      )
)

regular_df["next_candle_color"] = (
    regular_df.groupby("date")["candle_color"].shift(-1)
)

regular_df["buy_signal"] = (
    (regular_df["slope"] < 0) &
    regular_df["doji_body"] &
    #regular_df["small_range"] &
(regular_df["next_candle_color"] == "green")&
    (
        regular_df.groupby("date")["timestamp"]
        .shift(-2)
        .dt.time < time(14, 0)
    )
)


regular_df["first_buy_signal"] = (
    regular_df["buy_signal"] &
    (regular_df.groupby("date")["buy_signal"].cumsum() == 1)
)

#buy time after witnessing the 2 signal candles
regular_df["buy_price"] = np.where(
    regular_df["buy_signal"],
    regular_df.groupby("date")["close"].shift(-2),
    np.nan
)

regular_df["buy_time"] = regular_df.groupby("date")["timestamp"].shift(-2).where(
    regular_df["buy_signal"]
)

# DEFINE BEST RETURN
regular_df["daily_high_after_buy"] = regular_df.apply(
    lambda row: regular_df.loc[
        (regular_df["date"] == row["date"]) &
        (regular_df["timestamp"] > row["buy_time"]),
        "high"
    ].max()
    if row["buy_signal"] else np.nan,
    axis=1
)

regular_df["daily_first_buy_time"] = (
     regular_df.groupby("date")["buy_time"].transform("first")
)

regular_df["after_buy"] = (
    regular_df["timestamp"] >= regular_df["daily_first_buy_time"]
)

regular_df["high_since_buy"] = (
    regular_df["high"]
        .where(regular_df["after_buy"])
        .groupby(regular_df["date"])
        .cummax()
)

regular_df["previous_high"] = (
    regular_df.groupby("date")["high"].shift(1)
)

regular_df["previous_high_since_buy"] = (
    regular_df.groupby("date")["high_since_buy"].shift(1)
)

# DEFINE SELL SIGNAL
NEAR_HIGH_PCT = 0.002
REGRESS_NUM_SELL = 7

regular_df["near_high"] = (
    regular_df["previous_high"] >=
    regular_df["previous_high_since_buy"] * (1 - NEAR_HIGH_PCT)
)

regular_df["slope_sell"] = (
    regular_df.groupby("date")["close"]
      .transform(
          lambda x: x.shift(1).rolling(REGRESS_NUM_SELL).apply(price_slope, raw=True)
      )
)

regular_df["previous_slope_sell"] = (
    regular_df.groupby("date")["slope_sell"].shift(1)
)

regular_df["sell_regression_ready"] = (
    regular_df["timestamp"] >= regular_df["daily_first_buy_time"]
    + pd.Timedelta(minutes=5 * REGRESS_NUM_SELL + 1)
)

regular_df["sell_signal"] = (
    regular_df["sell_regression_ready"] &
    (regular_df["slope_sell"] > 0) &
    (regular_df["slope_sell"] < regular_df["previous_slope_sell"]) &
    (
        regular_df["near_high"] |
        (regular_df["timestamp"].dt.time >= time(15, 0))
    )
)

regular_df["sell_price"] = (
    regular_df["open"].where(regular_df["sell_signal"])
)

regular_df["sell_time"] = (
    regular_df["timestamp"].where(regular_df["sell_signal"])
)

regular_df["first_sell_signal"] = (
    regular_df["sell_signal"] &
    (regular_df.groupby("date")["sell_signal"].cumsum() == 1)
)

# GRAPHING
def plot_trade_day(selected_date, folder, show=True):

    # select one trading day
    day_df = regular_df[
        regular_df["date"].astype(str) == selected_date
    ].copy()

    if day_df.empty:
        print("No data for this date.")
        return

    fig, ax = plt.subplots(figsize=(20, 12))

    # draw candlesticks
    for _, row in day_df.iterrows():

        x = mdates.date2num(row["timestamp"])
        color = row["candle_color"]


        # wick
        ax.plot(
            [x, x],
            [row["low"], row["high"]],
            color=color,
            linewidth=1
        )

        # body
        body_bottom = min(row["open"], row["close"])
        body_height = row["body"]

        ax.add_patch(
            Rectangle(
                (x - 0.0012, body_bottom),
                0.0024,
                max(body_height, 0.001),
                facecolor=color,
                edgecolor=color
            )
        )

    # MARK BUY CANDLE
    buy_rows = day_df[day_df["first_buy_signal"]]

    for _, row in buy_rows.iterrows():

        buy_time = row["buy_time"]
        buy_price = row["buy_price"]
        buy_candle_low = day_df.loc[
            day_df["timestamp"] == buy_time,
            "low"
        ].iloc[0]


        ax.scatter(
            buy_time,
            buy_candle_low - 0.05,
            marker="^",
            s=150,
            zorder=5,
            color="black"
        )

        label = f"{buy_time.strftime('%H:%M')}\n${buy_price:.2f}"

        ax.annotate(
            label,
            (buy_time, buy_candle_low - 0.05),
            xytext=(0, -10),
            textcoords="offset points",
            ha="center",
            va="top"
        )

        # MARK DAILY HIGH AFTER BUY
        high_after_buy_price = row["daily_high_after_buy"]

        if pd.notna(high_after_buy_price):
            high_after_buy_time = day_df.loc[
                (day_df["timestamp"] > buy_time) &
                (day_df["high"] == high_after_buy_price),
                "timestamp"
            ].iloc[0]

        ax.scatter(
            high_after_buy_time,
            high_after_buy_price + 0.05,
            marker="v",
            s=150,
            zorder=5,
            color="black"
        )

        label = f"{high_after_buy_time.strftime('%H:%M')}\n${high_after_buy_price:.2f}"

        ax.annotate(
            label,
            (high_after_buy_time, high_after_buy_price + 0.05),
            xytext=(0, 10),
            textcoords="offset points",
            ha="center",
            va="bottom"
        )

        # MARK SELL SIGNAL
        buy_rows = day_df[day_df["first_sell_signal"]]

        for _, row in buy_rows.iterrows():
            sell_time = row["sell_time"]
            sell_price = row["sell_price"]
            candle_high = day_df.loc[
                day_df["timestamp"] == sell_time,
                "high"
            ].iloc[0]

            ax.scatter(
                sell_time,
                candle_high + 0.05,
                marker="v",
                s=150,
                zorder=5,
                color="blue"
            )

            label = f"{sell_time.strftime('%H:%M')}\n${sell_price:.2f}"

            ax.annotate(
                label,
                (sell_time, candle_high + 0.05),
                xytext=(0, 10),
                textcoords="offset points",
                ha="center",
                va="bottom"
            )

    # time axis
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M",
                             tz="America/New_York")
    )

    ax.xaxis.set_major_locator(
        mdates.MinuteLocator(byminute=[0,30],
                             tz="America/New_York")
    )

    plt.xticks(rotation=45)

    ax.set_title(f"{selected_date}")
    ax.set_xlabel("Time")
    ax.set_ylabel("Price")

    plt.tight_layout()

    plt.savefig(
        f"{folder}/APLD_{selected_date}.png",
        dpi=150,
        bbox_inches="tight"
    )

    if show:
        plt.show()
    else:
        plt.close()

def plot_all_dates(folder):

    all_dates = regular_df["date"].unique()

    for date in all_dates:

        plot_trade_day(str(date), folder= folder, show=False)

plot_all_dates("trade graph")


# #CHECK STRONG BUY CONDITION
#
# qqq_request = StockBarsRequest(
#     symbol_or_symbols="QQQ",
#     timeframe=TimeFrame(5, TimeFrameUnit.Minute),
#     start= "2026-06-22",
#     end="2026-08-14"
# )
#
# qqq_df = stock_data_client.get_stock_bars(qqq_request).df.reset_index()
#
# qqq_df["timestamp"] = (
#     qqq_df["timestamp"].dt.tz_convert("America/New_York")
# )
#
# qqq_df["date"] = qqq_df["timestamp"].dt.date
#
# qqq_df = qqq_df[
#     (qqq_df["timestamp"].dt.time >= time(9, 30)) &
#     (qqq_df["timestamp"].dt.time < time(16, 0))
# ].copy()
#
# qqq_open = (
#     qqq_df[qqq_df["timestamp"].dt.time == time(9, 30)]
#     .set_index("date")["open"]
# )
#
# qqq_df["qqq_return"] = (
#     qqq_df["close"] - qqq_df["date"].map(qqq_open)
# ) / qqq_df["date"].map(qqq_open)
#
# regular_df = regular_df.merge(
#     qqq_df[["timestamp", "qqq_return"]],
#     on="timestamp",
#     how="left"
# )

# CHECK MAX RETURN
regular_df["max_return"] = (
        (regular_df["daily_high_after_buy"] - regular_df["buy_price"]) / regular_df["buy_price"]
).where(regular_df["first_buy_signal"])

regular_df["first_buy_price"] = regular_df.groupby("date")["buy_price"].transform("first")

regular_df["trade_return"] = (
    (regular_df["sell_price"] - regular_df["first_buy_price"]) /
regular_df["first_buy_price"]
).where(regular_df["first_sell_signal"])



# CHECK FAILED SIGNAL
buy_days = regular_df.groupby("date")["buy_signal"].any()
sell_days = regular_df.groupby("date")["sell_signal"].any()

buy_without_sell = buy_days & ~sell_days
bad_dates = buy_without_sell[buy_without_sell].index

print(bad_dates)

def setup_hit_rate(start_date=None, end_date=None):

    data = regular_df

    if start_date is not None:
        data = data[data["date"] >= start_date]

    if end_date is not None:
        data = data[data["date"] <= end_date]

    buy_days = data.groupby("date")["buy_signal"].any()
    sell_days = data.groupby("date")["sell_signal"].any()

    successful = buy_days & sell_days

    total_days = data["date"].nunique()
    total_buys = buy_days.sum()
    total_success = successful.sum()

    execution_rate = total_success/ total_days * 100

    returns = data["trade_return"].dropna()

    negative_returns = (returns < 0).sum()
    positive_returns = (returns > 0).sum()

    negative_rate = negative_returns / len(returns) * 100
    win_rate = positive_returns / len(returns) * 100

    avg_return = returns.mean() * 100
    median_return = returns.median() * 100

    avg_win = returns[returns > 0].mean() * 100
    avg_loss = returns[returns < 0].mean() * 100

    best_trade = returns.max() * 100
    worst_trade = returns.min() * 100

    hit_rate = total_success / total_buys * 100

    print("Total days:", total_days)
    print("Buy days:", total_buys)
    print("Successful days:", total_success)

    print("\nExecution rate:", round(execution_rate, 2), "%")
    print("Sell hit rate:", round(hit_rate, 2), "%")
    print("Win rate:", round(win_rate, 2), "%")
    print("Negative rate:", round(negative_rate, 2), "%")

    print("\nAverage return:", round(avg_return, 3), "%")
    print("Median return:", round(median_return, 3), "%")

    print("Average win:", round(avg_win, 3), "%")
    print("Average loss:", round(avg_loss, 3), "%")

    print("Best trade:", round(best_trade, 3), "%")
    print("Worst trade:", round(worst_trade, 3), "%")




setup_hit_rate(
    start_date=date(2026, 6, 22),
    end_date=date(2026, 8, 14)
)
