from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import statsmodels.api as sm

from intraday import GAP_TRADE_LOG_COLUMNS


@dataclass(frozen=True)
class AnalysisConfig:
    trade_log_path: Path = Path("trade_log.csv")
    x_cols: tuple = tuple(GAP_TRADE_LOG_COLUMNS)
    y_col: str = "trade_return_pct"


def load_trade_log(path):
    trade_log = pd.read_csv(path)

    return trade_log


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

    model = run_regression(
        trade_log,
        config.y_col,
        config.x_cols
    )

    print(model.summary())


if __name__ == "__main__":
    main()
