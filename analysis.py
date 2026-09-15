from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

from intraday import (
    StrategyConfig,
    create_trade_log,
    fetch_bars,
    run_strategy_on_data,
    summarize_trading,
    trading_simulation,
)


STRATEGY_NAMES = {
    "normal": "catch_bottom",
    "momentum": "momentum",
}

PARAMETER_FLOAT_STEPS = {
    "doji_body_range_ratio": 0.02,
    "momentum_doji_body_range_ratio": 0.02,
    "near_high_pct": 0.0005,
    "near_high_pct_momentum": 0.0005,
    "early_take_profit_pct": 0.005,
    "early_take_profit_pct_momentum": 0.005,
    "stop_loss_pct": 0.0025,
    "support_break_pct": 0.0025,
    "momentum_stop_loss_from_high_pct": 0.0025,
}

PARAMETER_FLOAT_LIMITS = {
    "doji_body_range_ratio": (0.02, 0.25),
    "momentum_doji_body_range_ratio": (0.02, 0.25),
    "near_high_pct": (0.0, 0.005),
    "near_high_pct_momentum": (0.0, 0.005),
    "early_take_profit_pct": (0.005, 0.05),
    "early_take_profit_pct_momentum": (0.005, 0.05),
    "stop_loss_pct": (0.0025, 0.03),
    "support_break_pct": (0.0, 0.03),
    "momentum_stop_loss_from_high_pct": (0.0025, 0.03),
}

PARAMETER_CUSTOM_VALUES = {
    "buy_regression_bars": (3, 4, 5, 6, 7, 8),
    "momentum_buy_regression_bars": (3, 4, 5, 6, 7, 8),
    "body_average_bars": (5, 6, 7, 8, 9, 10),
    "momentum_body_average_bars": (5, 6, 7, 8, 9, 10),
    "momentum_latest_buy_time": (
        time(13, 30),
        time(13, 45),
        time(14, 0),
        time(14, 15),
        time(14, 30),
    ),
    "near_high_bars": (1, 2, 3, 4),
    "near_high_bars_momentum": (1, 2, 3, 4),
    "sell_regression_bars": (8, 9, 10, 11, 12, 13),
    "sell_regression_bars_momentum": (2, 3, 4, 5, 6, 7),
    "support_bars": (10, 13, 16, 19, 22, 25),
    "stop_loss_bars": (1, 2, 3, 4, 5),
    "stop_loss_bars_momentum": (1, 2, 3, 4, 5),
    "force_exit_time": (
        time(15, 0),
        time(15, 15),
        time(15, 30),
        time(15, 45),
        time(15, 55),
    ),
    "force_exit_time_momentum": (
        time(15, 0),
        time(15, 15),
        time(15, 30),
        time(15, 45),
        time(15, 55),
    ),
}

NORMAL_PARAMETER_SEARCH_PARAMS = {
    "buy_regression_bars",
    "body_average_bars",
    "doji_body_range_ratio",
    "latest_buy_time",
    "near_high_pct",
    "near_high_bars",
    "sell_regression_bars",
    "support_bars",
    "stop_loss_pct",
    "support_break_pct",
    "use_stop_loss",
    "stop_loss_bars",
    "force_exit_time",
}

MOMENTUM_PARAMETER_SEARCH_PARAMS = {
    "momentum_buy_regression_bars",
    "momentum_body_average_bars",
    "momentum_doji_body_range_ratio",
    "momentum_latest_buy_time",
    "near_high_pct_momentum",
    "near_high_bars_momentum",
    "sell_regression_bars_momentum",
    "momentum_stop_loss_from_high_pct",
    "use_stop_loss_momentum",
    "stop_loss_bars_momentum",
    "force_exit_time_momentum",
}


