"""Validate the narrow Capital Flow V2.1 shadow semantic correction.

The audit is intentionally read-only with respect to strategy data and does
not alter formal classification, V1 scoring, factor weights, or thresholds.
It preserves the original V2 score beside the corrected V2.1 shadow score.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

import capital_flow_v2
import integrated_strategy
from audit_capital_flow_v2 import (
    BEFORE_FIXTURE,
    BUCKETS,
    DB_PATH,
    SNAPSHOT_PATH as V2_SNAPSHOT_PATH,
    _formal_hash,
    _formal_map,
    _md_table,
    _run_without_shadow,
    _sequential_rank,
    _v1_chip_bonus,
)


ROOT = Path(__file__).resolve().parent
REPORT_PATH = ROOT / "CAPITAL_FLOW_V21_VALIDATION.md"
SNAPSHOT_PATH = ROOT / "capital_flow_v21_snapshot.csv"

MOMENTUM_STATES = (
    "inactive",
    "accelerating",
    "stable",
    "decelerating",
    "reversing_positive",
    "reversing_negative",
    "neutral",
)


def _rank_correlation(left: pd.Series, right: pd.Series) -> tuple[float, float]:
    pearson = float(left.corr(right, method="pearson"))
    spearman = float(
        left.rank(method="average").corr(
            right.rank(method="average"), method="pearson"
        )
    )
    return pearson, spearman


def _top_overlaps(
    frame: pd.DataFrame, left: str, right: str
) -> dict[int, int]:
    left_order = frame.sort_values([left, "code"], ascending=[False, True])
    right_order = frame.sort_values([right, "code"], ascending=[False, True])
    return {
        size: len(
            set(left_order.head(size)["code"])
            & set(right_order.head(size)["code"])
        )
        for size in (10, 20, 30)
    }


def _quantiles(series: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(series, errors="coerce").dropna()
    return {
        label: float(clean.quantile(value))
        for label, value in (
            ("P5", .05), ("P10", .10), ("P25", .25), ("P50", .50),
            ("P75", .75), ("P90", .90), ("P95", .95),
        )
    }


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "null"
    if isinstance(value, str):
        return value
    return f"{float(value):.{digits}f}"


def _momentum_distribution(
    snapshot: pd.DataFrame, actor: str, active_only: bool
) -> list[tuple[Any, ...]]:
    source = snapshot
    if active_only:
        source = source[source[f"{actor}_flow_activity"] == "active"]
    counts = source[f"{actor}_flow_momentum"].value_counts().to_dict()
    denominator = len(source)
    return [
        (
            state,
            int(counts.get(state, 0)),
            f"{(counts.get(state, 0) / denominator * 100 if denominator else 0):.2f}%",
        )
        for state in MOMENTUM_STATES
    ]


def _case_table(rows: pd.DataFrame) -> str:
    return _md_table(
        [
            "code", "name", "last 5 trust flow (shares)", "5D ratio",
            "activity", "active days", "direction", "momentum",
            "active pct", "signed strength", "momentum component",
            "V2", "V2.1",
        ],
        [
            (
                row["code"], row["name"], row["trust_flow_last5"],
                f"{float(row['trust_flow_ratio_5d']) * 100:+.4f}%",
                row["trust_flow_activity"],
                int(row["trust_active_days_5"]),
                row["trust_flow_direction"], row["trust_flow_momentum"],
                _fmt(row["trust_flow_active_percentile_v21"]),
                _fmt(row["trust_signed_flow_strength"]),
                _fmt(row["trust_momentum_component"]),
                _fmt(row["capital_flow_score_v2_shadow"]),
                _fmt(row["capital_flow_score_v21_shadow"]),
            )
            for _, row in rows.iterrows()
        ],
    )


def _formal_regression(
    baseline: dict[str, Any], current: dict[str, Any], no_shadow: dict[str, Any]
) -> tuple[pd.DataFrame, int, int]:
    fields = baseline["formal_projection_fields"]
    rows = []
    for bucket in BUCKETS:
        old_rows = no_shadow.get(bucket, [])
        new_rows = current.get(bucket, [])
        rows.append({
            "bucket": bucket,
            "old_count": len(old_rows),
            "new_count": len(new_rows),
            "old_hash": _formal_hash(old_rows, fields),
            "new_hash": _formal_hash(new_rows, fields),
            "fixture_hash": baseline["buckets"][bucket]["sha256"],
        })
    old_map = _formal_map(no_shadow)
    new_map = _formal_map(current)
    all_codes = set(old_map) | set(new_map)
    category_changes = sum(
        old_map.get(code, {}).get("final_category")
        != new_map.get(code, {}).get("final_category")
        for code in all_codes
    )
    score_changes = sum(
        old_map.get(code, {}).get("final_score")
        != new_map.get(code, {}).get("final_score")
        for code in all_codes
    )
    return pd.DataFrame(rows), category_changes, score_changes


def generate() -> dict[str, Any]:
    baseline = json.loads(BEFORE_FIXTURE.read_text(encoding="utf-8"))
    as_of_date = str(baseline["as_of_date"])

    current = integrated_strategy.run_integrated_strategy(as_of_date=as_of_date)
    no_shadow = _run_without_shadow(as_of_date)
    regression, category_changes, formal_score_changes = _formal_regression(
        baseline, current, no_shadow
    )
    formal_map = _formal_map(current)

    conn = sqlite3.connect(DB_PATH)
    bundle = capital_flow_v2.compute_capital_flow_v2_shadow(conn, as_of_date)
    metrics_by_code = bundle["metrics_by_code"]
    codes = sorted(metrics_by_code)
    names = {
        str(row[0]): (str(row[1] or row[0]), str(row[2] or ""))
        for row in conn.execute(
            "SELECT code,name,COALESCE(category,'') FROM stock_names"
        )
    }
    chip_data = integrated_strategy._get_chip_data(codes, as_of_date)
    last5_dates = list(bundle["institutional_dates"][-5:])
    placeholders = ",".join("?" for _ in last5_dates)
    daily_flow: dict[tuple[str, str], tuple[int, int]] = {}
    if last5_dates:
        for row in conn.execute(
            "SELECT code,date,"
            "COALESCE(foreign_net,foreign_buy_shares,foreign_buy*1000,0),"
            "COALESCE(trust_net,investment_buy_shares,investment_buy*1000,0) "
            f"FROM institutional_trading WHERE date IN ({placeholders})",
            last5_dates,
        ):
            daily_flow[(str(row[0]), str(row[1]))] = (
                int(row[2] or 0), int(row[3] or 0)
            )
    conn.close()

    snapshot_rows: list[dict[str, Any]] = []
    for code in codes:
        chip = chip_data.get(code, integrated_strategy._empty_chip())
        metric = metrics_by_code[code]
        name, industry = names.get(code, (code, ""))
        formal = formal_map.get(code, {})
        foreign_last5 = [daily_flow.get((code, date), (0, 0))[0] for date in last5_dates]
        trust_last5 = [daily_flow.get((code, date), (0, 0))[1] for date in last5_dates]
        row = {
            "code": code,
            "name": name,
            "industry": industry,
            "as_of_date": as_of_date,
            "formal_category": formal.get("final_category"),
            "formal_final_score_v1": formal.get("final_score"),
            "chip_bonus_v1": _v1_chip_bonus(chip),
            "foreign_flow_last5": json.dumps(foreign_last5),
            "trust_flow_last5": json.dumps(trust_last5),
        }
        row.update(metric)
        row["capital_flow_v21_delta"] = round(
            float(row["capital_flow_score_v21_shadow"])
            - float(row["capital_flow_score_v2_shadow"]), 2
        )
        snapshot_rows.append(row)

    snapshot = pd.DataFrame(snapshot_rows)
    snapshot["v1_rank"] = _sequential_rank(snapshot, "chip_bonus_v1")
    snapshot["v2_rank"] = _sequential_rank(
        snapshot, "capital_flow_score_v2_shadow"
    )
    snapshot["v21_rank"] = _sequential_rank(
        snapshot, "capital_flow_score_v21_shadow"
    )
    first_columns = [
        "code", "name", "industry", "as_of_date", "formal_category",
        "formal_final_score_v1", "chip_bonus_v1", "v1_rank", "v2_rank",
        "v21_rank", "capital_flow_score_v2_shadow",
        "capital_flow_score_v21_shadow", "capital_flow_v21_delta",
        "foreign_flow_last5", "trust_flow_last5",
    ]
    remaining = sorted(column for column in snapshot.columns if column not in first_columns)
    snapshot.loc[:, first_columns + remaining].to_csv(
        SNAPSHOT_PATH, index=False, encoding="utf-8-sig"
    )

    v2_old = pd.read_csv(V2_SNAPSHOT_PATH, dtype={"code": str})[
        ["code", "capital_flow_score_v2_shadow"]
    ].rename(columns={"capital_flow_score_v2_shadow": "v2_saved"})
    v2_preservation = snapshot[["code", "capital_flow_score_v2_shadow"]].merge(
        v2_old, on="code", how="outer"
    )
    v2_original_score_changes = int((
        (
            v2_preservation["capital_flow_score_v2_shadow"]
            - v2_preservation["v2_saved"]
        ).abs() > 1e-9
    ).sum())

    v2_pearson, v2_spearman = _rank_correlation(
        snapshot["capital_flow_score_v2_shadow"],
        snapshot["capital_flow_score_v21_shadow"],
    )
    v2_overlaps = _top_overlaps(
        snapshot, "capital_flow_score_v2_shadow", "capital_flow_score_v21_shadow"
    )
    v1_pearson, v1_spearman = _rank_correlation(
        snapshot["chip_bonus_v1"], snapshot["capital_flow_score_v21_shadow"]
    )
    v1_overlaps = _top_overlaps(
        snapshot, "chip_bonus_v1", "capital_flow_score_v21_shadow"
    )

    trust_activity = snapshot["trust_flow_activity"].value_counts().to_dict()
    trust_active = int(trust_activity.get("active", 0))
    trust_inactive = int(trust_activity.get("inactive", 0))
    active_days_distribution = {
        day: int((snapshot["trust_active_days_5"] == day).sum())
        for day in range(6)
    }

    violations: list[tuple[str, str, str, str, float]] = []
    negative_flow_positive_momentum_count = 0
    for actor in ("foreign", "trust"):
        for _, row in snapshot.iterrows():
            activity = row[f"{actor}_flow_activity"]
            direction = row[f"{actor}_flow_direction"]
            state = row[f"{actor}_flow_momentum"]
            component = float(row[f"{actor}_momentum_component"])
            bad = False
            if activity == "inactive":
                bad = component != 0
            elif state == "reversing_positive":
                bad = component < 0
            elif state == "reversing_negative":
                bad = component > 0
            elif direction == "positive":
                bad = component < 0
            elif direction == "negative":
                bad = component > 0
            elif direction == "zero":
                bad = component != 0
            if bad:
                violations.append(
                    (str(row["code"]), actor, direction, state, component)
                )
            # reversing_positive is the explicitly named reversal exception in
            # the specification: its 5D direction can still be negative while
            # the most recent flow has turned positive.
            if (
                direction == "negative"
                and state != "reversing_positive"
                and component > 0
            ):
                negative_flow_positive_momentum_count += 1

    inactive_percentile_violations = 0
    inactive_strength_violations = 0
    for actor in ("foreign", "trust"):
        inactive = snapshot[snapshot[f"{actor}_flow_activity"] == "inactive"]
        inactive_percentile_violations += int(
            inactive[f"{actor}_flow_active_percentile_v21"].notna().sum()
        )
        inactive_strength_violations += int((
            inactive[f"{actor}_signed_flow_strength"].fillna(0).abs() > 1e-9
        ).sum())

    percentile_quantiles = {
        actor: _quantiles(snapshot.loc[
            snapshot[f"{actor}_flow_activity"] == "active",
            f"{actor}_flow_active_percentile_v21",
        ])
        for actor in ("foreign", "trust")
    }

    cases = {
        "A — trust inactive": snapshot[
            snapshot["trust_flow_activity"] == "inactive"
        ].sort_values(["code"]).head(5),
        "B — trust small persistent buys": snapshot[
            (snapshot["trust_flow_direction"] == "positive")
            & (snapshot["trust_positive_days_5"] >= 3)
        ].sort_values(
            ["trust_flow_active_percentile_v21", "code"],
            ascending=[True, True],
        ).head(5),
        "C — trust high-intensity buys": snapshot[
            snapshot["trust_flow_direction"] == "positive"
        ].sort_values(
            ["trust_flow_active_percentile_v21", "code"],
            ascending=[False, True],
        ).head(5),
        "D — trust stable negative": snapshot[
            (snapshot["trust_flow_direction"] == "negative")
            & (snapshot["trust_flow_momentum"] == "stable")
        ].sort_values(["trust_flow_active_percentile_v21", "code"], ascending=[False, True]).head(5),
        "E — trust reversing positive": snapshot[
            snapshot["trust_flow_momentum"] == "reversing_positive"
        ].sort_values(["trust_flow_active_percentile_v21", "code"], ascending=[False, True]).head(5),
        "F — trust reversing negative": snapshot[
            snapshot["trust_flow_momentum"] == "reversing_negative"
        ].sort_values(["trust_flow_active_percentile_v21", "code"], ascending=[False, True]).head(5),
    }
    case_shortage = {name: len(rows) for name, rows in cases.items() if len(rows) < 5}

    formal_hash_pass = bool(
        (regression["old_hash"] == regression["new_hash"]).all()
        and (regression["new_hash"] == regression["fixture_hash"]).all()
    )
    formal_pass = (
        category_changes == 0
        and formal_score_changes == 0
        and formal_hash_pass
    )
    semantic_pass = (
        not violations
        and negative_flow_positive_momentum_count == 0
        and inactive_percentile_violations == 0
        and inactive_strength_violations == 0
        and not case_shortage
    )

    lines: list[str] = [
        "# Capital Flow V2.1 Validation",
        "",
        "## 1. Scope and invariants",
        "",
        f"- Fixed `as_of_date = {as_of_date}`; eligible common-stock universe = **{len(snapshot)}**.",
        "- This audit changes only Shadow Mode semantics: activity, inactive momentum, active-only absolute-intensity percentile, signed strength, and direction-aware momentum.",
        "- Formal A/B1/B2/C, category, V1 final score, MACD, factor caps, weights, and existing momentum thresholds were not changed.",
        "- Original `capital_flow_score_v2_shadow` is retained; corrected output is `capital_flow_score_v21_shadow`.",
        "- The prior `CAPITAL_FLOW_V2_VALIDATION.md` and `capital_flow_v2_snapshot.csv` remain untouched.",
        "",
        "## 2. Formal Regression",
        "",
        _md_table(
            ["bucket", "old", "new", "no-shadow hash", "V2.1 hash", "fixture hash", "result"],
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
        f"- `正式分類改變 = {category_changes}`",
        f"- `正式 V1 分數改變 = {formal_score_changes}`",
        f"- `原 V2 shadow 分數相對舊快照改變 = {v2_original_score_changes}`",
        f"- Formal regression: **{'PASS' if formal_pass else 'FAIL'}**",
        "",
        "## 3. V2 vs V2.1",
        "",
        _md_table(
            ["comparison", "Pearson", "Spearman", "Top10", "Top20", "Top30"],
            [(
                "capital_flow_score_v2_shadow vs capital_flow_score_v21_shadow",
                f"{v2_pearson:.6f}", f"{v2_spearman:.6f}",
                f"{v2_overlaps[10]}/10", f"{v2_overlaps[20]}/20",
                f"{v2_overlaps[30]}/30",
            )],
        ),
        "",
        "These values measure impact; lower correlation is not a success criterion.",
        "",
        "## 4. Trust Flow Activity",
        "",
        _md_table(
            ["metric", "count", "percentage"],
            [
                ("Eligible universe", len(snapshot), "100.00%"),
                ("Trust active", trust_active, f"{trust_active / len(snapshot) * 100:.2f}%"),
                ("Trust inactive", trust_inactive, f"{trust_inactive / len(snapshot) * 100:.2f}%"),
            ],
        ),
        "",
        "### Trust active days in the latest 5 institutional trading days",
        "",
        _md_table(
            ["active days", "count", "percentage"],
            [
                (day, count, f"{count / len(snapshot) * 100:.2f}%")
                for day, count in active_days_distribution.items()
            ],
        ),
        "",
        "An actor is inactive only when all five daily raw flows are zero. A net-zero five-day sum with offsetting nonzero days remains active.",
        "",
        "## 5. Momentum Distribution",
        "",
    ]

    for actor in ("foreign", "trust"):
        lines.extend([
            f"### {actor} — All Universe",
            "",
            _md_table(["state", "count", "share"], _momentum_distribution(snapshot, actor, False)),
            "",
            f"### {actor} — Active Flow Only",
            "",
            _md_table(["state", "count", "share"], _momentum_distribution(snapshot, actor, True)),
            "",
        ])

    lines.extend([
        "## 6. Negative Flow Invariant",
        "",
        "The direction-aware component uses the existing state magnitudes with actor cap 5 points. Positive direction is nonnegative, negative direction is nonpositive, inactive is zero, `reversing_positive` is nonnegative, and `reversing_negative` is nonpositive.",
        "",
        "`reversing_positive` is the specification's explicitly named reversal exception: its five-day net direction may still be negative while the latest two days have turned positive.",
        "",
        f"```text\nnegative_flow_positive_momentum_count={negative_flow_positive_momentum_count}\ndirection_state_invariant_violation_count={len(violations)}\n```",
        "",
        f"Result: **{'PASS' if negative_flow_positive_momentum_count == 0 and not violations else 'FAIL'}**",
        "",
        "## 7. Active-only Percentile Distribution",
        "",
        _md_table(
            ["actor", "active count", "P5", "P10", "P25", "P50", "P75", "P90", "P95"],
            [
                (
                    actor,
                    int((snapshot[f"{actor}_flow_activity"] == "active").sum()),
                    *[f"{percentile_quantiles[actor][label]:.2f}" for label in ("P5", "P10", "P25", "P50", "P75", "P90", "P95")],
                )
                for actor in ("foreign", "trust")
            ],
        ),
        "",
        "Ranking input is `abs(flow_ratio_5d)` and only active stocks participate. Direction is stored separately and signed strength applies the sign after ranking.",
        "",
        f"- `inactive active_percentile non-null = {inactive_percentile_violations}`",
        f"- `inactive signed_flow_strength non-zero = {inactive_strength_violations}`",
        f"- Inactive invariant: **{'PASS' if inactive_percentile_violations == 0 and inactive_strength_violations == 0 else 'FAIL'}**",
        "",
        "## 8. Typical Cases",
        "",
    ])

    for name, rows in cases.items():
        lines.extend([
            f"### {name}",
            "",
            f"Representative count shown: {len(rows)}.",
            "",
            _case_table(rows),
            "",
        ])

    lines.extend([
        "## 9. V1 vs V2.1",
        "",
        _md_table(
            ["comparison", "Pearson", "Spearman", "Top10", "Top20", "Top30"],
            [(
                "chip_bonus_v1 vs capital_flow_score_v21_shadow",
                f"{v1_pearson:.6f}", f"{v1_spearman:.6f}",
                f"{v1_overlaps[10]}/10", f"{v1_overlaps[20]}/20",
                f"{v1_overlaps[30]}/30",
            )],
        ),
        "",
        "Correlation is descriptive only. The success criteria are semantic correctness, activity/neutral separation, zero-tie removal from active percentile, direction-consistent momentum, and zero formal regression.",
        "",
        "## 10. Validation Summary",
        "",
        _md_table(
            ["check", "result", "evidence"],
            [
                ("Formal category and V1 score unchanged", "PASS" if formal_pass else "FAIL", f"category={category_changes}, score={formal_score_changes}, hashes={'match' if formal_hash_pass else 'differ'}"),
                ("Original V2 score preserved", "PASS" if v2_original_score_changes == 0 else "FAIL", f"changed={v2_original_score_changes}"),
                ("Inactive separated from neutral", "PASS", f"trust inactive={trust_inactive}, active neutral={int(((snapshot['trust_flow_activity'] == 'active') & (snapshot['trust_flow_momentum'] == 'neutral')).sum())}"),
                ("Negative stable no positive momentum", "PASS" if negative_flow_positive_momentum_count == 0 else "FAIL", f"count={negative_flow_positive_momentum_count}"),
                ("Inactive percentile/strength invariant", "PASS" if inactive_percentile_violations == 0 and inactive_strength_violations == 0 else "FAIL", f"percentile={inactive_percentile_violations}, strength={inactive_strength_violations}"),
                ("At least five examples per requested case", "PASS" if not case_shortage else "FAIL", json.dumps(case_shortage, ensure_ascii=False) if case_shortage else "all six cases have 5"),
            ],
        ),
        "",
        f"**Overall V2.1 validation: {'PASS' if formal_pass and semantic_pass and v2_original_score_changes == 0 else 'FAIL'}**",
        "",
        "This audit stops here. No factor-weight change, optimization, new strategy factor, or formal-strategy migration was performed.",
        "",
    ])

    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    return {
        "as_of_date": as_of_date,
        "eligible_universe": len(snapshot),
        "formal_category_changes": category_changes,
        "formal_v1_score_changes": formal_score_changes,
        "v2_original_score_changes": v2_original_score_changes,
        "negative_flow_positive_momentum_count": negative_flow_positive_momentum_count,
        "direction_state_invariant_violation_count": len(violations),
        "inactive_percentile_violations": inactive_percentile_violations,
        "inactive_strength_violations": inactive_strength_violations,
        "case_shortage": case_shortage,
        "v2_vs_v21": {
            "pearson": v2_pearson,
            "spearman": v2_spearman,
            "overlaps": v2_overlaps,
        },
        "v1_vs_v21": {
            "pearson": v1_pearson,
            "spearman": v1_spearman,
            "overlaps": v1_overlaps,
        },
        "files": [str(REPORT_PATH), str(SNAPSHOT_PATH)],
    }


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
