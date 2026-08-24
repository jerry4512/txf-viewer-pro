"""Capital Flow V2 Milestone 1 shadow calculations.

This module is deliberately isolated from the formal A/B1/B2/C, scoring,
classification, and sorting paths.  It may add observability fields, but no
formal selector is allowed to read these values while ``shadow_mode`` is true.
All inputs are point-in-time filtered by ``as_of_date``.
"""

from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from stock_selection_schema import (
    classification_from_master,
    ensure_stock_selection_schema,
)


SHADOW_MODE = True

# Research-only heuristics.  They are centralized and intentionally do not
# affect formal selection.  No value below was optimized against outcomes.
CAPITAL_FLOW_V2_CONFIG = {
    "flow_intensity_full_score_ratio": 0.05,  # 5% of same-period volume
    "factor_caps": {
        "identity": 10.0,
        "intensity": 25.0,
        "persistence": 10.0,
        "momentum": 10.0,
        "price_confirmation": 25.0,
        "cross_sectional": 20.0,
    },
    "momentum_quality": {
        "inactive": 0.00,
        "accelerating": 1.00,
        "stable": 0.70,
        "decelerating": 0.35,
        "reversing_positive": 0.60,
        "reversing_negative": 0.00,
        "neutral": 0.20,
    },
}


def empty_capital_flow_shadow(reason: str = "not_eligible") -> dict[str, Any]:
    return {
        "shadow_mode": True,
        "capital_flow_v2_available": False,
        "capital_flow_v2_unavailable_reason": reason,
        "flow_price_quadrant": None,
        "flow_price_state": None,
        "capital_flow_score_v2_shadow": None,
        "capital_flow_score_v21_shadow": None,
    }


def _return_pct(close: pd.Series, periods: int) -> Optional[float]:
    values = pd.to_numeric(close, errors="coerce").dropna()
    if len(values) <= periods:
        return None
    previous = float(values.iloc[-periods - 1])
    latest = float(values.iloc[-1])
    if previous <= 0:
        return None
    return (latest / previous - 1.0) * 100.0


def _safe_ratio(numerator: float, denominator: float) -> Optional[float]:
    if denominator <= 0:
        return None
    return float(numerator) / float(denominator)


def _consecutive_positive(values: list[float]) -> int:
    count = 0
    for value in reversed(values):
        if value > 0:
            count += 1
        else:
            break
    return count


def classify_flow_momentum(
    flow_1d: float,
    flow_3d: float,
    flow_5d: float,
    flow_last_2d: float,
    ratio_1d: Optional[float],
    ratio_3d: Optional[float],
    active_days_5: Optional[int] = None,
) -> str:
    """Sign/relative-strength state machine without fitted thresholds."""
    # Activity is a separate concept from net direction.  A five-day net zero
    # can still be active when positive and negative daily flows offset.
    if active_days_5 == 0 or (
        active_days_5 is None
        and flow_1d == 0
        and flow_3d == 0
        and flow_5d == 0
        and flow_last_2d == 0
    ):
        return "inactive"
    if flow_5d > 0 and flow_last_2d < 0:
        return "reversing_negative"
    if flow_5d < 0 and flow_last_2d > 0:
        return "reversing_positive"
    if (
        flow_1d > 0
        and flow_3d > 0
        and ratio_1d is not None
        and ratio_3d is not None
        and ratio_1d > ratio_3d
    ):
        return "accelerating"
    if (
        flow_5d > 0
        and flow_3d > 0
        and ratio_1d is not None
        and ratio_3d is not None
        and ratio_1d < ratio_3d
    ):
        return "decelerating"
    if (flow_1d > 0 and flow_3d > 0 and flow_5d > 0) or (
        flow_1d < 0 and flow_3d < 0 and flow_5d < 0
    ):
        return "stable"
    return "neutral"


def classify_flow_price_quadrant(
    combined_flow_5d: float,
    return_5d: Optional[float],
    rs20: Optional[float],
) -> tuple[Optional[str], Optional[str]]:
    """Classify Flow × Price using positive return and market-relative RS."""
    if return_5d is None or rs20 is None or combined_flow_5d == 0:
        return None, "neutral"
    flow_positive = combined_flow_5d > 0
    price_confirmed = return_5d > 0 and rs20 > 0
    if flow_positive and price_confirmed:
        return "Q1", "confirmed_accumulation"
    if flow_positive and not price_confirmed:
        return "Q2", "unconfirmed_accumulation"
    if not flow_positive and price_confirmed:
        return "Q3", "absorption_divergence"
    return "Q4", "confirmed_distribution"


