from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import statsmodels.api as sm

from intraday import GAP_TRADE_LOG_COLUMNS, EXTRA_TRADE_LOG_COLUMNS

TREND_INTERACTION_COLUMN = "trend_recent_high_now_x_trend_open_recent_high"


@dataclass(frozen=True)
class AnalysisConfig:
    trade_log_path: Path = Path("trade_log.csv")
    x_cols: tuple[str, ...] = (
            tuple(GAP_TRADE_LOG_COLUMNS) +
            tuple(EXTRA_TRADE_LOG_COLUMNS) +
            (TREND_INTERACTION_COLUMN,)
    )
    y_col: str = "trade_return_pct"


def load_trade_log(path):
    trade_log = pd.read_csv(path)

    return trade_log


def add_interaction_terms(df):
    df = df.copy()
    df[TREND_INTERACTION_COLUMN] = (
        df["trend_recent_high_now"] * df["trend_open_recent_high"]
    )

    return df


def run_regression(df, y_col, x_cols):
    X = df[list(x_cols)]
    y = df[y_col]

    X = sm.add_constant(X)

    model = sm.OLS(
        y,
        X,
        missing="drop"
    ).fit()

    return model


def main():
    config = AnalysisConfig()

    trade_log = load_trade_log(
        config.trade_log_path
    )
    trade_log = add_interaction_terms(trade_log)

    model = run_regression(
        trade_log,
        config.y_col,
        config.x_cols
    )

    print(model.summary())


if __name__ == "__main__":
    main()