@dataclass(frozen=True)
class AnalysisConfig:
    trade_log_path: Path = Path("trade_log.csv")
    y_col: str = "trade_return_pct"
    bucket_count: int = 5
    min_trades: int = 5
    loss_penalty: float = 1.0
    top_thresholds: int = 20
    run_regression: bool = False
    run_parameter_search: bool = False
    search_use_buy_momentum: bool = True
    parameter_search_max_combinations: int = 100
    parameter_search_random_seed: int = 7
    parameter_search_min_trades: int = 20
    selected_parameter_index: int = 67
    parameter_search_params: tuple[str, ...] = (
        "buy_regression_bars",
        "body_average_bars",
        "doji_body_range_ratio",
        "latest_buy_time",
        "momentum_buy_regression_bars",
        "momentum_body_average_bars",
        "momentum_doji_body_range_ratio",
        "momentum_latest_buy_time",
        "near_high_pct",
        "near_high_bars",
        "near_high_pct_momentum",
        "near_high_bars_momentum",
        "sell_regression_bars",
        "sell_regression_bars_momentum",
        "support_bars",
        "stop_loss_pct",
        "support_break_pct",
        "momentum_stop_loss_from_high_pct",
        "use_stop_loss",
        "use_stop_loss_momentum",
        "stop_loss_bars",
        "stop_loss_bars_momentum",
        "force_exit_time",
        "force_exit_time_momentum",
    )
    bucket_columns: tuple[str, ...] = (
        "buy_slope",
        "body_pct",
        "range_pct",
        "opening_gap",
        "current_gap",
        "gap_change",
        "downstream_decline",
        "downstream_trend_slope",
        "downstream_trend_r2",
        "momentum_slope",
        "previous_momentum_slope",
        "momentum_slope_change",
        "momentum_avg_range_pct",
    )
    parameter_indicator_columns: tuple[str, ...] = (
        "opening_gap",
        "current_gap",
        "gap_change",
        "buy_slope",
        "body_pct",
        "range_pct",
        "downstream_decline",
        "downstream_trend_slope",
        "downstream_trend_r2",
        "momentum_slope",
        "previous_momentum_slope",
        "momentum_slope_change",
        "momentum_avg_range_pct",
    )


def load_trade_log(path):
    trade_log = pd.read_csv(path)

    if "date" in trade_log.columns:
        trade_log["date"] = pd.to_datetime(
            trade_log["date"],
            errors="coerce",
        ).dt.date

    return trade_log


def completed_trades(trade_log, y_col):
    return (
        trade_log[
            trade_log[y_col].notna()
        ]
        .copy()
    )


def display_strategy_name(buy_type):
    return STRATEGY_NAMES.get(
        buy_type,
        buy_type,
    )


def summarize_group(group, y_col):
    losses = group[
        group[y_col] < 0
    ]

    return {
        "trades": len(group),
        "win_rate": (
            group[y_col] > 0
        ).mean(),
        "negative_rate": (
            group[y_col] < 0
        ).mean(),
        "median_return": group[y_col].median(),
        "average_return": group[y_col].mean(),
        "average_loss": losses[y_col].mean(),
        "worst_return": group[y_col].min(),
        "best_return": group[y_col].max(),
        "net_pnl": group["net_pnl"].sum()
        if "net_pnl" in group.columns
        else np.nan,
    }


def strategy_summary(trade_log, config):
    trades = completed_trades(
        trade_log,
        config.y_col,
    )

    rows = []

    for buy_type, group in trades.groupby("buy_type"):
        rows.append({
            "buy_type": display_strategy_name(buy_type),
            **summarize_group(
                group,
                config.y_col,
            ),
        })

    return pd.DataFrame(rows)


def score_summary(summary, config):
    average_loss = summary["average_loss"]

    if pd.isna(average_loss):
        average_loss = 0

    return (
        summary["median_return"]
        - abs(average_loss)
        * summary["negative_rate"]
        * config.loss_penalty
    )


def available_columns(df, columns):
    return [
        column
        for column in columns
        if column in df.columns
    ]


def unique_numeric_columns(df, columns):
    unique_columns = []

    for column in available_columns(df, columns):
        if not pd.api.types.is_numeric_dtype(df[column]):
            continue

        values = df[column]

        if values.dropna().nunique() <= 1:
            continue

        duplicate_found = False

        for kept_column in unique_columns:
            comparison = df[
                [
                    column,
                    kept_column,
                ]
            ].dropna()

            if comparison.empty:
                continue

            if np.allclose(
                comparison[column],
                comparison[kept_column],
                equal_nan=True,
            ):
                duplicate_found = True
                break

        if not duplicate_found:
            unique_columns.append(column)

    return unique_columns