def _window_values(
    frame: pd.DataFrame,
    column: str,
    window_dates: list[str],
) -> list[float]:
    if not window_dates:
        return []
    indexed = frame.set_index("date") if not frame.empty else pd.DataFrame()
    if indexed.empty or column not in indexed.columns:
        return [0.0 for _ in window_dates]
    return [
        float(indexed.at[date_value, column])
        if date_value in indexed.index and pd.notna(indexed.at[date_value, column])
        else 0.0
        for date_value in window_dates
    ]


def _actor_metrics(
    prefix: str,
    flow_frame: pd.DataFrame,
    flow_column: str,
    bar_frame: pd.DataFrame,
    institutional_dates: list[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    bar_by_date = bar_frame.set_index("date")
    for window in (1, 3, 5, 10):
        dates = institutional_dates[-window:]
        flows = _window_values(flow_frame, flow_column, dates)
        flow_shares = float(sum(flows))
        volume_shares = sum(
            float(bar_by_date.at[date_value, "volume"]) * 1000.0
            for date_value in dates
            if date_value in bar_by_date.index
            and pd.notna(bar_by_date.at[date_value, "volume"])
        )
        result[f"{prefix}_flow_{window}d_shares"] = int(round(flow_shares))
        result[f"{prefix}_flow_{window}d"] = flow_shares / 1000.0
        result[f"{prefix}_flow_ratio_{window}d"] = _safe_ratio(
            flow_shares, volume_shares
        )

    for window in (5, 10):
        dates = institutional_dates[-window:]
        flows = _window_values(flow_frame, flow_column, dates)
        result[f"{prefix}_positive_days_{window}"] = sum(
            1 for value in flows if value > 0
        )

    dates10 = institutional_dates[-10:]
    values10 = _window_values(flow_frame, flow_column, dates10)
    result[f"{prefix}_consecutive_buy"] = _consecutive_positive(values10)

    dates5 = institutional_dates[-5:]
    values5 = _window_values(flow_frame, flow_column, dates5)
    active_days_5 = sum(1 for value in values5 if value != 0)
    result[f"{prefix}_active_days_5"] = active_days_5
    result[f"{prefix}_flow_activity"] = (
        "active" if active_days_5 > 0 else "inactive"
    )
    numerator = 0.0
    denominator = 0.0
    for date_value, flow_shares in zip(dates5, values5):
        if date_value not in bar_by_date.index:
            continue
        close = float(bar_by_date.at[date_value, "close"])
        volume_lots = float(bar_by_date.at[date_value, "volume"])
        # Approximation required by V2.1: institutional net shares × close.
        numerator += float(flow_shares) * close
        denominator += volume_lots * 1000.0 * close
    result[f"{prefix}_amount_ratio_5d"] = _safe_ratio(numerator, denominator)

    ratio1 = result.get(f"{prefix}_flow_ratio_1d")
    ratio3 = result.get(f"{prefix}_flow_ratio_3d")
    result[f"{prefix}_flow_momentum"] = classify_flow_momentum(
        result[f"{prefix}_flow_1d_shares"],
        result[f"{prefix}_flow_3d_shares"],
        result[f"{prefix}_flow_5d_shares"],
        sum(values10[-2:]),
        ratio1,
        ratio3,
        active_days_5,
    )
    flow_5d = float(result[f"{prefix}_flow_5d_shares"])
    result[f"{prefix}_flow_direction"] = (
        "positive" if flow_5d > 0 else "negative" if flow_5d < 0 else "zero"
    )
    return result


def _percentile_map(values: dict[str, Optional[float]]) -> dict[str, Optional[float]]:
    valid = {code: value for code, value in values.items() if value is not None}
    if not valid:
        return {code: None for code in values}
    series = pd.Series(valid, dtype="float64")
    ranked = series.rank(method="average", pct=True) * 100.0
    return {
        code: (round(float(ranked[code]), 2) if code in ranked else None)
        for code in values
    }


def _base_score_inputs(metrics: dict[str, Any]) -> dict[str, float]:
    """Calculate V2 components that are unchanged in V2.1."""
    caps = CAPITAL_FLOW_V2_CONFIG["factor_caps"]
    f5 = float(metrics.get("foreign_flow_5d_shares") or 0)
    t5 = float(metrics.get("trust_flow_5d_shares") or 0)
    dealer_prop5 = float(metrics.get("dealer_prop_flow_5d_shares") or 0)
    multi = f5 > 0 and t5 > 0

    identity = 0.0
    if f5 > 0:
        identity += 3.5
    if t5 > 0:
        identity += 3.5
    if multi:
        identity += 2.0
    if dealer_prop5 > 0 and metrics.get("dealer_flow_detail_level") == "split":
        identity += 1.0

    full_ratio = float(CAPITAL_FLOW_V2_CONFIG["flow_intensity_full_score_ratio"])
    intensity_parts = []
    for prefix in ("foreign", "trust"):
        ratio = max(0.0, float(metrics.get(f"{prefix}_flow_ratio_5d") or 0.0))
        intensity_parts.append(min(1.0, ratio / full_ratio))
    intensity = caps["intensity"] * sum(intensity_parts) / len(intensity_parts)

    persistence_parts = [
        min(1.0, max(0.0, float(metrics.get("foreign_positive_days_10") or 0) / 10.0)),
        min(1.0, max(0.0, float(metrics.get("trust_positive_days_10") or 0) / 10.0)),
    ]
    persistence = caps["persistence"] * sum(persistence_parts) / 2.0

    quadrant = metrics.get("flow_price_quadrant")
    price_confirmation = {
        "Q1": caps["price_confirmation"],
        "Q2": caps["price_confirmation"] * 0.20,
        # Q3 is observation-only in Milestone 1, not a directional bonus.
        "Q3": 0.0,
        "Q4": 0.0,
    }.get(quadrant, 0.0)

    return {
        "flow_identity_score": min(caps["identity"], identity),
        "flow_intensity_score": min(caps["intensity"], intensity),
        "flow_persistence_score": min(caps["persistence"], persistence),
        "flow_price_confirmation_score": min(
            caps["price_confirmation"], price_confirmation
        ),
    }


def _legacy_momentum_component(metrics: dict[str, Any]) -> float:
    """Reproduce the original V2 momentum component for comparison only."""
    caps = CAPITAL_FLOW_V2_CONFIG["factor_caps"]
    quality = CAPITAL_FLOW_V2_CONFIG["momentum_quality"]
    states = []
    for prefix in ("foreign", "trust"):
        state = metrics.get(f"{prefix}_flow_momentum")
        # Before V2.1, no activity was represented as neutral.
        states.append("neutral" if state == "inactive" else state)
    return caps["momentum"] * sum(
        float(quality.get(state, 0.0)) for state in states
    ) / 2.0


def _directional_momentum_component(
    direction: str,
    momentum_state: str,
    actor_cap: float,
) -> float:
    """Return a signed actor component while retaining existing magnitudes."""
    quality = CAPITAL_FLOW_V2_CONFIG["momentum_quality"]
    if momentum_state == "inactive":
        return 0.0
    if momentum_state == "reversing_positive":
        sign = 1.0
    elif momentum_state == "reversing_negative":
        sign = -1.0
    elif direction == "positive":
        sign = 1.0
    elif direction == "negative":
        sign = -1.0
    else:
        sign = 0.0
    component = actor_cap * sign * float(quality.get(momentum_state, 0.0))
    return 0.0 if component == 0 else component


def _relative_component(
    metrics: dict[str, Any], percentile_suffix: str
) -> float:
    caps = CAPITAL_FLOW_V2_CONFIG["factor_caps"]
    parts = []
    for prefix in ("foreign", "trust"):
        direction = metrics.get(f"{prefix}_flow_direction")
        if direction is None:
            flow = float(metrics.get(f"{prefix}_flow_5d_shares") or 0.0)
            direction = "positive" if flow > 0 else "negative" if flow < 0 else "zero"
        percentile = metrics.get(f"{prefix}_{percentile_suffix}")
        parts.append(
            max(0.0, float(percentile or 0.0) / 100.0)
            if direction == "positive"
            else 0.0
        )
    return caps["cross_sectional"] * sum(parts) / 2.0


def score_capital_flow_v2_original(metrics: dict[str, Any]) -> dict[str, float]:
    """Preserve the pre-fix V2 score as a stable research baseline."""
    caps = CAPITAL_FLOW_V2_CONFIG["factor_caps"]
    scores = _base_score_inputs(metrics)
    scores["flow_momentum_score"] = min(
        caps["momentum"], _legacy_momentum_component(metrics)
    )
    scores["flow_relative_score"] = min(
        caps["cross_sectional"],
        _relative_component(metrics, "flow_intensity_percentile"),
    )
    return {key: round(float(value), 2) for key, value in scores.items()}


def score_capital_flow_shadow(metrics: dict[str, Any]) -> dict[str, float]:
    """Calculate V2.1 score components without touching formal V1 scoring."""
    caps = CAPITAL_FLOW_V2_CONFIG["factor_caps"]
    actor_cap = caps["momentum"] / 2.0
    actor_components = {}
    for prefix in ("foreign", "trust"):
        direction = metrics.get(f"{prefix}_flow_direction")
        if direction is None:
            flow = float(metrics.get(f"{prefix}_flow_5d_shares") or 0.0)
            direction = "positive" if flow > 0 else "negative" if flow < 0 else "zero"
        actor_components[prefix] = _directional_momentum_component(
            str(direction),
            str(metrics.get(f"{prefix}_flow_momentum") or "inactive"),
            actor_cap,
        )

    scores = _base_score_inputs(metrics)
    scores["flow_momentum_score"] = sum(actor_components.values())
    scores["flow_relative_score"] = min(
        caps["cross_sectional"],
        _relative_component(metrics, "flow_active_percentile_v21"),
    )
    return {key: round(float(value), 2) for key, value in scores.items()}


def compute_capital_flow_v2_shadow(conn, as_of_date: str) -> dict[str, Any]:
    """Compute point-in-time V2 shadow fields for the full eligible universe."""
    as_of_date = str(as_of_date or "")[:10]
    ensure_stock_selection_schema(conn)
    if not as_of_date:
        return {
            "shadow_mode": True,
            "as_of_date": as_of_date,
            "metrics_by_code": {},
            "universe_size": 0,
            "errors": ["as_of_date missing"],
        }

    bars = pd.read_sql_query(
        "SELECT code,date,open,high,low,close,volume FROM daily_kbars "
        "WHERE date <= ? ORDER BY code,date", conn, params=[as_of_date]
    )
    market = pd.read_sql_query(
        "SELECT date,close FROM market_index_daily WHERE date <= ? ORDER BY date",
        conn, params=[as_of_date],
    )
    names = {
        str(row[0]): {"name": str(row[1] or row[0]), "industry": str(row[2] or "")}
        for row in conn.execute(
            "SELECT code,name,COALESCE(category,'') FROM stock_names"
        ).fetchall()
    }
    master_rows = conn.execute("SELECT * FROM security_master").fetchall()
    master_columns = [description[0] for description in conn.execute(
        "SELECT * FROM security_master LIMIT 0"
    ).description]
    master = {
        str(row[0]): dict(zip(master_columns, row)) for row in master_rows
    }

    if bars.empty or market.empty or str(market.iloc[-1]["date"]) != as_of_date:
        return {
            "shadow_mode": True,
            "as_of_date": as_of_date,
            "metrics_by_code": {},
            "universe_size": 0,
            "errors": ["daily_kbars or market_index_daily not aligned to as_of_date"],
        }

    taiex_return20 = _return_pct(market["close"], 20)
    taiex_return60 = _return_pct(market["close"], 60)
    taiex_return5 = _return_pct(market["close"], 5)
    if taiex_return20 is None or taiex_return60 is None:
        return {
            "shadow_mode": True,
            "as_of_date": as_of_date,
            "metrics_by_code": {},
            "universe_size": 0,
            "errors": ["market history shorter than 61 bars"],
        }

    eligible_bars: dict[str, pd.DataFrame] = {}
    for code, group in bars.groupby("code"):
        code_str = str(code)
        identity = names.get(code_str, {"name": code_str, "industry": ""})
        instrument_type, _ = classification_from_master(
            master.get(code_str), code_str, identity["name"], identity["industry"]
        )
        frame = group.sort_values("date").reset_index(drop=True)
        if instrument_type != "common_stock":
            continue
        if frame.empty or str(frame.iloc[-1]["date"]) != as_of_date:
            continue
        if len(frame) < 61:
            continue
        eligible_bars[code_str] = frame

    institutional_dates = [
        str(row[0]) for row in conn.execute(
            "SELECT DISTINCT date FROM institutional_trading "
            "WHERE date <= ? ORDER BY date DESC LIMIT 10", (as_of_date,)
        ).fetchall()
    ][::-1]
    flows = pd.read_sql_query(
        """
        SELECT code,date,
               COALESCE(foreign_net,foreign_buy_shares,foreign_buy*1000,0) AS foreign_net,
               COALESCE(trust_net,investment_buy_shares,investment_buy*1000,0) AS trust_net,
               dealer_prop_net,dealer_hedge_net,dealer_unknown_net,
               COALESCE(flow_detail_level,'legacy_combined') AS flow_detail_level
        FROM institutional_trading
        WHERE date <= ?
        ORDER BY code,date
        """,
        conn, params=[as_of_date],
    )
    flow_groups = {
        str(code): group.tail(10).copy()
        for code, group in flows.groupby("code")
        if str(code) in eligible_bars
    } if not flows.empty else {}

    metrics_by_code: dict[str, dict[str, Any]] = {}
    for code, frame in eligible_bars.items():
        flow_frame = flow_groups.get(code, pd.DataFrame(columns=[
            "date", "foreign_net", "trust_net", "dealer_prop_net",
            "dealer_hedge_net", "dealer_unknown_net", "flow_detail_level",
        ]))
        metric: dict[str, Any] = {
            "shadow_mode": True,
            "capital_flow_v2_available": bool(institutional_dates),
            "capital_flow_v2_unavailable_reason": None if institutional_dates else "institutional_dates_missing",
            "return_1d": _return_pct(frame["close"], 1),
            "return_3d": _return_pct(frame["close"], 3),
            "return_5d": _return_pct(frame["close"], 5),
            "stock_return5": _return_pct(frame["close"], 5),
            "stock_return20": _return_pct(frame["close"], 20),
            "stock_return60": _return_pct(frame["close"], 60),
            "taiex_return5": taiex_return5,
            "taiex_return20": taiex_return20,
            "taiex_return60": taiex_return60,
        }
        metric["rs5"] = (
            metric["stock_return5"] - taiex_return5
            if metric["stock_return5"] is not None and taiex_return5 is not None
            else None
        )
        metric["rs20"] = (
            metric["stock_return20"] - taiex_return20
            if metric["stock_return20"] is not None else None
        )
        metric["rs60"] = (
            metric["stock_return60"] - taiex_return60
            if metric["stock_return60"] is not None else None
        )
        # Both spellings are exposed because the V2.1 specification uses
        # rs_5d/rs_20d in Price Response and rs20/rs60 in Relative Strength.
        metric["rs_5d"] = metric["rs5"]
        metric["rs_20d"] = metric["rs20"]
        metric.update(_actor_metrics(
            "foreign", flow_frame, "foreign_net", frame, institutional_dates
        ))
        metric.update(_actor_metrics(
            "trust", flow_frame, "trust_net", frame, institutional_dates
        ))

        for dealer_prefix, column in (
            ("dealer_prop", "dealer_prop_net"),
            ("dealer_hedge", "dealer_hedge_net"),
            ("dealer_unknown", "dealer_unknown_net"),
        ):
            values = _window_values(flow_frame, column, institutional_dates[-5:])
            metric[f"{dealer_prefix}_flow_5d_shares"] = int(round(sum(values)))
            metric[f"{dealer_prefix}_flow_5d"] = sum(values) / 1000.0

        detail_values = set(str(value) for value in flow_frame.get(
            "flow_detail_level", pd.Series(dtype=str)
        ).dropna().tolist())
        metric["dealer_flow_detail_level"] = (
            "split" if detail_values == {"split"}
            else "mixed" if "split" in detail_values
            else "legacy_combined"
        )
        metric["multi_flow_confirmation"] = (
            (metric.get("foreign_flow_ratio_5d") or 0) > 0
            and (metric.get("trust_flow_ratio_5d") or 0) > 0
        )
        combined_flow_5d = (
            float(metric.get("foreign_flow_5d_shares") or 0)
            + float(metric.get("trust_flow_5d_shares") or 0)
        )
        quadrant, state = classify_flow_price_quadrant(
            combined_flow_5d, metric["return_5d"], metric["rs20"]
        )
        metric["flow_price_quadrant"] = quadrant
        metric["flow_price_state"] = state
        metrics_by_code[code] = metric

    rs20_percentiles = _percentile_map({
        code: metric.get("rs20") for code, metric in metrics_by_code.items()
    })
    rs60_percentiles = _percentile_map({
        code: metric.get("rs60") for code, metric in metrics_by_code.items()
    })
    foreign_percentiles = _percentile_map({
        code: metric.get("foreign_flow_ratio_5d")
        for code, metric in metrics_by_code.items()
    })
    trust_percentiles = _percentile_map({
        code: metric.get("trust_flow_ratio_5d")
        for code, metric in metrics_by_code.items()
    })
    foreign_active_percentiles = _percentile_map({
        code: (
            abs(float(metric.get("foreign_flow_ratio_5d") or 0.0))
            if metric.get("foreign_flow_activity") == "active"
            else None
        )
        for code, metric in metrics_by_code.items()
    })
    trust_active_percentiles = _percentile_map({
        code: (
            abs(float(metric.get("trust_flow_ratio_5d") or 0.0))
            if metric.get("trust_flow_activity") == "active"
            else None
        )
        for code, metric in metrics_by_code.items()
    })

    for code, metric in metrics_by_code.items():
        metric["rs20_percentile"] = rs20_percentiles.get(code)
        metric["rs60_percentile"] = rs60_percentiles.get(code)
        metric["foreign_flow_intensity_percentile"] = foreign_percentiles.get(code)
        metric["trust_flow_intensity_percentile"] = trust_percentiles.get(code)
        metric["foreign_flow_percentile"] = foreign_percentiles.get(code)
        metric["trust_flow_percentile"] = trust_percentiles.get(code)
        metric["foreign_flow_percentile_v2_original"] = foreign_percentiles.get(code)
        metric["trust_flow_percentile_v2_original"] = trust_percentiles.get(code)
        metric["foreign_flow_intensity_active_percentile"] = (
            foreign_active_percentiles.get(code)
        )
        metric["trust_flow_intensity_active_percentile"] = (
            trust_active_percentiles.get(code)
        )
        metric["foreign_flow_active_percentile_v21"] = (
            foreign_active_percentiles.get(code)
        )
        metric["trust_flow_active_percentile_v21"] = (
            trust_active_percentiles.get(code)
        )
        for prefix in ("foreign", "trust"):
            percentile = metric.get(f"{prefix}_flow_active_percentile_v21")
            direction = metric.get(f"{prefix}_flow_direction")
            metric[f"{prefix}_signed_flow_strength"] = (
                float(percentile or 0.0)
                if direction == "positive"
                else -float(percentile or 0.0)
                if direction == "negative"
                else 0.0
            )
            metric[f"{prefix}_momentum_component"] = round(
                _directional_momentum_component(
                    str(direction),
                    str(metric.get(f"{prefix}_flow_momentum") or "inactive"),
                    float(CAPITAL_FLOW_V2_CONFIG["factor_caps"]["momentum"]) / 2.0,
                ),
                2,
            )

        original_score_parts = score_capital_flow_v2_original(metric)
        score_parts = score_capital_flow_shadow(metric)
        metric.update(score_parts)
        metric["flow_momentum_score_v2_original"] = original_score_parts[
            "flow_momentum_score"
        ]
        metric["flow_relative_score_v2_original"] = original_score_parts[
            "flow_relative_score"
        ]
        metric["flow_momentum_score_v21"] = score_parts["flow_momentum_score"]
        metric["flow_relative_score_v21"] = score_parts["flow_relative_score"]
        metric["capital_flow_score_v2_shadow"] = round(
            sum(original_score_parts.values()), 2
        )
        metric["capital_flow_score_v21_shadow"] = round(
            max(0.0, min(100.0, sum(score_parts.values()))), 2
        )

    return {
        "shadow_mode": True,
        "as_of_date": as_of_date,
        "metrics_by_code": metrics_by_code,
        "universe_size": len(metrics_by_code),
        "institutional_dates": institutional_dates,
        "taiex_return5": taiex_return5,
        "taiex_return20": taiex_return20,
        "taiex_return60": taiex_return60,
        "config": CAPITAL_FLOW_V2_CONFIG,
        "errors": [],
    }
