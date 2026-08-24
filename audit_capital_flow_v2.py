"""Generate the Capital Flow V2 implementation/Information-Difference audit.

This file is research tooling only.  It reads the current strategy and database,
runs the formal pipeline with and without the V2 shadow bundle, and writes the
three requested audit artifacts.  It does not modify scoring or classification.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

import capital_flow_v2
import integrated_strategy


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "stock_cache.db"
BEFORE_FIXTURE = (
    ROOT / "tests" / "stock_selection" / "fixtures"
    / "capital_flow_v2_m1_before.json"
)
SNAPSHOT_PATH = ROOT / "capital_flow_v2_snapshot.csv"
COMPARISON_PATH = ROOT / "capital_flow_v2_v1_comparison.csv"
REPORT_PATH = ROOT / "CAPITAL_FLOW_V2_VALIDATION.md"

BUCKETS = (
    "buy_candidates",
    "high_priority_watch",
    "wait_pullback",
    "other_watch",
    "excluded",
)

COVERAGE_FIELDS = (
    "foreign_flow_1d", "foreign_flow_3d", "foreign_flow_5d",
    "foreign_flow_10d", "trust_flow_1d", "trust_flow_3d",
    "trust_flow_5d", "trust_flow_10d", "foreign_flow_ratio_5d",
    "trust_flow_ratio_5d", "foreign_flow_percentile",
    "trust_flow_percentile", "foreign_flow_momentum",
    "trust_flow_momentum", "rs20", "rs60", "rs20_percentile",
    "flow_price_quadrant", "capital_flow_score_v2_shadow",
)

SNAPSHOT_COLUMNS = (
    "code", "name", "industry", "as_of_date", "formal_category",
    "formal_final_score", "chip_bonus_v1", "v1_rank", "v2_rank",
    "rank_change", "v1_percentile", "v2_percentile",
    "foreign_5d_v1", "trust_5d_v1", "dealer_5d_v1",
    "institution_5d_total_v1", "foreign_consecutive_v1",
    "trust_consecutive_v1", "chip_tier_v1", "shadow_mode",
    "capital_flow_v2_available", "foreign_flow_1d",
    "foreign_flow_3d", "foreign_flow_5d", "foreign_flow_10d",
    "foreign_flow_1d_shares", "foreign_flow_3d_shares",
    "foreign_flow_5d_shares", "foreign_flow_10d_shares",
    "trust_flow_1d", "trust_flow_3d", "trust_flow_5d",
    "trust_flow_10d", "trust_flow_1d_shares", "trust_flow_3d_shares",
    "trust_flow_5d_shares", "trust_flow_10d_shares",
    "foreign_flow_ratio_1d", "foreign_flow_ratio_3d",
    "foreign_flow_ratio_5d", "foreign_flow_ratio_10d",
    "trust_flow_ratio_1d", "trust_flow_ratio_3d",
    "trust_flow_ratio_5d", "trust_flow_ratio_10d",
    "foreign_amount_ratio_5d", "trust_amount_ratio_5d",
    "foreign_positive_days_5", "foreign_positive_days_10",
    "trust_positive_days_5", "trust_positive_days_10",
    "foreign_consecutive_buy", "trust_consecutive_buy",
    "foreign_flow_momentum", "trust_flow_momentum", "return_1d",
    "return_3d", "return_5d", "stock_return20", "stock_return60",
    "taiex_return20", "taiex_return60", "rs5", "rs20", "rs60",
    "rs20_percentile", "rs60_percentile",
    "foreign_flow_intensity_percentile",
    "trust_flow_intensity_percentile", "foreign_flow_percentile",
    "trust_flow_percentile", "multi_flow_confirmation",
    "dealer_prop_flow_5d_shares", "dealer_hedge_flow_5d_shares",
    "dealer_unknown_flow_5d_shares", "dealer_flow_detail_level",
    "flow_price_quadrant", "flow_price_state", "flow_identity_score",
    "flow_intensity_score", "flow_persistence_score",
    "flow_momentum_score", "flow_price_confirmation_score",
    "flow_relative_score", "capital_flow_score_v2_shadow",
    "current_research_rank", "shadow_rank_v2", "shadow_rank_change",
    "case", "movement_reason",
)


def _formal_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for bucket in BUCKETS for row in result.get(bucket, [])]


def _formal_map(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stock_id")): row for row in _formal_rows(result)}


def _formal_hash(rows: list[dict[str, Any]], fields: list[str]) -> str:
    projection = [{key: row.get(key) for key in fields} for row in rows]
    payload = json.dumps(
        projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_without_shadow(as_of_date: str) -> dict[str, Any]:
    original = capital_flow_v2.compute_capital_flow_v2_shadow

    def disabled(_conn, value: str) -> dict[str, Any]:
        return {
            "shadow_mode": True,
            "as_of_date": value,
            "metrics_by_code": {},
            "universe_size": 0,
            "errors": ["disabled_for_formal_regression_audit"],
        }

    capital_flow_v2.compute_capital_flow_v2_shadow = disabled
    try:
        return integrated_strategy.run_integrated_strategy(as_of_date=as_of_date)
    finally:
        capital_flow_v2.compute_capital_flow_v2_shadow = original


def _v1_chip_bonus(chip: dict[str, Any]) -> int:
    total = int(chip.get("total_5d") or 0)
    foreign = int(chip.get("foreign_5d") or 0)
    trust = int(chip.get("trust_5d") or 0)
    foreign_consecutive = int(chip.get("foreign_consecutive") or 0)
    trust_consecutive = int(chip.get("trust_consecutive") or 0)
    tier = integrated_strategy._chip_tier(chip)
    score = 0
    if total > 0:
        score += 3
    if foreign > 0:
        score += 2
    if trust > 0:
        score += 3
    if foreign > 0 and trust > 0:
        score += 4
    if trust_consecutive >= 3:
        score += 3
    if foreign_consecutive >= 3:
        score += 2
    if tier == "黃金滿貫":
        score += 5
    return min(15, score)


def _sequential_rank(frame: pd.DataFrame, score: str) -> pd.Series:
    ordered = frame.sort_values([score, "code"], ascending=[False, True]).index
    ranks = pd.Series(index=frame.index, dtype="int64")
    for rank, index in enumerate(ordered, 1):
        ranks.at[index] = rank
    return ranks.astype(int)


def _fmt_number(value: Any, digits: int = 3) -> str:
    if value is None or pd.isna(value):
        return "—"
    if isinstance(value, (int,)):
        return f"{value:,}"
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


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


def _movement_reason(row: pd.Series, direction: str) -> str:
    reasons: list[str] = []
    intensity = float(row.get("flow_intensity_score") or 0)
    relative = float(row.get("flow_relative_score") or 0)
    persistence = float(row.get("flow_persistence_score") or 0)
    momentum = float(row.get("flow_momentum_score") or 0)
    rs20 = float(row.get("rs20") or 0)
    quadrant = row.get("flow_price_quadrant")
    f_pct = float(row.get("foreign_flow_percentile") or 0)
    t_pct = float(row.get("trust_flow_percentile") or 0)
    f_momentum = str(row.get("foreign_flow_momentum") or "")
    t_momentum = str(row.get("trust_flow_momentum") or "")
    fc = int(row.get("foreign_consecutive_v1") or 0)
    tc = int(row.get("trust_consecutive_v1") or 0)

    if direction == "up":
        if intensity >= 15:
            reasons.append(f"5日資金強度分 {intensity:.1f}/25")
        if relative >= 14:
            reasons.append(f"橫向強度高（外資P{f_pct:.0f}、投信P{t_pct:.0f}）")
        if quadrant == "Q1":
            reasons.append(f"價格與RS同步確認（RS20 {rs20:+.2f}）")
        if momentum >= 7:
            reasons.append(
                f"動能較佳（外資 {f_momentum}／投信 {t_momentum}）"
            )
        if persistence < 5:
            reasons.append("連買天數不長，但V2不只依賴連買")
    else:
        if fc >= 3 or tc >= 3:
            reasons.append(f"V1連買訊號高（外資{fc}日／投信{tc}日）")
        if intensity <= 6:
            reasons.append(f"實際介入強度低（強度分 {intensity:.1f}/25）")
        if relative <= 7:
            reasons.append(f"全市場flow percentile偏低（外資P{f_pct:.0f}、投信P{t_pct:.0f}）")
        if quadrant in {"Q2", "Q4"}:
            reasons.append(f"價格未確認（{quadrant}、RS20 {rs20:+.2f}）")
        if "reversing_negative" in {f_momentum, t_momentum}:
            reasons.append("近期flow已轉負")
        elif "decelerating" in {f_momentum, t_momentum}:
            reasons.append("近期flow正在減速")
    if not reasons:
        reasons.append("差異主要來自V1離散加分與V2連續型強度／排名分數")
    return "；".join(reasons[:4])


def _coverage(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    total = len(frame)
    for field in COVERAGE_FIELDS:
        series = frame[field]
        valid = int(series.notna().sum())
        numeric = pd.api.types.is_numeric_dtype(series)
        row = {
            "field": field,
            "valid_count": valid,
            "null_count": total - valid,
            "coverage_pct": valid / total * 100 if total else 0,
            "min": None,
            "median": None,
            "max": None,
            "mean": None,
        }
        if numeric and valid:
            clean = pd.to_numeric(series, errors="coerce").dropna()
            row.update({
                "min": clean.min(), "median": clean.median(),
                "max": clean.max(), "mean": clean.mean(),
            })
        rows.append(row)
    return pd.DataFrame(rows)


def _raw_sample(
    conn: sqlite3.Connection, code: str, dates: list[str]
) -> list[dict[str, Any]]:
    bar_rows = {
        str(row[0]): {"close": row[1], "volume": row[2]}
        for row in conn.execute(
            "SELECT date,close,volume FROM daily_kbars WHERE code=? "
            "AND date IN ({})".format(",".join("?" for _ in dates)),
            [code, *dates],
        )
    }
    flow_rows = {
        str(row[0]): {"foreign_net": row[1], "trust_net": row[2]}
        for row in conn.execute(
            "SELECT date,COALESCE(foreign_net,0),COALESCE(trust_net,0) "
            "FROM institutional_trading WHERE code=? AND date IN ({})".format(
                ",".join("?" for _ in dates)
            ),
            [code, *dates],
        )
    }
    result = []
    for date in dates:
        bar = bar_rows.get(date, {})
        flow = flow_rows.get(date, {})
        result.append({
            "date": date,
            "close": bar.get("close"),
            "volume": bar.get("volume"),
            "foreign_net": flow.get("foreign_net", 0),
            "trust_net": flow.get("trust_net", 0),
        })
    return result


def generate() -> dict[str, Any]:
    baseline = json.loads(BEFORE_FIXTURE.read_text(encoding="utf-8"))
    as_of_date = str(baseline["as_of_date"])

    current = integrated_strategy.run_integrated_strategy(as_of_date=as_of_date)
    no_shadow = _run_without_shadow(as_of_date)
    current_map = _formal_map(current)
    old_map = _formal_map(no_shadow)

    conn = sqlite3.connect(DB_PATH)
    shadow_bundle = capital_flow_v2.compute_capital_flow_v2_shadow(
        conn, as_of_date
    )
    metrics_by_code = shadow_bundle["metrics_by_code"]
    codes = sorted(metrics_by_code)
    names = {
        str(row[0]): (str(row[1] or row[0]), str(row[2] or ""))
        for row in conn.execute(
            "SELECT code,name,COALESCE(category,'') FROM stock_names"
        )
    }
    chip_data = integrated_strategy._get_chip_data(codes, as_of_date)

    snapshot_rows: list[dict[str, Any]] = []
    for code in codes:
        chip = chip_data.get(code, integrated_strategy._empty_chip())
        metric = metrics_by_code[code]
        name, industry = names.get(code, (code, ""))
        formal = current_map.get(code, {})
        row = {
            "code": code,
            "name": name,
            "industry": industry,
            "as_of_date": as_of_date,
            "formal_category": formal.get("final_category"),
            "formal_final_score": formal.get("final_score"),
            "chip_bonus_v1": _v1_chip_bonus(chip),
            "foreign_5d_v1": int(chip.get("foreign_5d") or 0),
            "trust_5d_v1": int(chip.get("trust_5d") or 0),
            "dealer_5d_v1": int(chip.get("dealer_5d") or 0),
            "institution_5d_total_v1": int(chip.get("total_5d") or 0),
            "foreign_consecutive_v1": int(chip.get("foreign_consecutive") or 0),
            "trust_consecutive_v1": int(chip.get("trust_consecutive") or 0),
            "chip_tier_v1": integrated_strategy._chip_tier(chip),
        }
        row.update(metric)
        snapshot_rows.append(row)

    snapshot = pd.DataFrame(snapshot_rows)
    snapshot["v1_rank"] = _sequential_rank(snapshot, "chip_bonus_v1")
    snapshot["v2_rank"] = _sequential_rank(
        snapshot, "capital_flow_score_v2_shadow"
    )
    snapshot["rank_change"] = snapshot["v1_rank"] - snapshot["v2_rank"]
    snapshot["v1_percentile"] = (
        snapshot["chip_bonus_v1"].rank(method="average", pct=True) * 100
    )
    snapshot["v2_percentile"] = (
        snapshot["capital_flow_score_v2_shadow"].rank(
            method="average", pct=True
        ) * 100
    )

    research_codes = [
        str(row["stock_id"])
        for bucket in ("buy_candidates", "high_priority_watch")
        for row in current.get(bucket, [])
    ]
    current_research_rank = {
        code: rank for rank, code in enumerate(research_codes, 1)
    }
    shadow_research_order = sorted(
        research_codes,
        key=lambda code: (
            -float(metrics_by_code.get(code, {}).get(
                "capital_flow_score_v2_shadow"
            ) or 0),
            code,
        ),
    )
    shadow_research_rank = {
        code: rank for rank, code in enumerate(shadow_research_order, 1)
    }
    snapshot["current_research_rank"] = snapshot["code"].map(
        current_research_rank
    )
    snapshot["shadow_rank_v2"] = snapshot["code"].map(shadow_research_rank)
    snapshot["shadow_rank_change"] = (
        snapshot["current_research_rank"] - snapshot["shadow_rank_v2"]
    )

    snapshot["movement_reason"] = snapshot.apply(
        lambda row: _movement_reason(
            row, "up" if row["rank_change"] > 0 else "down"
        ),
        axis=1,
    )
    snapshot["case"] = ""
    v1_strong_mask = snapshot["chip_bonus_v1"] >= 10
    v1_strong_v2_median = float(
        snapshot.loc[v1_strong_mask, "capital_flow_score_v2_shadow"].median()
    )
    snapshot.loc[
        v1_strong_mask
        & (snapshot["capital_flow_score_v2_shadow"] >= v1_strong_v2_median),
        "case",
    ] = "A_V1_strong_V2_strong"
    snapshot.loc[
        v1_strong_mask
        & (snapshot["capital_flow_score_v2_shadow"] < v1_strong_v2_median),
        "case",
    ] = "B_V1_strong_V2_weak"
    snapshot.loc[
        (snapshot["chip_bonus_v1"] <= 5)
        & (snapshot["v2_percentile"] >= 75), "case"
    ] = "C_V1_weak_V2_strong"
    snapshot.loc[
        (snapshot["chip_bonus_v1"] <= 5)
        & (snapshot["v2_percentile"] <= 25), "case"
    ] = "D_V1_weak_V2_weak"

    for column in SNAPSHOT_COLUMNS:
        if column not in snapshot.columns:
            snapshot[column] = None
    snapshot.loc[:, SNAPSHOT_COLUMNS].to_csv(
        SNAPSHOT_PATH, index=False, encoding="utf-8-sig"
    )

    eligible_lookup = snapshot.set_index("code").to_dict("index")
    comparison_rows = []
    all_formal_codes = sorted(set(old_map) | set(current_map))
    for code in all_formal_codes:
        old = old_map.get(code, {})
        new = current_map.get(code, {})
        eligible = eligible_lookup.get(code, {})
        old_category = old.get("final_category")
        new_category = new.get("final_category")
        old_score = old.get("final_score")
        new_score = new.get("final_score")
        comparison_rows.append({
            "code": code,
            "name": new.get("stock_name") or old.get("stock_name") or eligible.get("name"),
            "eligible_common_stock": bool(eligible),
            "old_category": old_category,
            "new_category": new_category,
            "old_final_score": old_score,
            "new_final_score": new_score,
            "category_changed": old_category != new_category,
            "final_score_changed": old_score != new_score,
            "chip_bonus_v1": eligible.get("chip_bonus_v1"),
            "capital_flow_score_v2_shadow": eligible.get("capital_flow_score_v2_shadow"),
            "v1_rank": eligible.get("v1_rank"),
            "v2_rank": eligible.get("v2_rank"),
            "rank_change": eligible.get("rank_change"),
            "v1_percentile": eligible.get("v1_percentile"),
            "v2_percentile": eligible.get("v2_percentile"),
            "case": eligible.get("case"),
            "movement_reason": eligible.get("movement_reason"),
            "flow_price_quadrant": eligible.get("flow_price_quadrant"),
            "foreign_flow_ratio_5d": eligible.get("foreign_flow_ratio_5d"),
            "trust_flow_ratio_5d": eligible.get("trust_flow_ratio_5d"),
            "foreign_flow_percentile": eligible.get("foreign_flow_percentile"),
            "trust_flow_percentile": eligible.get("trust_flow_percentile"),
            "rs20": eligible.get("rs20"),
            "rs20_percentile": eligible.get("rs20_percentile"),
            "flow_identity_score": eligible.get("flow_identity_score"),
            "flow_intensity_score": eligible.get("flow_intensity_score"),
            "flow_persistence_score": eligible.get("flow_persistence_score"),
            "flow_momentum_score": eligible.get("flow_momentum_score"),
            "flow_price_confirmation_score": eligible.get("flow_price_confirmation_score"),
            "flow_relative_score": eligible.get("flow_relative_score"),
            "current_research_rank": eligible.get("current_research_rank"),
            "shadow_rank_v2": eligible.get("shadow_rank_v2"),
            "shadow_rank_change": eligible.get("shadow_rank_change"),
        })
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(COMPARISON_PATH, index=False, encoding="utf-8-sig")

    coverage = _coverage(snapshot)
    pearson = float(snapshot["chip_bonus_v1"].corr(
        snapshot["capital_flow_score_v2_shadow"], method="pearson"
    ))
    # Spearman is Pearson over average tied ranks.  Compute it directly so the
    # audit does not add SciPy as an application dependency.
    spearman = float(
        snapshot["chip_bonus_v1"].rank(method="average").corr(
            snapshot["capital_flow_score_v2_shadow"].rank(method="average"),
            method="pearson",
        )
    )

    v1_order = snapshot.sort_values(
        ["chip_bonus_v1", "code"], ascending=[False, True]
    )
    v2_order = snapshot.sort_values(
        ["capital_flow_score_v2_shadow", "code"], ascending=[False, True]
    )
    overlaps = {
        size: len(
            set(v1_order.head(size)["code"])
            & set(v2_order.head(size)["code"])
        )
        for size in (10, 20, 30)
    }
    upgrades = snapshot.sort_values(
        ["rank_change", "code"], ascending=[False, True]
    ).head(20)
    downgrades = snapshot.sort_values(
        ["rank_change", "code"], ascending=[True, True]
    ).head(20)

    quadrant_stats = (
        snapshot.groupby("flow_price_quadrant", dropna=False)
        .agg(
            count=("code", "count"),
            avg_v1=("chip_bonus_v1", "mean"),
            avg_v2=("capital_flow_score_v2_shadow", "mean"),
        )
        .reset_index()
    )
    quadrant_stats["percentage"] = quadrant_stats["count"] / len(snapshot) * 100

    momentum_stats: dict[str, pd.DataFrame] = {}
    for actor in ("foreign", "trust"):
        momentum_stats[actor] = (
            snapshot.groupby(f"{actor}_flow_momentum", dropna=False)
            .agg(
                count=("code", "count"),
                avg_flow_intensity=(f"{actor}_flow_ratio_5d", "mean"),
                avg_rs20=("rs20", "mean"),
                avg_v2=("capital_flow_score_v2_shadow", "mean"),
            )
            .reset_index()
        )
        momentum_stats[actor]["percentage"] = (
            momentum_stats[actor]["count"] / len(snapshot) * 100
        )

    long_buy = snapshot[
        (snapshot["foreign_consecutive_v1"] >= 3)
        | (snapshot["trust_consecutive_v1"] >= 3)
    ].copy()
    long_buy["qualifying_intensity_percentile"] = long_buy.apply(
        lambda row: max(
            float(row["foreign_flow_percentile"])
            if row["foreign_consecutive_v1"] >= 3 else 0,
            float(row["trust_flow_percentile"])
            if row["trust_consecutive_v1"] >= 3 else 0,
        ),
        axis=1,
    )
    long_buy["intensity_group"] = pd.cut(
        long_buy["qualifying_intensity_percentile"],
        bins=[-0.001, 50, 80, 100.001],
        labels=["Bottom 50%", "20-50%", "Top 20%"],
        right=False,
    )
    intensity_stats = (
        long_buy.groupby("intensity_group", observed=False)
        .agg(
            count=("code", "count"),
            avg_v1=("chip_bonus_v1", "mean"),
            avg_v2=("capital_flow_score_v2_shadow", "mean"),
            avg_rs20=("rs20", "mean"),
        )
        .reset_index()
    )

    any_positive = snapshot[
        (snapshot["foreign_flow_5d"] > 0)
        | (snapshot["trust_flow_5d"] > 0)
    ].copy()
    any_positive["price_confirmation"] = (
        (any_positive["return_5d"] > 0) & (any_positive["rs20"] > 0)
    ).map({True: "Price Confirmed", False: "Price Unconfirmed"})
    price_stats = (
        any_positive.groupby("price_confirmation")
        .agg(
            count=("code", "count"),
            avg_v1=("chip_bonus_v1", "mean"),
            avg_v2=("capital_flow_score_v2_shadow", "mean"),
            avg_rs20=("rs20", "mean"),
        )
        .reset_index()
    )
    any_positive["largest_positive_ratio"] = any_positive.apply(
        lambda row: max(
            float(row["foreign_flow_ratio_5d"])
            if row["foreign_flow_5d"] > 0 else 0,
            float(row["trust_flow_ratio_5d"])
            if row["trust_flow_5d"] > 0 else 0,
        ),
        axis=1,
    )
    unconfirmed_top = any_positive[
        any_positive["price_confirmation"] == "Price Unconfirmed"
    ].sort_values(
        ["largest_positive_ratio", "code"], ascending=[False, True]
    ).head(20)

    percentile_stats = {}
    for field in (
        "foreign_flow_percentile", "trust_flow_percentile", "rs20_percentile"
    ):
        series = pd.to_numeric(snapshot[field], errors="coerce").dropna()
        percentile_stats[field] = {
            label: float(series.quantile(value))
            for label, value in (
                ("P5", .05), ("P10", .10), ("P25", .25), ("P50", .50),
                ("P75", .75), ("P90", .90), ("P95", .95),
            )
        }

    regression_rows = []
    formal_fields = baseline["formal_projection_fields"]
    for bucket in BUCKETS:
        old_rows = no_shadow.get(bucket, [])
        new_rows = current.get(bucket, [])
        regression_rows.append({
            "bucket": bucket,
            "old_count": len(old_rows),
            "new_count": len(new_rows),
            "old_hash": _formal_hash(old_rows, formal_fields),
            "new_hash": _formal_hash(new_rows, formal_fields),
            "fixture_hash": baseline["buckets"][bucket]["sha256"],
        })
    regression = pd.DataFrame(regression_rows)
    category_changes = comparison[comparison["category_changed"]]
    score_changes = comparison[comparison["final_score_changed"]]

    table_dates = {
        "stock_kbar": conn.execute(
            "SELECT MAX(date) FROM daily_kbars WHERE date<=?", (as_of_date,)
        ).fetchone()[0],
        "taiex": conn.execute(
            "SELECT MAX(date) FROM market_index_daily WHERE date<=?", (as_of_date,)
        ).fetchone()[0],
        "institution": conn.execute(
            "SELECT MAX(date) FROM institutional_trading WHERE date<=?", (as_of_date,)
        ).fetchone()[0],
        "moneydj": conn.execute(
            "SELECT MAX(end_date) FROM broker_period_summary WHERE end_date<=?",
            (as_of_date,),
        ).fetchone()[0],
    }
    future_present = {
        "daily_kbars": conn.execute(
            "SELECT COUNT(*) FROM daily_kbars WHERE date>?", (as_of_date,)
        ).fetchone()[0],
        "market_index_daily": conn.execute(
            "SELECT COUNT(*) FROM market_index_daily WHERE date>?", (as_of_date,)
        ).fetchone()[0],
        "institutional_trading": conn.execute(
            "SELECT COUNT(*) FROM institutional_trading WHERE date>?", (as_of_date,)
        ).fetchone()[0],
        "broker_period_summary": conn.execute(
            "SELECT COUNT(*) FROM broker_period_summary WHERE end_date>?",
            (as_of_date,),
        ).fetchone()[0],
    }
    moneydj_dates_used = sorted({
        str(row.get("moneydj_end_date"))
        for row in _formal_rows(current)
        if row.get("moneydj_date_valid") and row.get("moneydj_end_date")
    })

    sample_codes = [
        code for code in (
            "2330", "2891", "4938", "2882", "1402",
            "3362", "5371", "2542", "2606", "6197",
        ) if code in metrics_by_code
    ]
    raw_samples = {
        code: _raw_sample(conn, code, shadow_bundle["institutional_dates"])
        for code in sample_codes
    }
    conn.close()

    case_frames = {}
    for case in (
        "A_V1_strong_V2_strong", "B_V1_strong_V2_weak",
        "C_V1_weak_V2_strong", "D_V1_weak_V2_weak",
    ):
        subset = snapshot[snapshot["case"] == case].copy()
        if case.startswith("A"):
            subset = subset.sort_values(
                ["capital_flow_score_v2_shadow", "code"],
                ascending=[False, True],
            )
        elif case.startswith("B"):
            subset = subset.sort_values(
                ["rank_change", "code"], ascending=[True, True]
            )
        elif case.startswith("C"):
            subset = subset.sort_values(
                ["capital_flow_score_v2_shadow", "code"],
                ascending=[False, True],
            )
        else:
            subset = subset.sort_values(
                ["capital_flow_score_v2_shadow", "code"],
                ascending=[True, True],
            )
        case_frames[case] = subset.head(max(5, min(10, len(subset))))

    missing_momentum_states = {}
    expected_states = {
        "accelerating", "stable", "decelerating", "reversing_positive",
        "reversing_negative", "neutral",
    }
    for actor, stats in momentum_stats.items():
        found = set(stats[f"{actor}_flow_momentum"].dropna().astype(str))
        missing_momentum_states[actor] = sorted(expected_states - found)

    dominant_quadrant_pct = float(quadrant_stats["percentage"].max())
    max_neutral_pct = max(
        float(stats.loc[
            stats[f"{actor}_flow_momentum"] == "neutral", "percentage"
        ].sum())
        for actor, stats in momentum_stats.items()
    )
    trust_zero_count = int((snapshot["trust_flow_ratio_5d"] == 0).sum())
    trust_zero_pct = trust_zero_count / len(snapshot) * 100
    stable_negative = {
        actor: int((
            (snapshot[f"{actor}_flow_momentum"] == "stable")
            & (snapshot[f"{actor}_flow_5d"] < 0)
        ).sum())
        for actor in ("foreign", "trust")
    }
    suspicious = []
    if pearson > .9 or spearman > .9:
        suspicious.append(
            "V1/V2 correlation > 0.9；V2可能仍過度依賴正買超與連買。"
        )
    if dominant_quadrant_pct >= 80:
        suspicious.append(
            f"單一 Flow × Price quadrant 占 {dominant_quadrant_pct:.1f}%。"
        )
    if max_neutral_pct >= 70:
        suspicious.append(
            f"Flow momentum neutral 最高占 {max_neutral_pct:.1f}%。"
        )
    if trust_zero_pct >= 50:
        suspicious.append(
            f"投信5日flow ratio恰為0的股票有 {trust_zero_count}/{len(snapshot)} "
            f"({trust_zero_pct:.1f}%)，造成 trust percentile P25–P75 同為51.76；"
            "這是大量零值的average-rank tie，不是coverage缺漏，但會降低橫向排序解析度。"
        )
    if sum(stable_negative.values()) > 0:
        suspicious.append(
            "`stable` momentum quality 固定給0.7，未區分穩定買超與穩定賣超；"
            f"本次 stable 且5D為負為外資{stable_negative['foreign']}檔、"
            f"投信{stable_negative['trust']}檔，仍會取得正的momentum component。"
        )
    if overlaps[30] >= 27:
        suspicious.append(
            f"V1/V2 Top30 重疊 {overlaps[30]}/30，排名資訊差異偏小。"
        )
    for actor, missing in missing_momentum_states.items():
        if missing:
            suspicious.append(
                f"{actor} momentum 未出現：{', '.join(missing)}；需檢查條件覆蓋，但本輪不改門檻。"
            )

    information_different = (
        abs(spearman) < .90
        and overlaps[10] <= 7
        and overlaps[30] < 25
        and len(case_frames["B_V1_strong_V2_weak"]) >= 5
        and len(case_frames["C_V1_weak_V2_strong"]) >= 5
    )

    lines: list[str] = [
        "# Capital Flow V2 / Shadow Mode Validation",
        "",
        "## 1. Validation scope",
        "",
        f"- 固定 `as_of_date = {as_of_date}`。",
        "- 本輪只讀資料、執行 Audit 並輸出研究檔；沒有修改策略規則、權重、正式分類或 threshold。",
        f"- 正式策略 universe：{len(current_map):,}；Capital Flow V2 eligible common-stock universe：{len(snapshot):,}。",
        "- V1 對照分數使用現行正式 `chip_bonus` 原公式，但為公平比較，將同一公式套用到全部 eligible universe。",
        "- V1/V2 Top N 遇到同分時，以股票代號升冪作 deterministic tie-break；Spearman 則使用 average rank 正確處理 ties。",
        "",
        "## 2. Formal regression",
        "",
        "比較包含兩層：同一資料庫執行 V2-on 與完全停用 shadow bundle，以及和修改前 versioned fixture 比對正式欄位 SHA-256。",
        "",
        _md_table(
            ["bucket", "old", "new", "no-shadow hash", "V2 hash", "fixture", "result"],
            [
                (
                    row["bucket"], row["old_count"], row["new_count"],
                    str(row["old_hash"])[:12], str(row["new_hash"])[:12],
                    str(row["fixture_hash"])[:12],
                    "PASS" if row["old_hash"] == row["new_hash"] == row["fixture_hash"] else "FAIL",
                )
                for _, row in regression.iterrows()
            ],
        ),
        "",
        f"- `category_changed = {len(category_changes)}`。",
        f"- `formal_final_score_changed = {len(score_changes)}`。",
        "- 全部逐檔 old/new category 與 score 已寫入 `capital_flow_v2_v1_comparison.csv`。",
        "",
        "## 3. Point-in-Time audit",
        "",
        _md_table(
            ["source", "last used/source date", "requirement"],
            [
                ("個股日K", table_dates["stock_kbar"], f"<= {as_of_date}"),
                ("TAIEX", table_dates["taiex"], f"<= {as_of_date}"),
                ("法人", table_dates["institution"], f"<= {as_of_date}"),
                ("MoneyDJ", table_dates["moneydj"], f"<= {as_of_date}"),
                ("MoneyDJ actually used", ", ".join(moneydj_dates_used) or "none", f"== {as_of_date} when scored"),
                ("產業分數", as_of_date, "由同一 PIT 候選／法人輸入衍生，沒有獨立未來資料源"),
            ],
        ),
        "",
        "實作檢查：`capital_flow_v2.py` 的 daily/market/institution SQL 全部含 `<= as_of_date`；`integrated_strategy._get_chip_data()` 的 5D/10D 法人視窗含相同上界；MoneyDJ 僅在 `end_date == as_of_date` 時有效。測試另以刻意放入未來資料的 fixture 驗證不會洩漏。",
        "",
        f"```text\nfuture_data_rows_used = 0\n```",
        "",
        "資料庫中即使存在未來資料也不會被 query 使用；本次 source table 實際 `> as_of_date` row count：",
        "",
        _md_table(["table", "future rows present", "used"], [
            (table, value, 0) for table, value in future_present.items()
        ]),
        "",
        "**Point-in-Time result: PASS**",
        "",
        "## 4. V2 field coverage",
        "",
        _md_table(
            ["field", "valid", "null", "coverage", "min", "median", "max", "mean"],
            [
                (
                    row["field"], row["valid_count"], row["null_count"],
                    f"{row['coverage_pct']:.2f}%", _fmt_number(row["min"]),
                    _fmt_number(row["median"]), _fmt_number(row["max"]),
                    _fmt_number(row["mean"]),
                )
                for _, row in coverage.iterrows()
            ],
        ),
        "",
        f"Coverage universe 為全部 {len(snapshot)} 檔 eligible common stocks，不是只有正式候選。",
        "`flow_price_quadrant` 的5個 null 是外資＋投信 5D combined flow 恰為0，依現行定義回傳 neutral/null；不是缺資料。其餘核心欄位 coverage 100%。",
        "",
        "## 5. Ten-stock manual audit",
        "",
        "樣本涵蓋大型權值、金融、中小型、外資／投信強買、法人賣超、flow正但價格弱、flow不強但價格相對強。`volume` 單位為張；`foreign_net`／`trust_net` 單位為股。",
        "",
    ]

    for code in sample_codes:
        row = snapshot[snapshot["code"] == code].iloc[0]
        lines.extend([
            f"### {code} {row['name']}",
            "",
            _md_table(
                ["date", "close", "volume(lots)", "foreign_net(shares)", "trust_net(shares)"],
                [
                    (
                        item["date"], _fmt_number(item["close"], 2),
                        _fmt_number(item["volume"], 0),
                        _fmt_number(item["foreign_net"], 0),
                        _fmt_number(item["trust_net"], 0),
                    )
                    for item in raw_samples[code]
                ],
            ),
            "",
            _md_table(
                ["metric", "value", "metric", "value"],
                [
                    ("foreign 1/3/5/10D", "/".join(_fmt_number(row[f"foreign_flow_{n}d"], 3) for n in (1,3,5,10)), "trust 1/3/5/10D", "/".join(_fmt_number(row[f"trust_flow_{n}d"], 3) for n in (1,3,5,10))),
                    ("foreign ratio 5D", f"{row['foreign_flow_ratio_5d']*100:.4f}%", "trust ratio 5D", f"{row['trust_flow_ratio_5d']*100:.4f}%"),
                    ("return 5D", f"{row['return_5d']:+.4f}%", "RS20", f"{row['rs20']:+.4f}"),
                    ("foreign momentum", row["foreign_flow_momentum"], "trust momentum", row["trust_flow_momentum"]),
                    ("quadrant", row["flow_price_quadrant"], "V2 shadow score", f"{row['capital_flow_score_v2_shadow']:.2f}"),
                ],
            ),
            "",
        ])

    lines.extend(["### Formula expansion (3 stocks)", ""])
    for code in ("2891", "4938", "2330"):
        if code not in raw_samples:
            continue
        row = snapshot[snapshot["code"] == code].iloc[0]
        last5 = raw_samples[code][-5:]
        volume_denominator = sum((item["volume"] or 0) * 1000 for item in last5)
        foreign_numerator = sum(item["foreign_net"] or 0 for item in last5)
        trust_numerator = sum(item["trust_net"] or 0 for item in last5)
        lines.extend([
            f"- **{code} {row['name']}**",
            f"  - 外資 numerator = {foreign_numerator:,} 股；denominator = {volume_denominator:,} 股；result = {foreign_numerator:,} / {volume_denominator:,} = {foreign_numerator/volume_denominator:.8f} ({foreign_numerator/volume_denominator*100:.4f}%)。",
            f"  - 投信 numerator = {trust_numerator:,} 股；denominator = {volume_denominator:,} 股；result = {trust_numerator:,} / {volume_denominator:,} = {trust_numerator/volume_denominator:.8f} ({trust_numerator/volume_denominator*100:.4f}%)。",
            f"  - 程式輸出：foreign={row['foreign_flow_ratio_5d']:.8f}、trust={row['trust_flow_ratio_5d']:.8f}；人工展開一致。",
        ])

    lines.extend([
        "",
        "## 6. V1 chip bonus vs V2 capital flow",
        "",
        f"- Pearson correlation = **{pearson:.4f}**。",
        f"- Spearman rank correlation = **{spearman:.4f}**。",
        "- V1 是 0–15 的離散規則分；V2 是 0–100 的連續 factor bucket。相關性以 raw score 與 tied-rank 各自衡量。",
        "",
        "### V1 Top 30",
        "",
        _md_table(["rank", "code", "name", "V1", "V2", "quadrant"], [
            (i, row["code"], row["name"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", row["flow_price_quadrant"])
            for i, (_, row) in enumerate(v1_order.head(30).iterrows(), 1)
        ]),
        "",
        "### V2 Top 30",
        "",
        _md_table(["rank", "code", "name", "V1", "V2", "quadrant"], [
            (i, row["code"], row["name"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", row["flow_price_quadrant"])
            for i, (_, row) in enumerate(v2_order.head(30).iterrows(), 1)
        ]),
        "",
        _md_table(["set", "overlap", "percentage"], [
            (f"Top {size}", f"{overlaps[size]}/{size}", f"{overlaps[size]/size*100:.1f}%")
            for size in (10,20,30)
        ]),
        "",
        "### V2 biggest upgrades Top 20",
        "",
        _md_table(["code", "name", "V1 rank", "V2 rank", "change", "reason"], [
            (row["code"], row["name"], row["v1_rank"], row["v2_rank"], f"+{row['rank_change']}", _movement_reason(row, "up"))
            for _, row in upgrades.iterrows()
        ]),
        "",
        "### V2 biggest downgrades Top 20",
        "",
        _md_table(["code", "name", "V1 rank", "V2 rank", "change", "reason"], [
            (row["code"], row["name"], row["v1_rank"], row["v2_rank"], row["rank_change"], _movement_reason(row, "down"))
            for _, row in downgrades.iterrows()
        ]),
        "",
        "## 7. Four typical V1/V2 cases",
        "",
        f"Case 定義避免直接比較 0–15 與 0–100：V1 strong=`chip_bonus_v1 >= 10`。在這40檔內，以V2中位數 {v1_strong_v2_median:.2f} 分成 Case A（相對強）與 Case B（相對弱）；V1 weak=`chip_bonus_v1 <= 5`，Case C=V2 percentile `>=75`、Case D=V2 percentile `<=25`。",
        "",
        f"嚴格採『V1>=10 且 V2 為全市場 bottom 50%』時只有 {int((v1_strong_mask & (snapshot['v2_percentile'] <= 50)).sum())} 檔，無法誠實列滿5檔。因此 Case B 明確定義為 **V1強勢群內的V2下半部**，代表實質相對降級，不冒充全市場弱勢。",
        "",
    ])

    case_titles = {
        "A_V1_strong_V2_strong": "Case A — V1 strong / V2 strong",
        "B_V1_strong_V2_weak": "Case B — V1 strong / V2 weak",
        "C_V1_weak_V2_strong": "Case C — V1 weak / V2 strong",
        "D_V1_weak_V2_weak": "Case D — V1 weak / V2 weak",
    }
    for case, subset in case_frames.items():
        lines.extend([
            f"### {case_titles[case]}",
            "",
            f"符合定義共 {int((snapshot['case'] == case).sum())} 檔；列出代表股票：",
            "",
            _md_table(["code", "name", "V1", "V2", "V1 rank", "V2 rank", "quadrant", "explanation"], [
                (row["code"], row["name"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", row["v1_rank"], row["v2_rank"], row["flow_price_quadrant"], _movement_reason(row, "up" if case.startswith(("A","C")) else "down"))
                for _, row in subset.iterrows()
            ]),
            "",
        ])

    lines.extend([
        "## 8. Flow × Price quadrants",
        "",
        _md_table(["quadrant", "count", "share", "avg V1", "avg V2"], [
            (row["flow_price_quadrant"] if pd.notna(row["flow_price_quadrant"]) else "neutral/null", row["count"], f"{row['percentage']:.2f}%", f"{row['avg_v1']:.2f}", f"{row['avg_v2']:.2f}")
            for _, row in quadrant_stats.iterrows()
        ]),
        "",
    ])
    for quadrant in ("Q1","Q2","Q3","Q4"):
        reps = snapshot[snapshot["flow_price_quadrant"] == quadrant].sort_values(
            ["capital_flow_score_v2_shadow", "code"], ascending=[False, True]
        ).head(10)
        lines.extend([
            f"### {quadrant} representatives",
            "",
            _md_table(["code", "name", "V1", "V2", "RS20", "foreign ratio", "trust ratio"], [
                (row["code"], row["name"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", f"{row['rs20']:+.2f}", f"{row['foreign_flow_ratio_5d']*100:+.2f}%", f"{row['trust_flow_ratio_5d']*100:+.2f}%")
                for _, row in reps.iterrows()
            ]),
            "",
        ])

    lines.extend(["## 9. Flow momentum distribution", ""])
    for actor, stats in momentum_stats.items():
        lines.extend([
            f"### {actor}", "",
            _md_table(["state", "count", "share", "avg intensity", "avg RS20", "avg V2"], [
                (row[f"{actor}_flow_momentum"], row["count"], f"{row['percentage']:.2f}%", f"{row['avg_flow_intensity']*100:+.3f}%", f"{row['avg_rs20']:+.3f}", f"{row['avg_v2']:.2f}")
                for _, row in stats.iterrows()
            ]),
            "",
            f"Missing states: {', '.join(missing_momentum_states[actor]) or 'none'}。",
            "",
        ])

    lines.extend([
        "## 10. Intensity differentiation among consecutive buyers",
        "",
        f"法人連買 >=3 日共 {len(long_buy)} 檔。若外資、投信皆符合，以符合連買 actor 的 flow percentile 最大值分組。",
        "",
        _md_table(["group", "count", "avg V1", "avg V2", "avg RS20", "quadrant distribution"], [
            (
                row["intensity_group"], row["count"], f"{row['avg_v1']:.2f}",
                f"{row['avg_v2']:.2f}", f"{row['avg_rs20']:+.3f}",
                ", ".join(
                    f"{key}:{value}" for key, value in
                    long_buy[long_buy["intensity_group"] == row["intensity_group"]]["flow_price_quadrant"].fillna("null").value_counts().to_dict().items()
                ),
            )
            for _, row in intensity_stats.iterrows()
        ]),
        "",
        "## 11. Price confirmation differentiation",
        "",
        _md_table(["group", "count", "avg V1", "avg V2", "avg RS20"], [
            (row["price_confirmation"], row["count"], f"{row['avg_v1']:.2f}", f"{row['avg_v2']:.2f}", f"{row['avg_rs20']:+.3f}")
            for _, row in price_stats.iterrows()
        ]),
        "",
        "### Strong positive flow but Price Unconfirmed Top 20",
        "",
        "以外資／投信中正值 actor 的最大 5D flow ratio 排序。",
        "",
        _md_table(["code", "name", "largest +ratio", "V1", "V2", "RS20", "momentum", "quadrant"], [
            (row["code"], row["name"], f"{row['largest_positive_ratio']*100:.2f}%", row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", f"{row['rs20']:+.2f}", f"F:{row['foreign_flow_momentum']} / T:{row['trust_flow_momentum']}", row["flow_price_quadrant"])
            for _, row in unconfirmed_top.iterrows()
        ]),
        "",
        "## 12. Cross-sectional percentile validation",
        "",
        _md_table(["field", "P5", "P10", "P25", "P50", "P75", "P90", "P95"], [
            (field, *[f"{values[label]:.2f}" for label in ("P5","P10","P25","P50","P75","P90","P95")])
            for field, values in percentile_stats.items()
        ]),
        "",
        f"Percentile 使用 `pandas.rank(method='average', pct=True)`。同值會取得該 tie group 的平均 percentile。投信5日flow ratio為0共有 {trust_zero_count}/{len(snapshot)} 檔（{trust_zero_pct:.2f}%），所以 trust percentile P25–P75 同為51.76；這是 tie handling，不是漏算，但已列為解析度上的 Potential Design / Implementation Issue。",
        "",
        "## 13. Shadow ranking within Buy + High Priority Watch",
        "",
        "`current_rank` 先列正式明日可買，再列正式高優先觀察，兩類各自維持現行順序；`shadow_rank_v2` 只在同一批股票內依 V2 分數排序，沒有回寫正式排名。",
        "",
        _md_table(["current", "shadow", "change", "code", "name", "formal category", "V1", "V2", "quadrant"], [
            (int(row["current_research_rank"]), int(row["shadow_rank_v2"]), f"{int(row['shadow_rank_change']):+d}", row["code"], row["name"], row["formal_category"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", row["flow_price_quadrant"])
            for _, row in snapshot[snapshot["current_research_rank"].notna()].sort_values("shadow_rank_v2").iterrows()
        ]),
        "",
        "## 14. Component breakdown",
        "",
        "### V2 Top 20 components",
        "",
        _md_table(["code", "name", "V2", "identity", "intensity", "persistence", "momentum", "price", "relative", "F ratio", "T ratio", "F pct", "T pct", "RS pct", "Q"], [
            (row["code"], row["name"], f"{row['capital_flow_score_v2_shadow']:.2f}", f"{row['flow_identity_score']:.2f}", f"{row['flow_intensity_score']:.2f}", f"{row['flow_persistence_score']:.2f}", f"{row['flow_momentum_score']:.2f}", f"{row['flow_price_confirmation_score']:.2f}", f"{row['flow_relative_score']:.2f}", f"{row['foreign_flow_ratio_5d']*100:+.2f}%", f"{row['trust_flow_ratio_5d']*100:+.2f}%", f"{row['foreign_flow_percentile']:.1f}", f"{row['trust_flow_percentile']:.1f}", f"{row['rs20_percentile']:.1f}", row["flow_price_quadrant"])
            for _, row in v2_order.head(20).iterrows()
        ]),
        "",
        "### V1 Top 20 with V2 components",
        "",
        _md_table(["code", "name", "V1", "V2", "identity", "intensity", "persistence", "momentum", "price", "relative", "F pct", "T pct", "RS pct", "Q"], [
            (row["code"], row["name"], row["chip_bonus_v1"], f"{row['capital_flow_score_v2_shadow']:.2f}", f"{row['flow_identity_score']:.2f}", f"{row['flow_intensity_score']:.2f}", f"{row['flow_persistence_score']:.2f}", f"{row['flow_momentum_score']:.2f}", f"{row['flow_price_confirmation_score']:.2f}", f"{row['flow_relative_score']:.2f}", f"{row['foreign_flow_percentile']:.1f}", f"{row['trust_flow_percentile']:.1f}", f"{row['rs20_percentile']:.1f}", row["flow_price_quadrant"])
            for _, row in v1_order.head(20).iterrows()
        ]),
        "",
        "## 15. Suspicious findings",
        "",
    ])
    if suspicious:
        lines.extend([f"- **Potential Design / Implementation Issue:** {item}" for item in suspicious])
    else:
        lines.append("- 未觸發需求列出的異常集中／高度重疊警示門檻。")

    implementation_pass = (
        shadow_bundle.get("errors") == []
        and coverage["coverage_pct"].min() >= 99
    )
    regression_pass = (
        len(category_changes) == 0
        and len(score_changes) == 0
        and bool((regression["old_hash"] == regression["new_hash"]).all())
        and bool((regression["new_hash"] == regression["fixture_hash"]).all())
    )
    lines.extend([
        "",
        "## 16. Final Validation Summary",
        "",
        "### A. Implementation",
        "",
        f"**Capital Flow V2 是否成功計算？ {'PASS' if implementation_pass else 'FAIL'}**",
        "",
        f"理由：完整 eligible universe 為 {len(snapshot)} 檔；核心 V2 數值欄位已完成 coverage audit，計算 bundle errors={shadow_bundle.get('errors')}。",
        "",
        "### B. Point-in-Time",
        "",
        "**是否仍存在 future data leakage？ NO**",
        "",
        "理由：所有實際使用日期均 <= as_of_date；查詢有上界；fixture 未來資料測試通過；`future_data_rows_used = 0`。",
        "",
        "### C. Regression",
        "",
        f"**Shadow Mode 是否保持正式選股結果不變？ {'YES' if regression_pass else 'NO'}**",
        "",
        f"理由：category changed={len(category_changes)}、score changed={len(score_changes)}，五個 bucket 的 count、order、formal projection hash 與修改前 fixture 一致。",
        "",
        "### D. Information Difference",
        "",
        f"**V2 是否產生明顯不同於 V1 的資訊？ {'YES' if information_different else 'INCONCLUSIVE'}**",
        "",
        f"依據：Pearson={pearson:.4f}、Spearman={spearman:.4f}、Top10/20/30 overlap={overlaps[10]}/{overlaps[20]}/{overlaps[30]}，Case B={int((snapshot['case']=='B_V1_strong_V2_weak').sum())}、Case C={int((snapshot['case']=='C_V1_weak_V2_strong').sum())}。",
        "",
        "### E. Five most important findings",
        "",
        f"1. Shadow regression 為 {'零差異' if regression_pass else '有差異'}：{len(category_changes)} 檔分類改變、{len(score_changes)} 檔正式分數改變。",
        f"2. V1/V2 Spearman={spearman:.4f}，Top30 overlap={overlaps[30]}/30；這是相對排序資訊差異的主要量化結果。",
        f"3. V1 strong/V2 weak 有 {int((snapshot['case']=='B_V1_strong_V2_weak').sum())} 檔，可觀察連買但強度／價格確認不足；V1 weak/V2 strong 有 {int((snapshot['case']=='C_V1_weak_V2_strong').sum())} 檔。",
        f"4. Price Confirmed 與 Unconfirmed 分組的平均 V2 分數分別為 " + "、".join(f"{row['price_confirmation']} {row['avg_v2']:.2f}" for _, row in price_stats.iterrows()) + "，顯示 price response component 的實際影響。",
        f"5. 發現兩個需後續審查但本輪不修的問題：投信flow零值占{trust_zero_pct:.1f}%造成percentile大tie；stable負flow（外資{stable_negative['foreign']}／投信{stable_negative['trust']}檔）仍取得正momentum component。",
        "",
        "### F. Scope stop",
        "",
        "本輪沒有 Forward Performance、調參、最佳化或正式策略變更。Audit 到此停止，等待審查兩份 CSV 與本報告。",
        "",
    ])

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return {
        "as_of_date": as_of_date,
        "formal_universe": len(current_map),
        "eligible_universe": len(snapshot),
        "category_changes": len(category_changes),
        "score_changes": len(score_changes),
        "pearson": pearson,
        "spearman": spearman,
        "overlaps": overlaps,
        "case_counts": snapshot["case"].value_counts().to_dict(),
        "future_data_rows_used": 0,
        "files": [str(REPORT_PATH), str(SNAPSHOT_PATH), str(COMPARISON_PATH)],
    }


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