def bucket_analysis(trade_log, buy_type, variable, config):
    trades = completed_trades(
        trade_log,
        config.y_col,
    )

    trades = trades[
        trades["buy_type"] == buy_type
    ].copy()

    trades = trades[
        trades[variable].notna()
    ]

    if len(trades) < config.min_trades:
        return pd.DataFrame()

    trades["bucket"] = pd.qcut(
        trades[variable],
        q=config.bucket_count,
        duplicates="drop",
    )

    rows = []

    for bucket, group in trades.groupby(
        "bucket",
        observed=True,
    ):
        if len(group) < config.min_trades:
            continue

        rows.append({
            "buy_type": display_strategy_name(buy_type),
            "variable": variable,
            "bucket": str(bucket),
            **summarize_group(
                group,
                config.y_col,
            ),
        })

    return pd.DataFrame(rows)


def all_bucket_analysis(trade_log, config):
    tables = []

    for buy_type in sorted(
        trade_log["buy_type"].dropna().unique()
    ):
        trades = completed_trades(
            trade_log,
            config.y_col,
        )

        trades = trades[
            trades["buy_type"] == buy_type
        ]

        for variable in unique_numeric_columns(
            trades,
            config.bucket_columns,
        ):
            table = bucket_analysis(
                trade_log,
                buy_type,
                variable,
                config,
            )

            if not table.empty:
                tables.append(table)

    if not tables:
        return pd.DataFrame()

    return pd.concat(
        tables,
        ignore_index=True,
    )


def threshold_search(trade_log, buy_type, variable, config):
    trades = completed_trades(
        trade_log,
        config.y_col,
    )

    trades = trades[
        trades["buy_type"] == buy_type
    ].copy()

    trades = trades[
        trades[variable].notna()
    ]

    if len(trades) < config.min_trades:
        return pd.DataFrame()

    cutoffs = (
        trades[variable]
        .quantile(
            np.arange(
                0.1,
                1.0,
                0.1,
            )
        )
        .drop_duplicates()
    )

    rows = []

    for cutoff in cutoffs:
        rules = {
            "<=": trades[
                trades[variable] <= cutoff
            ],
            ">=": trades[
                trades[variable] >= cutoff
            ],
        }

        for operator, group in rules.items():
            if len(group) < config.min_trades:
                continue

            summary = summarize_group(
                group,
                config.y_col,
            )

            rows.append({
                "buy_type": display_strategy_name(buy_type),
                "rule": f"{variable} {operator} {cutoff:.6f}",
                "kept_trades": len(group),
                "excluded_trades": len(trades) - len(group),
                "score": score_summary(
                    summary,
                    config,
                ),
                **summary,
            })

    return pd.DataFrame(rows)


def all_threshold_search(trade_log, config):
    tables = []

    for buy_type in sorted(
        trade_log["buy_type"].dropna().unique()
    ):
        trades = completed_trades(
            trade_log,
            config.y_col,
        )

        trades = trades[
            trades["buy_type"] == buy_type
        ]

        for variable in unique_numeric_columns(
            trades,
            config.bucket_columns,
        ):
            table = threshold_search(
                trade_log,
                buy_type,
                variable,
                config,
            )

            if not table.empty:
                tables.append(table)

    if not tables:
        return pd.DataFrame()

    return pd.concat(
        tables,
        ignore_index=True,
    ).sort_values(
        "score",
        ascending=False,
    )


def regression_columns(trade_log, config):
    return unique_numeric_columns(
        trade_log,
        [
            column
            for column in config.bucket_columns
            if column != config.y_col
        ],
    )


def run_regression(trade_log, buy_type, config):
    trades = completed_trades(
        trade_log,
        config.y_col,
    )

    trades = trades[
        trades["buy_type"] == buy_type
    ].copy()

    x_cols = regression_columns(
        trades,
        config,
    )

    if len(trades) < config.min_trades or not x_cols:
        return None

    regression_data = trades[
        [config.y_col, *x_cols]
    ].dropna()

    if len(regression_data) <= len(x_cols) + 2:
        return None

    X = sm.add_constant(
        regression_data[x_cols],
        has_constant="add",
    )

    y = regression_data[config.y_col]

    return sm.OLS(
        y,
        X,
        missing="drop",
    ).fit()


