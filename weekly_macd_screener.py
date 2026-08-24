"""Weekly MACD bullish-divergence screener for Taiwan ordinary stocks."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pandas as pd

from weekly_kd_screener import (
    DEFAULT_VOLUME_LOOKBACK_DAYS,
    _prepare_daily_bars,
    calculate_average_volume_lots,
    is_ky_or_dr_stock_name,
    passes_average_volume_filter,
)


DEFAULT_FAST_PERIOD = 12
DEFAULT_SLOW_PERIOD = 26
DEFAULT_SIGNAL_PERIOD = 9
DEFAULT_MIN_WEEKLY_BARS = 16
DEFAULT_RECENT_TROUGH_WEEKS = 3
DEFAULT_MIN_LOWER_LOW_PCT = 1.0
DEFAULT_MAX_STALE_DAYS = 7
MATURE_MACD_WEEKLY_BARS = 35


def calculate_weekly_macd(
    weekly_bars: pd.DataFrame,
    fast_period: int = DEFAULT_FAST_PERIOD,
    slow_period: int = DEFAULT_SLOW_PERIOD,
    signal_period: int = DEFAULT_SIGNAL_PERIOD,
) -> pd.DataFrame:
    """Return weekly DIF, signal and Taiwan-style OSC (DIF-signal) * 2."""
    if "close" not in weekly_bars.columns:
        raise ValueError("weekly_bars missing column: close")
    if not 1 < fast_period < slow_period or signal_period < 2:
        raise ValueError("invalid MACD periods")

    result = weekly_bars.sort_index().copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    fast_ema = close.ewm(span=fast_period, adjust=False).mean()
    slow_ema = close.ewm(span=slow_period, adjust=False).mean()
    dif = fast_ema - slow_ema
    signal = dif.ewm(span=signal_period, adjust=False).mean()
    result["dif"] = dif
    result["signal"] = signal
    result["osc"] = (dif - signal) * 2.0
    return result


def find_bullish_macd_divergence(
    macd_bars: pd.DataFrame,
    *,
    recent_trough_weeks: int = DEFAULT_RECENT_TROUGH_WEEKS,
    min_lower_low_pct: float = DEFAULT_MIN_LOWER_LOW_PCT,
) -> dict | None:
    """Find a confirmed weekly price lower-low with a higher negative OSC low."""
    required = {"low", "osc"}
    missing = required.difference(macd_bars.columns)
    if missing:
        raise ValueError(f"macd_bars missing columns: {sorted(missing)}")
    if recent_trough_weeks < 2:
        raise ValueError("recent_trough_weeks must be at least 2")
    if min_lower_low_pct < 0:
        raise ValueError("min_lower_low_pct cannot be negative")

    valid = macd_bars.dropna(subset=["low", "osc"])
    if len(valid) < 5:
        return None

    trough_positions: list[int] = []
    for position in range(1, len(valid) - 1):
        current_low = float(valid["low"].iloc[position])
        is_confirmed_low = (
            current_low <= float(valid["low"].iloc[position - 1])
            and current_low < float(valid["low"].iloc[position + 1])
        )
        if is_confirmed_low and float(valid["osc"].iloc[position]) < 0:
            trough_positions.append(position)

    if len(trough_positions) < 2:
        return None

    previous_position, recent_position = trough_positions[-2:]
    if recent_position < len(valid) - recent_trough_weeks:
        return None

    previous_price_low = float(valid["low"].iloc[previous_position])
    recent_price_low = float(valid["low"].iloc[recent_position])
    price_change_pct = (recent_price_low / previous_price_low - 1.0) * 100.0
    previous_osc = float(valid["osc"].iloc[previous_position])
    recent_osc = float(valid["osc"].iloc[recent_position])

    if price_change_pct > -min_lower_low_pct or recent_osc <= previous_osc:
        return None

    osc_recovery_pct = (recent_osc - previous_osc) / abs(previous_osc) * 100.0
    return {
        "previous_position": previous_position,
        "recent_position": recent_position,
        "previous_low_date": pd.Timestamp(valid.index[previous_position]).strftime("%Y-%m-%d"),
        "recent_low_date": pd.Timestamp(valid.index[recent_position]).strftime("%Y-%m-%d"),
        "previous_price_low": round(previous_price_low, 2),
        "recent_price_low": round(recent_price_low, 2),
        "price_lower_pct": round(price_change_pct, 2),
        "previous_osc": round(previous_osc, 4),
        "recent_osc": round(recent_osc, 4),
        "osc_recovery_pct": round(osc_recovery_pct, 2),
    }


def screen_daily_bars(
    daily_bars: pd.DataFrame,
    *,
    min_weekly_bars: int = DEFAULT_MIN_WEEKLY_BARS,
    recent_trough_weeks: int = DEFAULT_RECENT_TROUGH_WEEKS,
    min_lower_low_pct: float = DEFAULT_MIN_LOWER_LOW_PCT,
    max_stale_days: int = DEFAULT_MAX_STALE_DAYS,
    min_avg_volume_20d_lots: float | None = None,
) -> dict:
    """Screen an already-loaded ordinary-stock daily-bar universe."""
    if min_weekly_bars < 5:
        raise ValueError("min_weekly_bars must be at least 5")
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

    universe_count = int(daily_bars["code"].astype(str).nunique()) if not daily_bars.empty else 0
    excluded_ky_dr_count = len(excluded_ky_dr_codes)
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
            "max_weekly_bar_count": 0,
            "warmup_warning": "週 K 資料不足，無法計算 MACD 底背離。",
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
    weekly_counts = weekly.groupby(level="code").size()
    max_weekly_bar_count = int(weekly_counts.max()) if not weekly_counts.empty else 0

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

        analyzed_count += 1
        macd_bars = calculate_weekly_macd(stock_weekly)
        divergence = find_bullish_macd_divergence(
            macd_bars,
            recent_trough_weeks=recent_trough_weeks,
            min_lower_low_pct=min_lower_low_pct,
        )
        if divergence is None:
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

        latest = macd_bars.iloc[-1]
        matches.append({
            "code": str(code),
            "name": str(stock_meta["name"] or code),
            "category": str(stock_meta["category"] or "未分類"),
            "data_date": latest_date.strftime("%Y-%m-%d"),
            "close": round(float(latest["close"]), 2),
            "current_dif": round(float(latest["dif"]), 4),
            "current_signal": round(float(latest["signal"]), 4),
            "current_osc": round(float(latest["osc"]), 4),
            "weekly_bar_count": int(len(stock_weekly)),
            "daily_bar_count": int(stock_meta["daily_bar_count"]),
            **volume_metrics,
            **divergence,
        })

    matches.sort(
        key=lambda item: (
            item["recent_low_date"],
            item["osc_recovery_pct"],
            abs(item["price_lower_pct"]),
        ),
        reverse=True,
    )
    for rank, item in enumerate(matches, start=1):
        item["rank"] = rank

    warmup_warning = ""
    if max_weekly_bar_count < MATURE_MACD_WEEKLY_BARS:
        warmup_warning = (
            f"目前資料庫最長僅 {max_weekly_bar_count} 根週 K，"
            "MACD 26/9 仍在暖機期，結果僅供早期觀察。"
        )

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
        "max_weekly_bar_count": max_weekly_bar_count,
        "warmup_warning": warmup_warning,
        "results": matches,
    }


def scan_weekly_macd_divergence(
    db_path: str | Path,
    *,
    min_avg_volume_20d_lots: float | None = None,
) -> dict:
    """Read all 4-digit ordinary stocks from SQLite and scan MACD divergence."""
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
        min_avg_volume_20d_lots=min_avg_volume_20d_lots,
    )
    payload.update({
        "criteria": {
            "timeframe": "weekly",
            "macd": {"fast": 12, "slow": 26, "signal": 9, "osc_multiplier": 2},
            "divergence": "confirmed price lower-low with higher negative OSC low",
            "min_lower_low_pct": DEFAULT_MIN_LOWER_LOW_PCT,
            "recent_trough_weeks": DEFAULT_RECENT_TROUGH_WEEKS,
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
