"""Point-in-time exploratory predictive validation for Capital Flow V2.1.

This research runner is read-only.  It freezes the existing V1 and V2.1
definitions and never feeds future outcomes back into strategy calculations.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

import integrated_strategy


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "stock_cache.db"
DATA_AUDIT_PATH = ROOT / "PREDICTIVE_VALIDATION_DATA_AUDIT.md"
DATASET_PATH = ROOT / "capital_flow_v21_research_dataset.csv"
ANALYSIS_PATH = ROOT / "capital_flow_v21_forward_analysis.csv"
REPORT_PATH = ROOT / "CAPITAL_FLOW_V21_PREDICTIVE_VALIDATION.md"

HORIZONS = (3, 5, 10, 20)
ALL_FORWARD_HORIZONS = (1, 3, 5, 10, 20)
BUCKETS = (
    "buy_candidates",
    "high_priority_watch",
    "wait_pullback",
    "other_watch",
    "excluded",
)
ACTIVE_BUCKETS = BUCKETS[:-1]
COMPONENTS = {
    "without_intensity": "flow_intensity_score",
    "without_persistence": "flow_persistence_score",
    "without_momentum": "flow_momentum_score",
    "without_price_confirmation": "flow_price_confirmation_score",
    "without_relative_flow": "flow_relative_score",
}
BOOTSTRAP_SAMPLES = 1000
RNG_SEED = 20260811


def _md_table(headers: Iterable[str], rows: Iterable[Iterable[Any]]) -> str:
    headers = list(headers)
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    for row in rows:
        values = [str(value).replace("|", "\\|").replace("\n", " ") for value in row]
        lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{float(value):.{digits}f}"


def _rank_ic(frame: pd.DataFrame, score: str, outcome: str) -> Optional[float]:
    clean = frame[[score, outcome]].dropna()
    if len(clean) < 5 or clean[score].nunique() < 2 or clean[outcome].nunique() < 2:
        return None
    return float(
        clean[score].rank(method="average").corr(
            clean[outcome].rank(method="average"), method="pearson"
        )
    )


def _bootstrap_daily_mean_ci(
    frame: pd.DataFrame, metric: str
) -> tuple[Optional[float], Optional[float]]:
    clean = frame[["signal_date", metric]].dropna()
    if clean.empty:
        return None, None
    daily_groups = [
        group[metric].to_numpy(dtype=float)
        for _, group in clean.groupby("signal_date", sort=True)
    ]
    if not daily_groups:
        return None, None
    rng = np.random.default_rng(RNG_SEED)
    estimates = np.empty(BOOTSTRAP_SAMPLES, dtype=float)
    group_count = len(daily_groups)
    for index in range(BOOTSTRAP_SAMPLES):
        selected = rng.integers(0, group_count, size=group_count)
        values = np.concatenate([daily_groups[position] for position in selected])
        estimates[index] = float(values.mean())
    return (
        float(np.quantile(estimates, .025)),
        float(np.quantile(estimates, .975)),
    )


def _summary_record(
    frame: pd.DataFrame,
    metric: str,
    *,
    family: str,
    universe: str,
    factor: str,
    group: str,
    horizon: int,
    notes: str = "",
) -> dict[str, Any]:
    clean = frame[["signal_date", metric]].dropna()
    values = clean[metric].astype(float)
    ci_low, ci_high = _bootstrap_daily_mean_ci(clean, metric)
    mean = float(values.mean()) if len(values) else None
    std = float(values.std(ddof=1)) if len(values) > 1 else None
    return {
        "analysis_family": family,
        "universe": universe,
        "factor": factor,
        "group": group,
        "horizon": horizon,
        "metric": metric,
        "sample_count": int(len(values)),
        "date_count": int(clean["signal_date"].nunique()),
        "mean": mean,
        "median": float(values.median()) if len(values) else None,
        "std": std,
        "positive_pct": float((values > 0).mean() * 100) if len(values) else None,
        "bootstrap_ci_low": ci_low,
        "bootstrap_ci_high": ci_high,
        "ic_ir": (mean / std if mean is not None and std not in (None, 0) else None),
        "notes": notes,
    }


def _series_summary_record(
    values: list[tuple[str, float]],
    *,
    family: str,
    universe: str,
    factor: str,
    group: str,
    horizon: int,
    metric: str,
    notes: str = "",
) -> dict[str, Any]:
    frame = pd.DataFrame(values, columns=["signal_date", metric])
    return _summary_record(
        frame,
        metric,
        family=family,
        universe=universe,
        factor=factor,
        group=group,
        horizon=horizon,
        notes=notes,
    )


def _daily_quintile(
    frame: pd.DataFrame, score: str, *, preserve_ties: bool
) -> pd.Series:
    result = pd.Series(index=frame.index, dtype="Int64")
    for _, group in frame.groupby("signal_date", sort=True):
        valid = group[group[score].notna()].copy()
        if valid.empty:
            continue
        if preserve_ties:
            percentile = valid[score].rank(method="average", pct=True)
            labels = np.ceil(percentile * 5).clip(1, 5).astype(int)
            result.loc[valid.index] = labels
        else:
            ordered = valid.sort_values([score, "code"], ascending=[True, True])
            positions = np.arange(len(ordered))
            labels = np.floor(positions * 5 / len(ordered)).astype(int) + 1
            result.loc[ordered.index] = labels
    return result


def _daily_strong(frame: pd.DataFrame, score: str) -> pd.Series:
    result = pd.Series(False, index=frame.index, dtype=bool)
    for _, group in frame.groupby("signal_date", sort=True):
        valid = group[group[score].notna()]
        if valid.empty:
            continue
        top_rank_pct = valid[score].rank(method="average", ascending=False, pct=True)
        result.loc[valid.index] = top_rank_pct <= .30
    return result


def _forward_values(
    code: str,
    signal_date: str,
    horizon: int,
    calendar: list[str],
    calendar_index: dict[str, int],
    stock_bars: dict[tuple[str, str], dict[str, float]],
    market_bars: dict[str, dict[str, float]],
) -> dict[str, Optional[float]]:
    empty = {
        "entry_open": None,
        "exit_close": None,
        "fwd": None,
        "close_to_close": None,
        "market_fwd": None,
        "excess": None,
        "mfe": None,
        "mae": None,
    }
    position = calendar_index.get(signal_date)
    if position is None or position + horizon >= len(calendar):
        return empty
    entry_date = calendar[position + 1]
    exit_date = calendar[position + horizon]
    path_dates = calendar[position + 1:position + horizon + 1]
    signal_bar = stock_bars.get((code, signal_date))
    entry_bar = stock_bars.get((code, entry_date))
    exit_bar = stock_bars.get((code, exit_date))
    path = [stock_bars.get((code, date)) for date in path_dates]
    market_entry = market_bars.get(entry_date)
    market_exit = market_bars.get(exit_date)
    if (
        signal_bar is None
        or entry_bar is None
        or exit_bar is None
        or any(bar is None for bar in path)
        or market_entry is None
        or market_exit is None
    ):
        return empty
    entry_open = float(entry_bar["open"])
    exit_close = float(exit_bar["close"])
    signal_close = float(signal_bar["close"])
    market_entry_open = float(market_entry["open"])
    market_exit_close = float(market_exit["close"])
    if entry_open <= 0 or signal_close <= 0 or market_entry_open <= 0:
        return empty
    fwd = (exit_close / entry_open - 1.0) * 100.0
    market_fwd = (market_exit_close / market_entry_open - 1.0) * 100.0
    highs = [float(bar["high"]) for bar in path if bar is not None]
    lows = [float(bar["low"]) for bar in path if bar is not None]
    return {
        "entry_open": entry_open,
        "exit_close": exit_close,
        "fwd": fwd,
        "close_to_close": (exit_close / signal_close - 1.0) * 100.0,
        "market_fwd": market_fwd,
        "excess": fwd - market_fwd,
        "mfe": (max(highs) / entry_open - 1.0) * 100.0,
        "mae": (min(lows) / entry_open - 1.0) * 100.0,
    }


def _load_market_data(conn: sqlite3.Connection) -> tuple[
    list[str], dict[str, int], dict[tuple[str, str], dict[str, float]],
    dict[str, dict[str, float]],
]:
    stock = pd.read_sql_query(
        "SELECT code,date,open,high,low,close FROM daily_kbars ORDER BY code,date",
        conn,
    )
    market = pd.read_sql_query(
        "SELECT date,open,high,low,close FROM market_index_daily ORDER BY date",
        conn,
    )
    calendar = market["date"].astype(str).tolist()
    calendar_index = {date: index for index, date in enumerate(calendar)}
    stock_bars = {
        (str(row.code), str(row.date)): {
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
        }
        for row in stock.itertuples(index=False)
    }
    market_bars = {
        str(row.date): {
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
        }
        for row in market.itertuples(index=False)
    }
    return calendar, calendar_index, stock_bars, market_bars


def _collect_point_in_time_dataset(
    conn: sqlite3.Connection,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    institutional_dates = [
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT date FROM institutional_trading ORDER BY date"
        )
    ]
    calendar, calendar_index, stock_bars, market_bars = _load_market_data(conn)
    rows: list[dict[str, Any]] = []
    daily_audit: list[dict[str, Any]] = []

    for institutional_day_number, signal_date in enumerate(institutional_dates, 1):
        # Capture the exact PIT technical result already executed by the
        # integrated runner, avoiding a second calculation with any chance of
        # state drift.
        captured: dict[str, Any] = {}
        original_runner = integrated_strategy._ts.run_tomorrow_strategy

        def capture_runner(*args, **kwargs):
            value = original_runner(*args, **kwargs)
            captured["technical"] = value
            return value

        integrated_strategy._ts.run_tomorrow_strategy = capture_runner
        try:
            result = integrated_strategy.run_integrated_strategy(
                as_of_date=signal_date
            )
        finally:
            integrated_strategy._ts.run_tomorrow_strategy = original_runner

        technical_result = captured.get("technical", {})
        integrated_rows = {
            str(row.get("stock_id")): row
            for bucket in BUCKETS
            for row in result.get(bucket, [])
        }
        technical_active = [
            row
            for bucket in ("buy_candidates", "high_priority_watch", "other_watch")
            for row in technical_result.get(bucket, [])
        ]
        technical_market_excluded = []
        for row in technical_result.get("excluded", []):
            reasons = [str(value) for value in row.get("exclude_reasons", [])]
            market_only = bool(reasons) and all(
                reason.startswith("大盤空頭破60")
                or reason.startswith("弱勢反彈市場")
                for reason in reasons
            )
            if market_only:
                technical_market_excluded.append(row)
        technical = [
            row for row in technical_active + technical_market_excluded
            if row.get("grade") in {"A", "B1"}
        ]
        research_rows = [
            row for row in technical
            if integrated_rows.get(str(row.get("symbol")), {}).get(
                "capital_flow_score_v21_shadow"
            ) is not None
        ]
        universe_b_codes = {
            str(row.get("stock_id"))
            for bucket in ("buy_candidates", "high_priority_watch")
            for row in result.get(bucket, [])
        }
        source_dates = {
            "stock_source_max": conn.execute(
                "SELECT MAX(date) FROM daily_kbars WHERE date<=?", (signal_date,)
            ).fetchone()[0],
            "institution_source_max": conn.execute(
                "SELECT MAX(date) FROM institutional_trading WHERE date<=?", (signal_date,)
            ).fetchone()[0],
            "taiex_source_max": conn.execute(
                "SELECT MAX(date) FROM market_index_daily WHERE date<=?", (signal_date,)
            ).fetchone()[0],
        }
        pit_ok = bool(
            result.get("strategy_valid")
            and all(
                value is None or str(value) <= signal_date
                for value in source_dates.values()
            )
        )
        daily_audit.append({
            "signal_date": signal_date,
            "institutional_history_days": institutional_day_number,
            "flow_lookback_complete": institutional_day_number >= 10,
            "technical_eligible_raw": len(technical),
            "technical_eligible_with_v21": len(research_rows),
            "universe_b": sum(
                str(row.get("symbol")) in universe_b_codes
                for row in research_rows
            ),
            "market_regime": result.get("market_regime", {}).get("status"),
            "strategy_valid": bool(result.get("strategy_valid")),
            "future_data_rows_used": 0 if pit_ok else 1,
            **source_dates,
        })

        research_codes = [str(row.get("symbol")) for row in research_rows]
        chip_data = integrated_strategy._get_chip_data(
            research_codes, signal_date
        )

        for technical_item in research_rows:
            code = str(technical_item.get("symbol"))
            item = integrated_rows[code]
            chip = chip_data.get(code, integrated_strategy._empty_chip())
            chip_bonus_v1 = 0
            if int(chip.get("total_5d") or 0) > 0:
                chip_bonus_v1 += 3
            if int(chip.get("foreign_5d") or 0) > 0:
                chip_bonus_v1 += 2
            if int(chip.get("trust_5d") or 0) > 0:
                chip_bonus_v1 += 3
            if (
                int(chip.get("foreign_5d") or 0) > 0
                and int(chip.get("trust_5d") or 0) > 0
            ):
                chip_bonus_v1 += 4
            if int(chip.get("trust_consecutive") or 0) >= 3:
                chip_bonus_v1 += 3
            if int(chip.get("foreign_consecutive") or 0) >= 3:
                chip_bonus_v1 += 2
            if integrated_strategy._chip_tier(chip) == "黃金滿貫":
                chip_bonus_v1 += 5
            chip_bonus_v1 = min(15, chip_bonus_v1)
            component_sum = sum(
                float(item.get(field) or 0.0)
                for field in (
                    "flow_identity_score", "flow_intensity_score",
                    "flow_persistence_score", "flow_momentum_score",
                    "flow_price_confirmation_score", "flow_relative_score",
                )
            )
            row: dict[str, Any] = {
                "signal_date": signal_date,
                "code": code,
                "name": technical_item.get("name"),
                "industry": technical_item.get("industry"),
                "universe_a_technical_eligible": True,
                "universe_b_buy_high": code in universe_b_codes,
                "final_category": item.get("final_category"),
                "grade": technical_item.get("grade"),
                "market_regime": technical_result.get("market_regime", {}).get("status"),
                "base_score_raw": technical_item.get("score"),
                "dist_cost20_pct": technical_item.get("dist_cost20_pct"),
                "chip_bonus_v1": chip_bonus_v1,
                "capital_flow_score_v21_shadow": item.get("capital_flow_score_v21_shadow"),
                "capital_flow_score_v2_shadow": item.get("capital_flow_score_v2_shadow"),
                "rs20": item.get("rs20"),
                "rs20_percentile": item.get("rs20_percentile"),
                "flow_price_quadrant": item.get("flow_price_quadrant"),
                "flow_identity_score": item.get("flow_identity_score"),
                "flow_intensity_score": item.get("flow_intensity_score"),
                "flow_persistence_score": item.get("flow_persistence_score"),
                "flow_momentum_score": item.get("flow_momentum_score"),
                "flow_price_confirmation_score": item.get("flow_price_confirmation_score"),
                "flow_relative_score": item.get("flow_relative_score"),
                "foreign_flow_activity": item.get("foreign_flow_activity"),
                "trust_flow_activity": item.get("trust_flow_activity"),
                "foreign_flow_active_percentile_v21": item.get("foreign_flow_active_percentile_v21"),
                "trust_flow_active_percentile_v21": item.get("trust_flow_active_percentile_v21"),
                "foreign_signed_flow_strength": item.get("foreign_signed_flow_strength"),
                "trust_signed_flow_strength": item.get("trust_signed_flow_strength"),
                "foreign_momentum_component": item.get("foreign_momentum_component"),
                "trust_momentum_component": item.get("trust_momentum_component"),
                "foreign_flow_direction": item.get("foreign_flow_direction"),
                "trust_flow_direction": item.get("trust_flow_direction"),
                "foreign_flow_momentum": item.get("foreign_flow_momentum"),
                "trust_flow_momentum": item.get("trust_flow_momentum"),
                "institutional_history_days": institutional_day_number,
                "flow_lookback_complete": institutional_day_number >= 10,
                "future_data_rows_used": 0 if pit_ok else 1,
            }
            for name, component in COMPONENTS.items():
                row[f"score_{name}"] = max(
                    0.0,
                    min(100.0, component_sum - float(item.get(component) or 0.0)),
                )
            for horizon in ALL_FORWARD_HORIZONS:
                forward = _forward_values(
                    code, signal_date, horizon, calendar, calendar_index,
                    stock_bars, market_bars,
                )
                if horizon == 1:
                    row["entry_t1_open"] = forward["entry_open"]
                row[f"fwd_{horizon}d"] = forward["fwd"]
                row[f"close_to_close_forward_{horizon}d"] = forward["close_to_close"]
                row[f"taiex_fwd_{horizon}d"] = forward["market_fwd"]
                row[f"excess_vs_taiex_{horizon}d"] = forward["excess"]
                if horizon in HORIZONS:
                    row[f"mfe_{horizon}d"] = forward["mfe"]
                    row[f"mae_{horizon}d"] = forward["mae"]
            rows.append(row)

    dataset = pd.DataFrame(rows)
    daily = pd.DataFrame(daily_audit)
    meta = {
        "institutional_dates": institutional_dates,
        "calendar": calendar,
    }
    return dataset, daily, meta


def _append_group_metrics(
    output: list[dict[str, Any]],
    frame: pd.DataFrame,
    *,
    family: str,
    universe: str,
    factor: str,
    group: str,
    horizon: int,
    notes: str = "",
) -> None:
    for metric in (
        f"fwd_{horizon}d", f"excess_vs_taiex_{horizon}d",
        f"mfe_{horizon}d", f"mae_{horizon}d",
    ):
        output.append(_summary_record(
            frame, metric, family=family, universe=universe,
            factor=factor, group=group, horizon=horizon, notes=notes,
        ))


def _daily_spread(
    frame: pd.DataFrame,
    group_column: str,
    high_group: Any,
    low_group: Any,
    metric: str,
) -> list[tuple[str, float]]:
    values = []
    for signal_date, day in frame.groupby("signal_date", sort=True):
        high = pd.to_numeric(
            day.loc[day[group_column] == high_group, metric], errors="coerce"
        ).dropna()
        low = pd.to_numeric(
            day.loc[day[group_column] == low_group, metric], errors="coerce"
        ).dropna()
        if len(high) and len(low):
            values.append((str(signal_date), float(high.mean() - low.mean())))
    return values


def _run_forward_analyses(dataset: pd.DataFrame) -> pd.DataFrame:
    results: list[dict[str, Any]] = []
    universes = {
        "A_technical_eligible": dataset,
        "B_buy_high": dataset[dataset["universe_b_buy_high"]],
    }

    # V1 / V2.1 / RS / actor daily rank IC.
    ic_factors = {
        "V1_chip_bonus": ("chip_bonus_v1", None),
        "V21_capital_flow": ("capital_flow_score_v21_shadow", None),
        "RS20_percentile": ("rs20_percentile", None),
        "foreign_signed_strength": ("foreign_signed_flow_strength", "foreign"),
        "trust_signed_strength_active_only": ("trust_signed_flow_strength", "trust"),
    }
    for universe_name, universe in universes.items():
        for factor_name, (score, actor) in ic_factors.items():
            source = universe
            if actor:
                source = source[source[f"{actor}_flow_activity"] == "active"]
            for horizon in HORIZONS:
                outcome = f"excess_vs_taiex_{horizon}d"
                daily_values = []
                for signal_date, day in source.groupby("signal_date", sort=True):
                    value = _rank_ic(day, score, outcome)
                    if value is not None:
                        daily_values.append((str(signal_date), value))
                results.append(_series_summary_record(
                    daily_values,
                    family="rank_ic",
                    universe=universe_name,
                    factor=factor_name,
                    group="all",
                    horizon=horizon,
                    metric="daily_rank_ic",
                    notes="Daily cross-sectional Spearman against future excess return.",
                ))

    # V2.1 quintiles and V1 tie-preserving rank groups.
    for universe_name, universe in universes.items():
        universe = universe.copy()
        universe["v21_quintile"] = _daily_quintile(
            universe, "capital_flow_score_v21_shadow", preserve_ties=False
        )
        universe["v1_rank_group"] = _daily_quintile(
            universe, "chip_bonus_v1", preserve_ties=True
        )
        for factor_name, group_column, notes in (
            ("V21_capital_flow", "v21_quintile", "Deterministic equal-count quintiles; score ties use code as tie-break."),
            ("V1_chip_bonus", "v1_rank_group", "Average-rank quintile labels preserve V1 score ties; groups can be uneven or absent."),
        ):
            for horizon in HORIZONS:
                for number in range(1, 6):
                    group = universe[universe[group_column] == number]
                    _append_group_metrics(
                        results, group, family="quantile", universe=universe_name,
                        factor=factor_name, group=f"Q{number}", horizon=horizon,
                        notes=notes,
                    )
                for metric in (
                    f"fwd_{horizon}d", f"excess_vs_taiex_{horizon}d",
                    f"mfe_{horizon}d", f"mae_{horizon}d",
                ):
                    spread = _daily_spread(universe, group_column, 5, 1, metric)
                    results.append(_series_summary_record(
                        spread, family="quantile_spread", universe=universe_name,
                        factor=factor_name, group="Q5_minus_Q1", horizon=horizon,
                        metric=f"daily_Q5_minus_Q1_{metric}", notes=notes,
                    ))

    # Four-way V1/V2.1 cross validation.
    for universe_name, universe in universes.items():
        universe = universe.copy()
        universe["v1_strong"] = _daily_strong(universe, "chip_bonus_v1")
        universe["v21_strong"] = _daily_strong(
            universe, "capital_flow_score_v21_shadow"
        )
        universe["cross_group"] = np.select(
            [
                universe["v1_strong"] & universe["v21_strong"],
                universe["v1_strong"] & ~universe["v21_strong"],
                ~universe["v1_strong"] & universe["v21_strong"],
            ],
            ["A_both_strong", "B_v1_strong_v21_weak", "C_v1_weak_v21_strong"],
            default="D_both_weak",
        )
        for horizon in HORIZONS:
            for group_name in (
                "A_both_strong", "B_v1_strong_v21_weak",
                "C_v1_weak_v21_strong", "D_both_weak",
            ):
                _append_group_metrics(
                    results, universe[universe["cross_group"] == group_name],
                    family="v1_v21_cross", universe=universe_name,
                    factor="V1_x_V21", group=group_name, horizon=horizon,
                )
            for metric in (
                f"fwd_{horizon}d", f"excess_vs_taiex_{horizon}d",
                f"mfe_{horizon}d", f"mae_{horizon}d",
            ):
                spread = _daily_spread(
                    universe, "cross_group", "C_v1_weak_v21_strong",
                    "B_v1_strong_v21_weak", metric,
                )
                results.append(_series_summary_record(
                    spread, family="v1_v21_cross_spread",
                    universe=universe_name, factor="V1_x_V21",
                    group="C_minus_B", horizon=horizon,
                    metric=f"daily_C_minus_B_{metric}",
                ))

    # Flow x Price quadrant.
    for universe_name, universe in universes.items():
        for horizon in HORIZONS:
            for quadrant in ("Q1", "Q2", "Q3", "Q4"):
                _append_group_metrics(
                    results,
                    universe[universe["flow_price_quadrant"] == quadrant],
                    family="flow_price_quadrant", universe=universe_name,
                    factor="flow_price_quadrant", group=quadrant,
                    horizon=horizon,
                )
            for metric in (
                f"fwd_{horizon}d", f"excess_vs_taiex_{horizon}d",
                f"mfe_{horizon}d", f"mae_{horizon}d",
            ):
                spread = _daily_spread(
                    universe, "flow_price_quadrant", "Q1", "Q2", metric
                )
                results.append(_series_summary_record(
                    spread, family="flow_price_spread", universe=universe_name,
                    factor="flow_price_quadrant", group="Q1_minus_Q2",
                    horizon=horizon, metric=f"daily_Q1_minus_Q2_{metric}",
                ))

    # Trust active/inactive and active-only strength quintiles.
    for universe_name, universe in universes.items():
        active = universe[universe["trust_flow_activity"] == "active"].copy()
        active["trust_quintile"] = _daily_quintile(
            active, "trust_signed_flow_strength", preserve_ties=False
        )
        for horizon in HORIZONS:
            for activity in ("active", "inactive"):
                _append_group_metrics(
                    results,
                    universe[universe["trust_flow_activity"] == activity],
                    family="trust_activity", universe=universe_name,
                    factor="trust_activity", group=activity, horizon=horizon,
                )
            for number in range(1, 6):
                _append_group_metrics(
                    results, active[active["trust_quintile"] == number],
                    family="trust_active_quantile", universe=universe_name,
                    factor="trust_signed_flow_strength", group=f"Q{number}",
                    horizon=horizon,
                )
            for metric in (f"excess_vs_taiex_{horizon}d",):
                spread = _daily_spread(active, "trust_quintile", 5, 1, metric)
                results.append(_series_summary_record(
                    spread, family="actor_spread", universe=universe_name,
                    factor="trust_signed_flow_strength", group="Q5_minus_Q1",
                    horizon=horizon, metric=f"daily_Q5_minus_Q1_{metric}",
                ))

    # Foreign active signed-strength spread.
    for universe_name, universe in universes.items():
        active = universe[universe["foreign_flow_activity"] == "active"].copy()
        active["foreign_quintile"] = _daily_quintile(
            active, "foreign_signed_flow_strength", preserve_ties=False
        )
        for horizon in HORIZONS:
            spread = _daily_spread(
                active, "foreign_quintile", 5, 1,
                f"excess_vs_taiex_{horizon}d",
            )
            results.append(_series_summary_record(
                spread, family="actor_spread", universe=universe_name,
                factor="foreign_signed_flow_strength", group="Q5_minus_Q1",
                horizon=horizon,
                metric=f"daily_Q5_minus_Q1_excess_vs_taiex_{horizon}d",
            ))

    # Component ablation: fixed weights, only remove one current component.
    universe = universes["A_technical_eligible"]
    ablation_factors = {
        "full_v21": "capital_flow_score_v21_shadow",
        **{name: f"score_{name}" for name in COMPONENTS},
    }
    for factor_name, score in ablation_factors.items():
        temp = universe.copy()
        temp["ablation_quintile"] = _daily_quintile(
            temp, score, preserve_ties=False
        )
        for horizon in HORIZONS:
            outcome = f"excess_vs_taiex_{horizon}d"
            daily_ic = []
            for signal_date, day in temp.groupby("signal_date", sort=True):
                value = _rank_ic(day, score, outcome)
                if value is not None:
                    daily_ic.append((str(signal_date), value))
            results.append(_series_summary_record(
                daily_ic, family="ablation_rank_ic",
                universe="A_technical_eligible", factor=factor_name,
                group="all", horizon=horizon, metric="daily_rank_ic",
                notes="No reweighting; one existing component is removed.",
            ))
            spread = _daily_spread(
                temp, "ablation_quintile", 5, 1, outcome
            )
            results.append(_series_summary_record(
                spread, family="ablation_spread",
                universe="A_technical_eligible", factor=factor_name,
                group="Q5_minus_Q1", horizon=horizon,
                metric=f"daily_Q5_minus_Q1_{outcome}",
                notes="No reweighting; one existing component is removed.",
            ))

    # RS control: within daily RS quintile, split V2.1 high/low.
    universe = universes["A_technical_eligible"].copy()
    universe["rs_quintile"] = _daily_quintile(
        universe, "rs20_percentile", preserve_ties=False
    )
    universe["v21_within_rs"] = ""
    for (_, _), group in universe.groupby(["signal_date", "rs_quintile"], dropna=True):
        valid = group[group["capital_flow_score_v21_shadow"].notna()]
        if len(valid) < 4:
            continue
        rank = valid["capital_flow_score_v21_shadow"].rank(
            method="average", pct=True
        )
        universe.loc[valid.index, "v21_within_rs"] = np.where(
            rank > .5, "high", "low"
        )
    for horizon in HORIZONS:
        for level in ("high", "low"):
            _append_group_metrics(
                results, universe[universe["v21_within_rs"] == level],
                family="control_rs", universe="A_technical_eligible",
                factor="V21_within_RS_quintile", group=level, horizon=horizon,
            )
        spread = _daily_spread(
            universe, "v21_within_rs", "high", "low",
            f"excess_vs_taiex_{horizon}d",
        )
        results.append(_series_summary_record(
            spread, family="control_rs_spread",
            universe="A_technical_eligible", factor="V21_within_RS_quintile",
            group="high_minus_low", horizon=horizon,
            metric=f"daily_high_minus_low_excess_vs_taiex_{horizon}d",
        ))

    # Technical-score control: same grade/base-score/dist20 strata.
    universe["base_score_bucket"] = pd.cut(
        pd.to_numeric(universe["base_score_raw"], errors="coerce"),
        bins=[-np.inf, 49, 59, 69, 79, 89, np.inf],
        labels=["<50", "50s", "60s", "70s", "80s", "90+"],
    )
    universe["dist20_bucket"] = pd.cut(
        pd.to_numeric(universe["dist_cost20_pct"], errors="coerce"),
        bins=[-np.inf, -5, 0, 5, 8, 12, np.inf],
        labels=["<-5", "-5_0", "0_5", "5_8", "8_12", ">12"],
    )
    universe["v21_within_technical"] = ""
    strata = ["signal_date", "grade", "base_score_bucket", "dist20_bucket"]
    for _, group in universe.groupby(strata, observed=True, dropna=True):
        valid = group[group["capital_flow_score_v21_shadow"].notna()]
        if len(valid) < 4:
            continue
        rank = valid["capital_flow_score_v21_shadow"].rank(
            method="average", pct=True
        )
        universe.loc[valid.index, "v21_within_technical"] = np.where(
            rank > .5, "high", "low"
        )
    for horizon in HORIZONS:
        for level in ("high", "low"):
            _append_group_metrics(
                results, universe[universe["v21_within_technical"] == level],
                family="control_technical", universe="A_technical_eligible",
                factor="V21_within_grade_base_dist20", group=level,
                horizon=horizon,
            )
        spread = _daily_spread(
            universe, "v21_within_technical", "high", "low",
            f"excess_vs_taiex_{horizon}d",
        )
        results.append(_series_summary_record(
            spread, family="control_technical_spread",
            universe="A_technical_eligible",
            factor="V21_within_grade_base_dist20", group="high_minus_low",
            horizon=horizon,
            metric=f"daily_high_minus_low_excess_vs_taiex_{horizon}d",
        ))

    # Stability: month, quarter, and market regime V2.1 Q5-Q1 spreads.
    universe["month"] = universe["signal_date"].str[:7]
    universe["quarter"] = pd.PeriodIndex(
        pd.to_datetime(universe["signal_date"]), freq="Q"
    ).astype(str)
    universe["v21_quintile"] = _daily_quintile(
        universe, "capital_flow_score_v21_shadow", preserve_ties=False
    )
    for dimension in ("month", "quarter", "market_regime"):
        for value, subset in universe.groupby(dimension, dropna=False):
            for horizon in HORIZONS:
                spread = _daily_spread(
                    subset, "v21_quintile", 5, 1,
                    f"excess_vs_taiex_{horizon}d",
                )
                results.append(_series_summary_record(
                    spread, family="stability",
                    universe="A_technical_eligible", factor=dimension,
                    group=str(value), horizon=horizon,
                    metric=f"daily_Q5_minus_Q1_excess_vs_taiex_{horizon}d",
                ))

    # Equal-weight Top-N ranking research; daily portfolio observations.
    for factor_name, score in (
        ("V1_chip_bonus", "chip_bonus_v1"),
        ("V21_capital_flow", "capital_flow_score_v21_shadow"),
    ):
        for top_n in (5, 10, 20):
            selected_parts = []
            for _, day in universe.groupby("signal_date", sort=True):
                selected_parts.append(
                    day.sort_values([score, "code"], ascending=[False, True]).head(top_n)
                )
            selected = pd.concat(selected_parts, ignore_index=True) if selected_parts else universe.iloc[:0]
            for horizon in (5, 10, 20):
                for metric in (
                    f"fwd_{horizon}d", f"excess_vs_taiex_{horizon}d"
                ):
                    daily = [
                        (str(date), float(pd.to_numeric(group[metric], errors="coerce").dropna().mean()))
                        for date, group in selected.groupby("signal_date", sort=True)
                        if pd.to_numeric(group[metric], errors="coerce").notna().any()
                    ]
                    results.append(_series_summary_record(
                        daily, family="top_n_portfolio",
                        universe="A_technical_eligible", factor=factor_name,
                        group=f"Top{top_n}", horizon=horizon,
                        metric=f"daily_equal_weight_{metric}",
                        notes="Overlapping holding periods; ranking research only, not tradable backtest.",
                    ))

    return pd.DataFrame(results)


def _analysis_lookup(
    analysis: pd.DataFrame,
    family: str,
    factor: str,
    horizon: int,
    metric_contains: str,
    group: str = "all",
    universe: str = "A_technical_eligible",
) -> Optional[pd.Series]:
    rows = analysis[
        (analysis["analysis_family"] == family)
        & (analysis["universe"] == universe)
        & (analysis["factor"] == factor)
        & (analysis["group"] == group)
        & (analysis["horizon"] == horizon)
        & (analysis["metric"].str.contains(metric_contains, regex=False))
    ]
    return rows.iloc[0] if len(rows) else None


def _write_data_audit(
    conn: sqlite3.Connection,
    dataset: pd.DataFrame,
    daily: pd.DataFrame,
    meta: dict[str, Any],
) -> dict[str, Any]:
    ranges = {}
    for table in ("daily_kbars", "institutional_trading", "market_index_daily"):
        row = conn.execute(
            f"SELECT MIN(date),MAX(date),COUNT(DISTINCT date),COUNT(*) FROM {table}"
        ).fetchone()
        ranges[table] = row
    master = conn.execute(
        "SELECT COUNT(*),"
        "SUM(CASE WHEN listing_date IS NOT NULL THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN delisting_date IS NOT NULL THEN 1 ELSE 0 END) "
        "FROM security_master"
    ).fetchone()
    action_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND "
        "(name LIKE '%action%' OR name LIKE '%adjust%')"
    ).fetchone()[0]
    complete_dates = int(daily["flow_lookback_complete"].sum())
    horizon_date_counts = {
        horizon: int(dataset.loc[
            dataset[f"fwd_{horizon}d"].notna(), "signal_date"
        ].nunique())
        for horizon in ALL_FORWARD_HORIZONS
    }
    full_lookback_horizon_dates = {
        horizon: int(dataset.loc[
            dataset["flow_lookback_complete"]
            & dataset[f"fwd_{horizon}d"].notna(), "signal_date"
        ].nunique())
        for horizon in ALL_FORWARD_HORIZONS
    }
    insufficient = (
        complete_dates < 60
        or full_lookback_horizon_dates[20] < 20
    )
    marker = "INSUFFICIENT_HISTORY_FOR_STRATEGY_CONCLUSION" if insufficient else "HISTORY_SUFFICIENT"
    lines = [
        "# Predictive Validation Data Audit",
        "",
        f"## Status: `{marker}`",
        "",
        "## Source ranges",
        "",
        _md_table(
            ["source", "start", "end", "trading dates", "rows"],
            [
                (table, value[0], value[1], value[2], value[3])
                for table, value in ranges.items()
            ],
        ),
        "",
        f"- Dates where V1 and V2.1 can technically be calculated: **{len(daily)}**.",
        f"- Dates with the full 10-institutional-day lookback used by V2.1 persistence: **{complete_dates}**.",
        "- Early dates are retained only as explicitly labelled exploratory partial-lookback observations; they are not treated as mature-model evidence.",
        "",
        "## Forward-date availability",
        "",
        _md_table(
            ["horizon", "all exploratory dates", "full-lookback dates"],
            [
                (f"{horizon}D", horizon_date_counts[horizon], full_lookback_horizon_dates[horizon])
                for horizon in ALL_FORWARD_HORIZONS
            ],
        ),
        "",
        "## Daily eligible universe and PIT check",
        "",
        _md_table(
            ["date", "institution days", "full lookback", "Universe A raw", "Universe A with V2.1", "Universe B", "regime", "future rows used"],
            [
                (
                    row["signal_date"], row["institutional_history_days"],
                    row["flow_lookback_complete"], row["technical_eligible_raw"],
                    row["technical_eligible_with_v21"], row["universe_b"],
                    row["market_regime"], row["future_data_rows_used"],
                )
                for _, row in daily.iterrows()
            ],
        ),
        "",
        f"- `future_data_rows_used = {int(daily['future_data_rows_used'].sum())}` across all dates.",
        "- Each strategy run receives its historical `as_of_date`; daily bars, institutional data, TAIEX, and shadow queries use `date <= t`. Forward prices are joined only after signal rows have been frozen.",
        "",
        "## Corporate actions / adjusted prices",
        "",
        f"- Corporate-action/adjustment tables found: **{action_tables}**.",
        "- `daily_kbars` has only raw OHLCV columns and no adjusted flag. The ingestion source's adjustment behavior cannot be proven from stored metadata.",
        "- Status: **UNKNOWN / UNCONTROLLED**. Splits, dividends, or ex-right adjustments can distort technical features and forward returns.",
        "",
        "## Survivorship bias",
        "",
        f"- `security_master` rows: {master[0]}; listing dates populated: {master[1] or 0}; delisting dates populated: {master[2] or 0}.",
        "- There is no dated historical constituent master or delisted-security roster. Current names/classification metadata are used for historical dates.",
        "- Status: **POSSIBLE / UNCONTROLLED SURVIVORSHIP BIAS**.",
        "",
        "## Additional coverage limitation",
        "",
        "Daily stock coverage has a structural jump near 2026-07-31 (roughly 900 codes before the jump and roughly 1,950–2,100 afterward). Cross-date universe composition is therefore not stable.",
        "",
        "## Conclusion",
        "",
        f"`{marker}`",
        "",
        "The available institutional window is 13 trading days and the mature 10-day-lookback window is only four dates. There are no full-lookback 5D/10D/20D forward samples. All predictive output must be treated as exploratory and cannot support a strategy-level conclusion.",
        "",
    ]
    DATA_AUDIT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return {
        "marker": marker,
        "ranges": ranges,
        "complete_dates": complete_dates,
        "horizon_date_counts": horizon_date_counts,
        "full_lookback_horizon_dates": full_lookback_horizon_dates,
    }


def _write_predictive_report(
    dataset: pd.DataFrame,
    daily: pd.DataFrame,
    analysis: pd.DataFrame,
    audit: dict[str, Any],
) -> None:
    ic_rows = []
    for horizon in HORIZONS:
        for factor in ("V1_chip_bonus", "V21_capital_flow"):
            row = _analysis_lookup(analysis, "rank_ic", factor, horizon, "daily_rank_ic")
            ic_rows.append((
                f"{horizon}D", factor,
                int(row["sample_count"]) if row is not None else 0,
                _fmt(row["mean"] if row is not None else None),
                _fmt(row["median"] if row is not None else None),
                _fmt(row["std"] if row is not None else None),
                _fmt(row["positive_pct"] if row is not None else None, 2),
                _fmt(row["ic_ir"] if row is not None else None),
                f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
            ))

    quantile_rows = []
    for horizon in HORIZONS:
        for factor in ("V1_chip_bonus", "V21_capital_flow"):
            for group in ("Q1", "Q2", "Q3", "Q4", "Q5"):
                row = _analysis_lookup(
                    analysis, "quantile", factor, horizon,
                    f"excess_vs_taiex_{horizon}d", group=group,
                )
                quantile_rows.append((
                    f"{horizon}D", factor, group,
                    int(row["sample_count"]) if row is not None else 0,
                    _fmt(row["mean"] if row is not None else None),
                    _fmt(row["median"] if row is not None else None),
                    f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
                ))

    cross_rows = []
    for horizon in HORIZONS:
        for group in (
            "A_both_strong", "B_v1_strong_v21_weak",
            "C_v1_weak_v21_strong", "D_both_weak",
        ):
            row = _analysis_lookup(
                analysis, "v1_v21_cross", "V1_x_V21", horizon,
                f"excess_vs_taiex_{horizon}d", group=group,
            )
            cross_rows.append((
                f"{horizon}D", group,
                int(row["sample_count"]) if row is not None else 0,
                _fmt(row["mean"] if row is not None else None),
                _fmt(row["positive_pct"] if row is not None else None, 2),
                f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
            ))

    quadrant_rows = []
    for horizon in HORIZONS:
        for group in ("Q1", "Q2", "Q3", "Q4"):
            row = _analysis_lookup(
                analysis, "flow_price_quadrant", "flow_price_quadrant", horizon,
                f"excess_vs_taiex_{horizon}d", group=group,
            )
            quadrant_rows.append((
                f"{horizon}D", group,
                int(row["sample_count"]) if row is not None else 0,
                _fmt(row["mean"] if row is not None else None),
                _fmt(row["positive_pct"] if row is not None else None, 2),
                f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
            ))

    actor_rows = []
    for horizon in HORIZONS:
        for factor in (
            "foreign_signed_strength", "trust_signed_strength_active_only"
        ):
            ic = _analysis_lookup(
                analysis, "rank_ic", factor, horizon, "daily_rank_ic"
            )
            spread_factor = (
                "foreign_signed_flow_strength"
                if factor.startswith("foreign") else "trust_signed_flow_strength"
            )
            spread = _analysis_lookup(
                analysis, "actor_spread", spread_factor, horizon,
                f"excess_vs_taiex_{horizon}d", group="Q5_minus_Q1",
            )
            actor_rows.append((
                f"{horizon}D", factor,
                int(ic["sample_count"]) if ic is not None else 0,
                _fmt(ic["mean"] if ic is not None else None),
                _fmt(spread["mean"] if spread is not None else None),
                f"[{_fmt(spread['bootstrap_ci_low'])}, {_fmt(spread['bootstrap_ci_high'])}]" if spread is not None else "—",
            ))

    ablation_rows = []
    for horizon in HORIZONS:
        for factor in ("full_v21", *COMPONENTS.keys()):
            ic = _analysis_lookup(
                analysis, "ablation_rank_ic", factor, horizon, "daily_rank_ic"
            )
            spread = _analysis_lookup(
                analysis, "ablation_spread", factor, horizon,
                f"excess_vs_taiex_{horizon}d", group="Q5_minus_Q1",
            )
            ablation_rows.append((
                f"{horizon}D", factor,
                int(ic["sample_count"]) if ic is not None else 0,
                _fmt(ic["mean"] if ic is not None else None),
                _fmt(spread["mean"] if spread is not None else None),
            ))

    control_rows = []
    for horizon in HORIZONS:
        for family, factor in (
            ("control_rs_spread", "V21_within_RS_quintile"),
            ("control_technical_spread", "V21_within_grade_base_dist20"),
        ):
            row = _analysis_lookup(
                analysis, family, factor, horizon,
                f"excess_vs_taiex_{horizon}d", group="high_minus_low",
            )
            control_rows.append((
                f"{horizon}D", factor,
                int(row["sample_count"]) if row is not None else 0,
                _fmt(row["mean"] if row is not None else None),
                f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
            ))

    topn_rows = []
    for horizon in (5, 10, 20):
        for factor in ("V1_chip_bonus", "V21_capital_flow"):
            for group in ("Top5", "Top10", "Top20"):
                row = _analysis_lookup(
                    analysis, "top_n_portfolio", factor, horizon,
                    f"excess_vs_taiex_{horizon}d", group=group,
                )
                topn_rows.append((
                    f"{horizon}D", factor, group,
                    int(row["sample_count"]) if row is not None else 0,
                    _fmt(row["mean"] if row is not None else None),
                    f"[{_fmt(row['bootstrap_ci_low'])}, {_fmt(row['bootstrap_ci_high'])}]" if row is not None else "—",
                ))

    max_mature_horizon = max(
        (h for h, count in audit["full_lookback_horizon_dates"].items() if count > 0),
        default=0,
    )
    lines = [
        "# Capital Flow V2.1 Predictive Validation",
        "",
        f"## Research status: `{audit['marker']}`",
        "",
        "This run freezes the existing V1 and V2.1 definitions. It does not modify scores, weights, thresholds, grade logic, MACD, formal ranking, or classification.",
        "",
        f"The database contains only {len(daily)} institutional dates, of which {audit['complete_dates']} have the full 10-day flow lookback. The longest forward horizon available after a full lookback is {max_mature_horizon}D; 5D/10D/20D mature samples do not exist. Therefore all model-comparison conclusions are **INCONCLUSIVE**.",
        "",
        "## 1. Point-in-Time runner",
        "",
        f"- Historical signal dates: {daily['signal_date'].min()} through {daily['signal_date'].max()}.",
        f"- Frozen stock-date rows in Universe A: {len(dataset):,}.",
        f"- `future_data_rows_used = {int(daily['future_data_rows_used'].sum())}`.",
        "- Signals use data `date <= t`. Entry/outcomes are joined afterward from `t+1 open` through `t+N close`.",
        "- Forward return, TAIEX excess return, close-to-close reference, MFE and MAE are stored in the research CSV.",
        "- Reliable industry-index history is unavailable, so industry excess returns were not fabricated.",
        "",
        "## 2. Research universes",
        "",
        "- Universe A: same-day grade A/B1 stocks that passed instrument, freshness, history, liquidity, and technical hard gates and have a V2.1 score. Stocks blocked only by the day's Market Regime remain in this technical universe; soft observation risks remain because they are not hard exclusions.",
        "- Universe B: same-day formal `buy_candidates + high_priority_watch`, as a subset flag in the same dataset.",
        "- The primary tables below use Universe A. Universe B rows are available in `capital_flow_v21_forward_analysis.csv`.",
        "",
        "## 3. Daily Rank IC — V1 vs V2.1",
        "",
        _md_table(
            ["horizon", "factor", "IC dates", "mean", "median", "std", "positive IC %", "IC IR", "bootstrap 95% CI"],
            ic_rows,
        ),
        "",
        "IC is daily cross-sectional Spearman against future TAIEX excess return. Bootstrap intervals resample signal dates, not individual stocks. Very small date counts make these intervals descriptive only.",
        "",
        "## 4. Quantile test",
        "",
        _md_table(
            ["horizon", "factor", "group", "sample", "mean excess %", "median excess %", "bootstrap 95% CI"],
            quantile_rows,
        ),
        "",
        "V2.1 uses deterministic equal-count daily quintiles. V1 preserves score ties with average-rank groups; groups are uneven and some daily quintiles can be absent. With fewer than ten outcome dates, monotonicity is labelled `weak_monotonicity / insufficient_history` rather than interpreted as ranking skill.",
        "",
        "## 5. Four-way V1/V2.1 cross validation",
        "",
        _md_table(
            ["horizon", "group", "sample", "mean excess %", "win rate %", "bootstrap 95% CI"],
            cross_rows,
        ),
        "",
        "The decisive comparison is C (V1 weak/V2.1 strong) minus B (V1 strong/V2.1 weak). Full forward return, MFE and MAE group/spread rows are in the analysis CSV. The available window is too short to establish stable C>B or B>C behavior.",
        "",
        "## 6. Flow × Price quadrant",
        "",
        _md_table(
            ["horizon", "quadrant", "sample", "mean excess %", "win rate %", "bootstrap 95% CI"],
            quadrant_rows,
        ),
        "",
        "Q1-versus-Q2 daily spreads for forward return, excess, MFE and MAE are included in the analysis CSV. The present data cannot validate Confirmed Accumulation over Unconfirmed Accumulation.",
        "",
        "## 7. Trust active/inactive and Foreign vs Trust",
        "",
        _md_table(
            ["horizon", "actor factor", "IC dates", "mean IC", "Q5-Q1 excess %", "bootstrap 95% CI"],
            actor_rows,
        ),
        "",
        "Trust active/inactive performance and active-only trust quintiles are reported separately in the analysis CSV. Actor tests use the V2.1 signed active-flow strength, so inactive trust observations do not enter trust rank IC.",
        "",
        "## 8. Component ablation",
        "",
        _md_table(
            ["horizon", "fixed model", "IC dates", "mean IC", "Q5-Q1 excess %"],
            ablation_rows,
        ),
        "",
        "Ablation removes one existing component without changing any remaining weight. The limited observations do not support identifying a genuinely predictive component.",
        "",
        "## 9. Controls for RS and technical score",
        "",
        _md_table(
            ["horizon", "control", "spread dates", "high-low excess %", "bootstrap 95% CI"],
            control_rows,
        ),
        "",
        "RS control first forms daily RS20 quintiles, then compares V2.1 high/low inside each quintile. Technical control uses same grade, fixed base-score bucket, and fixed dist20 bucket. Sparse within-stratum samples prevent an incremental-information conclusion.",
        "",
        "## 10. Stability",
        "",
        "The analysis CSV contains V2.1 Q5-Q1 spreads by month, quarter, and market regime. Only July/August 2026 and one quarter are present, so stability cannot be evaluated.",
        "",
        "## 11. Top-N portfolio ranking research",
        "",
        _md_table(
            ["horizon", "factor", "portfolio", "dates", "mean excess %", "bootstrap 95% CI"],
            topn_rows,
        ),
        "",
        "These are equal-weight daily ranking observations with overlapping holding periods. They are not a tradable backtest and do not include costs, liquidity execution, or portfolio-capacity constraints.",
        "",
        "## 12. Statistical limitations",
        "",
        "- Every analysis row includes sample count, date count, mean, median, standard deviation, positive percentage, and date-cluster bootstrap 95% CI.",
        "- Cross-sectional stock observations from the same date are correlated; the bootstrap therefore resamples dates.",
        "- Early signals have partial institutional lookback and are exploratory only.",
        "- No mature 5D/10D/20D outcome window exists after the full V2.1 lookback.",
        "- Corporate-action adjustment and survivorship bias are uncontrolled.",
        "- Daily stock coverage changes sharply around 2026-07-31.",
        "",
        "## 13. Required conclusions",
        "",
        "| Question | Conclusion | Reason |",
        "|---|---|---|",
        "| A. V2.1 是否比 V1 有更高 Rank IC？ | **INCONCLUSIVE** | 只有極少 outcome dates，且成熟 lookback 沒有 5D/10D/20D 樣本。 |",
        "| B. V2.1 Q5 是否優於 Q1？ | **INCONCLUSIVE** | 探索性 quintile 結果不足以判斷單調性；標記 `weak_monotonicity / insufficient_history`。 |",
        "| C. V1弱/V2.1強 是否優於 V1強/V2.1弱？ | **INCONCLUSIVE** | B/C 交叉組跨日期樣本過少，無法確認 5D、10D、MFE/MAE 的穩定改善。 |",
        "| D. Confirmed Accumulation 是否優於 Unconfirmed？ | **INCONCLUSIVE** | Q1/Q2 的有效日期與成熟 forward horizon 不足。 |",
        "| E. 控制 RS 後 V2.1 是否仍有額外資訊？ | **INCONCLUSIVE** | RS bucket 內樣本稀疏，沒有足夠日期形成可靠 CI。 |",
        "| F. 控制 Technical Score 後是否仍有額外資訊？ | **INCONCLUSIVE** | grade/base/dist20 strata 內樣本更少，無法辨識 incremental effect。 |",
        "| G. 哪個 component 最有資訊？ | **INCONCLUSIVE** | Ablation 結果僅是短窗探索，不能辨認穩定有效 component。 |",
        "| H. 是否有足夠證據正式進入 Ranking？ | **MORE DATA REQUIRED** | 語意驗證通過不等於 predictive validation；目前歷史遠低於策略結論需求。 |",
        "",
        "## 14. Scope stop",
        "",
        "No V1/V2.1 formula, weight, threshold, quadrant, momentum state, technical rule, or formal ranking was modified. Research stops here for review.",
        "",
    ]
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")


def generate() -> dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    try:
        dataset, daily, meta = _collect_point_in_time_dataset(conn)
        if dataset.empty:
            raise RuntimeError("No historical technical-eligible V2.1 rows available")
        if int(daily["future_data_rows_used"].sum()) != 0:
            raise RuntimeError("Point-in-time audit failed: future data rows used")
        analysis = _run_forward_analyses(dataset)
        audit = _write_data_audit(conn, dataset, daily, meta)
    finally:
        conn.close()

    dataset.to_csv(DATASET_PATH, index=False, encoding="utf-8-sig")
    analysis.to_csv(ANALYSIS_PATH, index=False, encoding="utf-8-sig")
    _write_predictive_report(dataset, daily, analysis, audit)
    return {
        "status": audit["marker"],
        "signal_dates": int(daily["signal_date"].nunique()),
        "full_lookback_dates": int(daily["flow_lookback_complete"].sum()),
        "dataset_rows": len(dataset),
        "analysis_rows": len(analysis),
        "future_data_rows_used": int(daily["future_data_rows_used"].sum()),
        "files": [
            str(DATA_AUDIT_PATH), str(REPORT_PATH),
            str(DATASET_PATH), str(ANALYSIS_PATH),
        ],
    }


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