def format_table(df):
    if df.empty:
        return df

    display = df.copy()

    fixed_pct_columns = [
        "win_rate",
        "negative_rate",
        "median_return",
        "average_return",
        "average_loss",
        "worst_return",
        "best_return",
        "score",
    ]

    pct_columns = [
        column
        for column in display.columns
        if (
            column in fixed_pct_columns
            or column.endswith("_rate")
            or column.endswith("_return")
            or "_pct" in column
            or "gap" in column
            or "slope" in column
            or "decline" in column
        )
    ]

    for column in pct_columns:
        if column in display.columns:
            display[column] = (
                display[column]
                * 100
            ).round(3)

    if "net_pnl" in display.columns:
        display["net_pnl"] = (
            display["net_pnl"]
            .round(2)
        )

    return display


def print_table(title, df):
    print(f"\n{title}")

    if df.empty:
        print("No rows.")
        return

    print(
        format_table(df)
        .to_string(index=False)
    )


def print_regressions(trade_log, config):
    if not config.run_regression:
        return

    for buy_type in sorted(
        trade_log["buy_type"].dropna().unique()
    ):
        model = run_regression(
            trade_log,
            buy_type,
            config,
        )

        if model is None:
            continue

        print(
            "\nRegression:",
            display_strategy_name(buy_type),
        )
        print(model.summary())


def shift_clock_time(clock_time, minutes):
    shifted = (
        datetime.combine(
            datetime.today(),
            clock_time,
        )
        + timedelta(minutes=minutes)
    )

    return shifted.time().replace(
        second=0,
        microsecond=0,
    )


def integer_values_around(value):
    values = [
        value + offset
        for offset in (-3, -2, -1, 0, 1, 2)
    ]

    return sorted({
        max(1, int(candidate))
        for candidate in values
    })


def clamp(value, lower, upper):
    return min(
        max(
            value,
            lower,
        ),
        upper,
    )


def float_values_around(parameter, value):
    if parameter in PARAMETER_FLOAT_STEPS:
        step = PARAMETER_FLOAT_STEPS[parameter]
        lower, upper = PARAMETER_FLOAT_LIMITS[parameter]

        values = [
            clamp(
                value + step * offset,
                lower,
                upper,
            )
            for offset in (-3, -2, -1, 0, 1, 2)
        ]

    elif value == 0:
        values = [
            -0.002,
            -0.001,
            0,
            0.001,
            0.002,
            0.003,
        ]

    else:
        step = abs(value) * 0.15
        values = [
            value + step * offset
            for offset in (-3, -2, -1, 0, 1, 2)
        ]

    return sorted({
        round(float(candidate), 6)
        for candidate in values
    })


def time_values_around(value):
    return sorted({
        shift_clock_time(
            value,
            minutes,
        )
        for minutes in (
            -30,
            -15,
            0,
            15,
            30,
            45,
        )
    })


def values_around(parameter, value):
    if parameter in PARAMETER_CUSTOM_VALUES:
        return PARAMETER_CUSTOM_VALUES[parameter]

    if isinstance(value, bool):
        return [value]

    if parameter in PARAMETER_FLOAT_STEPS:
        return float_values_around(
            parameter,
            float(value),
        )

    if isinstance(value, int):
        return integer_values_around(value)

    if isinstance(value, float):
        return float_values_around(
            parameter,
            value,
        )

    if hasattr(value, "hour") and hasattr(value, "minute"):
        return time_values_around(value)

    return [value]


def parameter_search_params(config):
    if config.search_use_buy_momentum:
        active_parameters = MOMENTUM_PARAMETER_SEARCH_PARAMS
    else:
        active_parameters = NORMAL_PARAMETER_SEARCH_PARAMS

    return [
        parameter
        for parameter in config.parameter_search_params
        if parameter in active_parameters
    ]


def parameter_search_buy_type(config):
    if config.search_use_buy_momentum:
        return "momentum"

    return "normal"


def base_strategy_config(config):
    return replace(
        StrategyConfig(),
        plot_graphs=False,
        use_buy_momentum=config.search_use_buy_momentum,
    )


def parameter_grid(base_config, config):
    grid = {}

    for parameter in parameter_search_params(config):
        grid[parameter] = values_around(
            parameter,
            getattr(
                base_config,
                parameter,
            )
        )

    return grid


