"""Full-market weekly KD low-passivation golden-cross screener.

This module is deliberately read-only: it derives weekly bars from the existing
``stock_cache.db`` daily bars and never subscribes to quotes or modifies data.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd


DEFAULT_LOOKBACK_WEEKS = 9
DEFAULT_LOW_THRESHOLD = 20.0
DEFAULT_LOW_WEEKS = 3
DEFAULT_MIN_WEEKLY_BARS = 12
DEFAULT_MAX_STALE_DAYS = 7
DEFAULT_VOLUME_LOOKBACK_DAYS = 20
DEFAULT_MIN_AVG_VOLUME_LOTS = 500.0


def is_ky_or_dr_stock_name(name: object) -> bool:
    """Whether a stock name carries a KY or DR suffix."""
    normalized = "".join(str(name or "").upper().split()).replace("－", "-")
    return normalized.endswith("-KY") or normalized.endswith("-DR")


def calculate_weekly_kd(
    weekly_bars: pd.DataFrame,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
) -> pd.DataFrame:
    """Return weekly bars with Taiwan-style RSV/K/D columns.

    RSV uses a rolling ``lookback_weeks`` high/low range. K and D start at 50
    and use the conventional 2/3 previous value + 1/3 current input smoothing.
    """
    required = {"high", "low", "close"}
    missing = required.difference(weekly_bars.columns)
    if missing:
        raise ValueError(f"weekly_bars missing columns: {sorted(missing)}")
    if lookback_weeks < 2:
        raise ValueError("lookback_weeks must be at least 2")

    result = weekly_bars.sort_index().copy()
    lowest_low = result["low"].rolling(lookback_weeks, min_periods=lookback_weeks).min()
    highest_high = result["high"].rolling(lookback_weeks, min_periods=lookback_weeks).max()
    price_range = highest_high - lowest_low
    rsv = ((result["close"] - lowest_low) / price_range * 100.0).clip(0.0, 100.0)
    rsv = rsv.mask(price_range.eq(0), 50.0)

    k_values: list[float] = []
    d_values: list[float] = []
    previous_k = 50.0
    previous_d = 50.0
    for value in rsv:
        if pd.isna(value):
            k_values.append(float("nan"))
            d_values.append(float("nan"))
            continue
        previous_k = previous_k * (2.0 / 3.0) + float(value) / 3.0
        previous_d = previous_d * (2.0 / 3.0) + previous_k / 3.0
        k_values.append(previous_k)
        d_values.append(previous_d)

    result["rsv"] = rsv
    result["k"] = k_values
    result["d"] = d_values
    return result


def consecutive_low_weeks(
    kd_bars: pd.DataFrame,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
) -> int:
    """Count consecutive latest weeks where both K and D are in the low zone."""
    valid = kd_bars.dropna(subset=["k", "d"])
    count = 0
    for _, row in valid.iloc[::-1].iterrows():
        if float(row["k"]) <= low_threshold and float(row["d"]) <= low_threshold:
            count += 1
        else:
            break
    return count


def matches_low_passivation_golden_cross(
    kd_bars: pd.DataFrame,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    low_weeks: int = DEFAULT_LOW_WEEKS,
) -> bool:
    """Whether latest KD is low-passivated and has just crossed upward."""
    if low_weeks < 1:
        raise ValueError("low_weeks must be at least 1")
    valid = kd_bars.dropna(subset=["k", "d"])
    if len(valid) < max(2, low_weeks):
        return False

    latest_low = (
        valid["k"].iloc[-low_weeks:].le(low_threshold)
        & valid["d"].iloc[-low_weeks:].le(low_threshold)
    ).all()
    golden_cross = (
        float(valid["k"].iloc[-2]) <= float(valid["d"].iloc[-2])
        and float(valid["k"].iloc[-1]) > float(valid["d"].iloc[-1])
    )
    return bool(latest_low and golden_cross)


def _prepare_daily_bars(daily_bars: pd.DataFrame) -> pd.DataFrame:
    required = {"code", "date", "open", "high", "low", "close", "volume"}
    missing = required.difference(daily_bars.columns)
    if missing:
        raise ValueError(f"daily_bars missing columns: {sorted(missing)}")

    prepared = daily_bars.copy()
    prepared["code"] = prepared["code"].astype(str)
    prepared["date"] = pd.to_datetime(prepared["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    prepared = prepared.dropna(subset=["date", "open", "high", "low", "close"])
    prepared = prepared[
        prepared["close"].gt(0)
        & prepared["high"].ge(prepared["low"])
        & prepared["high"].ge(prepared["open"])
        & prepared["high"].ge(prepared["close"])
        & prepared["low"].le(prepared["open"])
        & prepared["low"].le(prepared["close"])
    ]
    prepared["volume"] = prepared["volume"].fillna(0).clip(lower=0)
    return prepared.sort_values(["code", "date"])


def calculate_average_volume_lots(
    daily_bars: pd.DataFrame,
    *,
    session_dates: pd.Index | list | None = None,
    lookback_days: int = DEFAULT_VOLUME_LOOKBACK_DAYS,
) -> dict:
    """Calculate average daily volume in lots over recent market sessions.

    Missing stock rows on a supplied market-session date count as zero lots.
    This prevents thinly traded stocks from looking more active merely because
    their no-trade dates are absent from the local table.
    """
    if lookback_days < 1:
        raise ValueError("lookback_days must be at least 1")
    required = {"date", "volume"}
    missing = required.difference(daily_bars.columns)
    if missing:
        raise ValueError(f"daily_bars missing volume columns: {sorted(missing)}")

    stock = daily_bars.loc[:, ["date", "volume"]].copy()
    stock["date"] = pd.to_datetime(stock["date"], errors="coerce").dt.normalize()
    stock["volume"] = pd.to_numeric(stock["volume"], errors="coerce")
    stock = stock.dropna(subset=["date", "volume"])
    stock = stock[stock["volume"].ge(0)]
    volume_by_date = stock.groupby("date", sort=True)["volume"].sum()

    if session_dates is None:
        recent_dates = pd.DatetimeIndex(volume_by_date.index).sort_values().unique()
    else:
        recent_dates = pd.DatetimeIndex(pd.to_datetime(session_dates, errors="coerce"))
        recent_dates = recent_dates.dropna().normalize().sort_values().unique()
    recent_dates = recent_dates[-lookback_days:]

    window_complete = len(recent_dates) == lookback_days
    if len(recent_dates):
        recent_volume = volume_by_date.reindex(recent_dates, fill_value=0.0)
        average_volume = float(recent_volume.mean())
    else:
        average_volume = 0.0
    return {
        "avg_volume_20d": round(average_volume, 2),
        "volume_window_days": int(len(recent_dates)),
        "volume_window_complete": bool(window_complete),
    }


def passes_average_volume_filter(
    volume_metrics: dict,
    min_avg_volume_lots: float = DEFAULT_MIN_AVG_VOLUME_LOTS,
) -> bool:
    """Return True only when a complete window is strictly above the limit."""
    if min_avg_volume_lots < 0:
        raise ValueError("min_avg_volume_lots cannot be negative")
    return bool(
        volume_metrics.get("volume_window_complete")
        and float(volume_metrics.get("avg_volume_20d") or 0) > min_avg_volume_lots
    )


def screen_daily_bars(
    daily_bars: pd.DataFrame,
    *,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    low_weeks: int = DEFAULT_LOW_WEEKS,
    min_weekly_bars: int = DEFAULT_MIN_WEEKLY_BARS,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    min_avg_volume_20d_lots: float | None = None,
) -> dict:
    """Screen an already-loaded ordinary-stock daily-bar universe."""
    if min_weekly_bars < lookback_weeks + low_weeks:
        raise ValueError("min_weekly_bars must cover KD lookback plus low weeks")
    if not 0 < low_threshold < 100:
        raise ValueError("low_threshold must be between 0 and 100")
    if min_avg_volume_20d_lots is not None and min_avg_volume_20d_lots < 0:
        raise ValueError("min_avg_volume_20d_lots cannot be negative")

    excluded_ky_dr_codes: set[str] = set()
    if not daily_bars.empty and "name" in daily_bars.columns:
        excluded_ky_dr_codes = set(
            daily_bars.loc[
                daily_bars["name"].map(is_ky_or_dr_stock_name),
                "code",
            ].astype(str)
        )
        if excluded_ky_dr_codes:
            daily_bars = daily_bars[
                ~daily_bars["code"].astype(str).isin(excluded_ky_dr_codes)
            ].copy()

    excluded_ky_dr_count = len(excluded_ky_dr_codes)
    universe_count = int(daily_bars["code"].astype(str).nunique()) if not daily_bars.empty else 0
    prepared = _prepare_daily_bars(daily_bars)
    if prepared.empty:
        return {
            "as_of_date": "",
            "universe_count": universe_count,
            "excluded_ky_dr_count": excluded_ky_dr_count,
            "analyzed_count": 0,
            "insufficient_count": universe_count,
            "stale_count": 0,
            "technical_matched_count": 0,
            "volume_filter_excluded_count": 0,
            "matched_count": 0,
            "results": [],
        }

    as_of = prepared["date"].max()
    metadata = prepared.groupby("code", sort=False).agg(
        name=("name", "last") if "name" in prepared.columns else ("code", "last"),
        category=("category", "last") if "category" in prepared.columns else ("code", "last"),
        stock_latest_date=("date", "max"),
        daily_bar_count=("date", "count"),
    )

    prepared["week_end"] = prepared["date"].dt.to_period("W-FRI").dt.end_time.dt.normalize()
    market_session_dates = pd.DatetimeIndex(
        prepared["date"].drop_duplicates().sort_values().tail(DEFAULT_VOLUME_LOOKBACK_DAYS)
    )
    weekly = prepared.groupby(["code", "week_end"], sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        last_trade_date=("date", "max"),
    )

    analyzed_count = 0
    insufficient_count = 0
    stale_count = 0
    technical_matched_count = 0
    volume_filter_excluded_count = 0
    matches: list[dict] = []

    for code, stock_meta in metadata.iterrows():
        latest_date = pd.Timestamp(stock_meta["stock_latest_date"])
        if (as_of - latest_date).days > max_stale_days:
            stale_count += 1
            continue

        try:
            stock_weekly = weekly.loc[code].copy()
        except KeyError:
            insufficient_count += 1
            continue
        if len(stock_weekly) < min_weekly_bars:
            insufficient_count += 1
            continue

        kd_bars = calculate_weekly_kd(stock_weekly, lookback_weeks=lookback_weeks)
        valid_kd = kd_bars.dropna(subset=["k", "d"])
        if len(valid_kd) < max(2, low_weeks):
            insufficient_count += 1
            continue

        analyzed_count += 1
        if not matches_low_passivation_golden_cross(
            valid_kd,
            low_threshold=low_threshold,
            low_weeks=low_weeks,
        ):
            continue

        technical_matched_count += 1
        volume_metrics = calculate_average_volume_lots(
            prepared.loc[prepared["code"].eq(str(code))],
            session_dates=market_session_dates,
        )
        if (
            min_avg_volume_20d_lots is not None
            and not passes_average_volume_filter(volume_metrics, min_avg_volume_20d_lots)
        ):
            volume_filter_excluded_count += 1
            continue

        latest = valid_kd.iloc[-1]
        previous = valid_kd.iloc[-2]
        close_price = float(latest["close"])
        previous_close = float(previous["close"])
        weekly_change_pct = (
            (close_price / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0
        )
        k_value = float(latest["k"])
        d_value = float(latest["d"])
        matches.append({
            "code": str(code),
            "name": str(stock_meta["name"] or code),
            "category": str(stock_meta["category"] or "未分類"),
            "data_date": latest_date.strftime("%Y-%m-%d"),
            "week_end": pd.Timestamp(valid_kd.index[-1]).strftime("%Y-%m-%d"),
            "close": round(close_price, 2),
            "weekly_change_pct": round(weekly_change_pct, 2),
            "k": round(k_value, 2),
            "d": round(d_value, 2),
            "previous_k": round(float(previous["k"]), 2),
            "previous_d": round(float(previous["d"]), 2),
            "cross_strength": round(k_value - d_value, 2),
            "low_weeks": consecutive_low_weeks(valid_kd, low_threshold=low_threshold),
            "weekly_bar_count": int(len(stock_weekly)),
            "daily_bar_count": int(stock_meta["daily_bar_count"]),
            **volume_metrics,
        })

    matches.sort(
        key=lambda item: (item["cross_strength"], item["low_weeks"], item["code"]),
        reverse=True,
    )
    for rank, item in enumerate(matches, start=1):
        item["rank"] = rank

    return {
        "as_of_date": pd.Timestamp(as_of).strftime("%Y-%m-%d"),
        "universe_count": universe_count,
        "excluded_ky_dr_count": excluded_ky_dr_count,
        "analyzed_count": analyzed_count,
        "insufficient_count": insufficient_count,
        "stale_count": stale_count,
        "technical_matched_count": technical_matched_count,
        "volume_filter_excluded_count": volume_filter_excluded_count,
        "matched_count": len(matches),
        "results": matches,
    }


def scan_weekly_kd(
    db_path: str | Path,
    *,
    lookback_weeks: int = DEFAULT_LOOKBACK_WEEKS,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    low_weeks: int = DEFAULT_LOW_WEEKS,
    min_avg_volume_20d_lots: float | None = None,
) -> dict:
    """Read all 4-digit ordinary stocks from SQLite and run the weekly KD scan."""
    started_at = time.perf_counter()
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"stock database not found: {path}")

    conn = sqlite3.connect(str(path), timeout=30.0)
    try:
        conn.execute("PRAGMA query_only=ON")
        daily_bars = pd.read_sql_query(
            """
            SELECT k.code, k.date, k.open, k.high, k.low, k.close, k.volume,
                   COALESCE(NULLIF(n.name, ''), k.code) AS name,
                   COALESCE(NULLIF(n.category, ''), '未分類') AS category
            FROM daily_kbars AS k
            LEFT JOIN stock_names AS n ON n.code = k.code
            WHERE length(k.code) = 4
              AND k.code GLOB '[1-9][0-9][0-9][0-9]'
            ORDER BY k.code, k.date
            """,
            conn,
        )
    finally:
        conn.close()

    payload = screen_daily_bars(
        daily_bars,
        lookback_weeks=lookback_weeks,
        low_threshold=low_threshold,
        low_weeks=low_weeks,
        min_avg_volume_20d_lots=min_avg_volume_20d_lots,
    )
    payload.update({
        "criteria": {
            "timeframe": "weekly",
            "rsv_lookback_weeks": lookback_weeks,
            "low_threshold": low_threshold,
            "low_weeks": low_weeks,
            "cross": "K upward crosses D on latest weekly bar",
            "exclude_ky_dr": True,
            "average_volume_filter": {
                "enabled": min_avg_volume_20d_lots is not None,
                "lookback_days": DEFAULT_VOLUME_LOOKBACK_DAYS,
                "operator": ">",
                "threshold_lots": min_avg_volume_20d_lots,
                "volume_unit": "lot",
            },
        },
        "scope": "台灣上市櫃 4 碼普通股（不含 ETF、ETN、權證、KY、DR）",
        "data_source": "stock_cache.db / daily_kbars",
        "elapsed_ms": int(round((time.perf_counter() - started_at) * 1000)),
    })
    return payload