def parameter_values_for_index(parameter_index, base_config, config):
    grid = parameter_grid(
        base_config,
        config,
    )

    for current_index, parameters in enumerate(
        sample_parameter_combinations(
            grid,
            base_config,
            config,
        ),
        start=1,
    ):
        if current_index == parameter_index:
            return parameters

    raise ValueError(
        f"Parameter index {parameter_index} was not found."
    )


def strategy_config_from_parameter_index(parameter_index, config):
    lookup_config = replace(
        config,
        parameter_search_max_combinations=max(
            config.parameter_search_max_combinations,
            parameter_index,
        ),
    )

    base_config = base_strategy_config(
        lookup_config,
    )

    parameters = parameter_values_for_index(
        parameter_index,
        base_config,
        lookup_config,
    )

    return replace(
        base_config,
        **parameters,
    )


def summarize_parameter_indicators(trades, config):
    summary = {}

    for column in config.parameter_indicator_columns:
        if column not in trades.columns:
            continue

        summary[f"avg_{column}"] = trades[column].mean()

    for buy_type, display_name in STRATEGY_NAMES.items():
        buy_type_trades = trades[
            trades["buy_type"] == buy_type
        ]

        summary[f"{display_name}_trades"] = len(
            buy_type_trades
        )

        summary[f"{display_name}_median_return"] = (
            buy_type_trades[config.y_col].median()
            if not buy_type_trades.empty
            else np.nan
        )

        summary[f"{display_name}_negative_rate"] = (
            (buy_type_trades[config.y_col] < 0).mean()
            if not buy_type_trades.empty
            else np.nan
        )

    return summary


def sample_parameter_combinations(grid, base_config, config):
    parameter_names = list(grid)
    total_combinations = int(
        np.prod([
            len(values)
            for values in grid.values()
        ])
    )

    if total_combinations <= config.parameter_search_max_combinations:
        for values in product(
            *[
                grid[name]
                for name in parameter_names
            ]
        ):
            yield dict(
                zip(
                    parameter_names,
                    values,
                )
            )

        return

    rng = np.random.default_rng(
        config.parameter_search_random_seed
    )

    seen = set()

    base_values = tuple(
        getattr(
            base_config,
            name,
        )
        for name in parameter_names
    )

    seen.add(base_values)

    yield dict(
        zip(
            parameter_names,
            base_values,
        )
    )

    while len(seen) < config.parameter_search_max_combinations:
        values = tuple(
            grid[name][
                int(
                    rng.integers(
                        len(grid[name])
                    )
                )
            ]
            for name in parameter_names
        )

        if values in seen:
            continue

        seen.add(values)

        yield dict(
            zip(
                parameter_names,
                values,
            )
        )


def parameter_result_row(
    parameter_index,
    parameters,
    trade_log,
    data,
    strategy_config,
    config,
):
    if (
        trade_log.empty
        or config.y_col not in trade_log.columns
    ):
        return None, "no_trades"

    summary = summarize_trading(
        trade_log,
        data,
        strategy_config,
    )

    trades = completed_trades(
        trade_log,
        config.y_col,
    )

    target_buy_type = parameter_search_buy_type(
        config,
    )

    target_trades = trades[
        trades["buy_type"] == target_buy_type
    ]

    if target_trades.empty:
        return None, "no_completed_trades"

    if (
        config.parameter_search_min_trades is not None
        and len(target_trades) < config.parameter_search_min_trades
    ):
        return None, "too_few_trades"

    target_summary = summarize_group(
        target_trades,
        config.y_col,
    )

    score = score_summary(
        target_summary,
        config,
    )

    return {
        "parameter_index": parameter_index,
        "search_buy_type": display_strategy_name(target_buy_type),
        "score": score,
        "completed_trades": len(target_trades),
        "completed_trade_rate": (
            len(target_trades) / summary["total_days"]
            if summary["total_days"] > 0
            else 0
        ),
        "median_return": target_summary["median_return"],
        "negative_rate": target_summary["negative_rate"],
        "best_return": target_summary["best_return"],
        "worst_return": target_summary["worst_return"],
        "net_pnl": target_summary["net_pnl"],
        **summarize_parameter_indicators(
            target_trades,
            config,
        ),
        **parameters,
    }, "scored"


def run_parameter_search(config):
    base_config = base_strategy_config(
        config,
    )
    target_buy_type = parameter_search_buy_type(
        config,
    )

    print(
        "\nParameter search"
    )
    print(
        "Search target:",
        display_strategy_name(target_buy_type),
    )
    print(
        "Fetching raw IBKR bars once, then reusing them "
        "for every parameter combination..."
    )

    raw_bars = fetch_bars(
        base_config,
    )

    grid = parameter_grid(
        base_config,
        config,
    )

    rows = []
    skipped_no_trades = 0
    skipped_no_completed_trades = 0
    skipped_too_few_trades = 0

    print(
        "Testing up to "
        f"{config.parameter_search_max_combinations} "
        "nearby parameter combinations..."
    )

    for combination_number, parameters in enumerate(
        sample_parameter_combinations(
            grid,
            base_config,
            config,
        ),
        start=1,
    ):
        if (
            combination_number == 1
            or combination_number % 25 == 0
        ):
            print(
                f"Checked {combination_number} combinations..."
            )

        strategy_config = replace(
            base_config,
            **parameters,
        )

        data = run_strategy_on_data(
            raw_bars,
            strategy_config,
        )

        trade_log = create_trade_log(
            data,
        )

        trade_log = trading_simulation(
            trade_log,
            strategy_config,
        )

        row, status = parameter_result_row(
            combination_number,
            parameters,
            trade_log,
            data,
            strategy_config,
            config,
        )

        if status == "no_trades":
            skipped_no_trades += 1

        elif status == "no_completed_trades":
            skipped_no_completed_trades += 1

        elif status == "too_few_trades":
            skipped_too_few_trades += 1

        elif row is not None:
            rows.append(row)

    print(
        "Scored combinations:",
        len(rows),
    )
    print(
        "Skipped with no trades:",
        skipped_no_trades,
    )
    print(
        "Skipped with no completed trades:",
        skipped_no_completed_trades,
    )
    print(
        "Skipped below minimum trades:",
        skipped_too_few_trades,
    )

    if not rows:
        return pd.DataFrame()

    return (
        pd.DataFrame(rows)
        .sort_values(
            "score",
            ascending=False,
        )
        .head(
            config.top_thresholds,
        )
    )


def format_strategy_config_value(value):
    if hasattr(value, "hour") and hasattr(value, "minute"):
        return f"time({value.hour}, {value.minute})"

    return repr(value)


def strategy_config_annotation(parameter):
    annotation = StrategyConfig.__annotations__.get(
        parameter,
        None,
    )

    if annotation is None:
        return ""

    annotation_name = getattr(
        annotation,
        "__name__",
        str(annotation),
    )

    return f": {annotation_name}"


def print_parameter_config(parameter_index, config):
    selected_config = strategy_config_from_parameter_index(
        parameter_index,
        config,
    )

    print(
        "\nSelected parameter config"
    )
    print(
        f"parameter_index: {parameter_index}"
    )

    for parameter in parameter_search_params(config):
        print(
            f"    {parameter}"
            f"{strategy_config_annotation(parameter)} = "
            f"{format_strategy_config_value(getattr(selected_config, parameter))}"
        )


def main():
    config = AnalysisConfig()

    trade_log = load_trade_log(
        config.trade_log_path
    )

    print_table(
        "Strategy summary",
        strategy_summary(
            trade_log,
            config,
        ),
    )

    thresholds = all_threshold_search(
        trade_log,
        config,
    )

    print_table(
        "Best simple filter rules",
        thresholds.head(
            config.top_thresholds
        ),
    )

    bucket_table = all_bucket_analysis(
        trade_log,
        config,
    )

    if bucket_table.empty:
        best_buckets = bucket_table
    else:
        best_buckets = (
            bucket_table.sort_values(
                [
                    "median_return",
                    "negative_rate",
                ],
                ascending=[
                    False,
                    True,
                ],
            )
            .head(
                config.top_thresholds
            )
        )

    print_table(
        "Best variable buckets",
        best_buckets,
    )

    print_regressions(
        trade_log,
        config,
    )

    if config.selected_parameter_index is not None:
        print_parameter_config(
            config.selected_parameter_index,
            config,
        )

    if config.run_parameter_search:
        try:
            parameter_results = run_parameter_search(config)

            print_table(
                "Best parameter combinations",
                parameter_results,
            )
        except RuntimeError as error:
            print(
                "\nParameter search skipped:"
            )
            print(error)


if __name__ == "__main__":
    main()
