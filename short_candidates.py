"""Explainable, read-only next-session short-candidate scanner.

The scanner consumes Fubon Neo market-data snapshots and historical daily
candles, persists raw factors in the existing SQLite stock cache, and never
places or prepares an order.  Every calculation is explicitly bounded by
``as_of_date`` so a historical rerun cannot see future rows already in SQLite.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional

from stock_selection_schema import ensure_stock_selection_schema


TAIPEI = timezone(timedelta(hours=8))


class ShortScannerError(RuntimeError):
    """User-safe scanner/data-completeness error."""


@dataclass(frozen=True)
class ShortScannerConfig:
    """Short V1 assumptions; these values are not backtest-optimized."""

    scanner_version: str = "short_v1"
    market_index_symbol: str = "IX0001"
    history_calendar_days: int = 240
    history_retention_bars: int = 150
    min_factor_bars: int = 21
    max_history_requests_per_run: int = 50
    metadata_candidates: int = 40
    support_window: int = 10
    volume_window: int = 20
    breakout_window: int = 20
    atr_window: int = 14
    min_avg_volume_20_lots: float = 500.0
    min_snapshot_total: int = 1000
    min_snapshot_per_market: int = 100
    snapshot_coverage_ratio: float = 0.80
    relative_weakness_full_pct: float = -3.0
    price_structure_weight: float = 35.0
    volume_candle_weight: float = 25.0
    relative_weakness_weight: float = 25.0
    capital_chip_weight: float = 15.0
    failed_breakout_points: float = 15.0
    breakdown_points: float = 10.0
    prior_return_points: float = 6.0
    prior_distance_points: float = 4.0
    close_location_points: float = 12.0
    relative_volume_points: float = 8.0
    upper_shadow_points: float = 5.0
    market_relative_points: float = 18.0
    industry_relative_points: float = 7.0
    prior_return_full_pct: float = 10.0
    prior_distance_full_pct: float = 8.0
    relative_volume_start: float = 1.0
    relative_volume_full: float = 2.5
    upper_shadow_full_ratio: float = 0.50
    chip_bearish_full_ratio: float = -0.50
    penalty_return_3d_pct: float = -8.0
    penalty_return_5d_pct: float = -12.0
    penalty_distance_5ma_pct: float = -5.0
    penalty_distance_20ma_pct: float = -10.0
    penalty_daily_move_atr: float = -1.5
    penalty_daily_return_pct: float = -7.0
    penalty_return_3d_points: float = 4.0
    penalty_return_5d_points: float = 4.0
    penalty_distance_5ma_points: float = 3.0
    penalty_distance_20ma_points: float = 3.0
    penalty_daily_move_atr_points: float = 3.0
    penalty_daily_return_points: float = 3.0

    def public_dict(self) -> dict[str, Any]:
        values = asdict(self)
        values["assumption"] = "Short Scanner V1 初始假設，尚未經回測最佳化"
        values["component_weights"] = {
            "price_structure": self.price_structure_weight,
            "volume_candle": self.volume_candle_weight,
            "relative_weakness": self.relative_weakness_weight,
            "capital_chip": self.capital_chip_weight,
        }
        values["normalization"] = (
            "Base Score = component sum / available component weight × 100; "
            "Final = clamp(Base − Overextension Penalty, 0, 100)"
        )
        values["missing_chip_policy"] = (
            "capital_chip_score 保留 null，Base Score 依可用 85% 權重等比例換算"
        )
        return values


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 4) -> Optional[float]:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] in "-/" and text[7] in "-/":
        return text[:10].replace("/", "-")
    if len(text) >= 8 and text[:8].isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return ""


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _normalize_volume_lots(
    volume: Any, close: Any = None, turnover: Any = None
) -> int:
    """Normalize Fubon stock volume to the existing daily_kbars lot unit."""
    raw = _finite_float(volume)
    if raw is None or raw <= 0:
        return 0
    price = _finite_float(close)
    amount = _finite_float(turnover)
    # Snapshot and daily Candle normally report shares.  The ratio check also
    # tolerates an SDK/provider payload that already reports board lots.
    if price and amount and price > 0:
        implied_multiplier = amount / (price * raw)
        if implied_multiplier >= 100:
            return max(0, int(round(raw)))
    return max(0, int(round(raw / 1000.0)))


def _clean_history(rows: Iterable[Mapping[str, Any]], as_of_date: str) -> list[dict[str, Any]]:
    by_date: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row_date = _date_text(raw.get("date"))
        if not row_date or row_date > as_of_date:
            continue
        open_price = _finite_float(raw.get("open"))
        high = _finite_float(raw.get("high"))
        low = _finite_float(raw.get("low"))
        close = _finite_float(raw.get("close"))
        volume = _finite_float(raw.get("volume"))
        if None in {open_price, high, low, close, volume} or close <= 0:
            continue
        by_date[row_date] = {
            "date": row_date,
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": max(0.0, float(volume)),
        }
    return [by_date[key] for key in sorted(by_date)]


def _return_pct(current: float, previous: float) -> Optional[float]:
    if previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def calculate_atr(rows: Iterable[Mapping[str, Any]], window: int = 14) -> Optional[float]:
    cleaned = list(rows)
    if len(cleaned) < window + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(cleaned)):
        current = cleaned[index]
        previous_close = float(cleaned[index - 1]["close"])
        high = float(current["high"])
        low = float(current["low"])
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    if len(true_ranges) < window:
        return None
    return sum(true_ranges[-window:]) / window


def calculate_factors(
    rows: Iterable[Mapping[str, Any]],
    *,
    as_of_date: str,
    market_return: float,
    industry_return: Optional[float] = None,
    config: ShortScannerConfig = ShortScannerConfig(),
) -> Optional[dict[str, Any]]:
    """Calculate raw factors using only rows whose date is <= as_of_date."""
    history = _clean_history(rows, as_of_date)
    if len(history) < config.min_factor_bars or history[-1]["date"] != as_of_date:
        return None

    today = history[-1]
    prior = history[:-1]
    if len(prior) < max(config.volume_window, config.breakout_window, config.support_window):
        return None

    open_price = today["open"]
    high = today["high"]
    low = today["low"]
    close = today["close"]
    volume = today["volume"]
    range_size = high - low
    close_location = (close - low) / range_size if range_size > 0 else 0.5
    upper_shadow_ratio = (
        max(0.0, high - max(open_price, close)) / range_size
        if range_size > 0 else 0.0
    )

    prior_volumes = [row["volume"] for row in prior[-config.volume_window:]]
    avg_volume_20 = sum(prior_volumes) / len(prior_volumes)
    relative_volume = volume / avg_volume_20 if avg_volume_20 > 0 else None
    prior_20d_high = max(row["high"] for row in prior[-config.breakout_window:])
    prior_support = min(row["low"] for row in prior[-config.support_window:])
    failed_breakout = high > prior_20d_high and close < prior_20d_high
    breakdown = close < prior_support
    breakdown_percent = _return_pct(close, prior_support)

    return_values: dict[int, Optional[float]] = {}
    for window in (3, 5, 10, 20):
        return_values[window] = (
            _return_pct(close, history[-(window + 1)]["close"])
            if len(history) >= window + 1 else None
        )
    ma5 = sum(row["close"] for row in history[-5:]) / 5
    ma20 = sum(row["close"] for row in history[-20:]) / 20
    atr14 = calculate_atr(history, config.atr_window)
    daily_return = _return_pct(close, history[-2]["close"])
    daily_move_atr = (
        (close - history[-2]["close"]) / atr14
        if atr14 is not None and atr14 > 0 else None
    )
    market_relative = (
        daily_return - market_return if daily_return is not None else None
    )
    industry_relative = (
        daily_return - industry_return
        if daily_return is not None and industry_return is not None else None
    )
    return {
        "date": as_of_date,
        "open": _round(open_price),
        "high": _round(high),
        "low": _round(low),
        "close": _round(close),
        "daily_return": _round(daily_return),
        "close_location": _round(max(0.0, min(1.0, close_location)), 6),
        "volume": int(round(volume)),
        "avg_volume_20": _round(avg_volume_20),
        "relative_volume": _round(relative_volume),
        "market_return": _round(market_return),
        "market_relative_strength": _round(market_relative),
        "industry_return": _round(industry_return),
        "industry_relative_strength": _round(industry_relative),
        "prior_20d_high": _round(prior_20d_high),
        "today_high": _round(high),
        "today_close": _round(close),
        "failed_breakout": bool(failed_breakout),
        "upper_shadow_ratio": _round(upper_shadow_ratio, 6),
        "prior_support": _round(prior_support),
        "breakdown": bool(breakdown),
        "breakdown_percent": _round(breakdown_percent),
        "return_3d": _round(return_values[3]),
        "return_5d": _round(return_values[5]),
        "return_10d": _round(return_values[10]),
        "return_20d": _round(return_values[20]),
        "ma5": _round(ma5),
        "ma20": _round(ma20),
        "distance_5ma": _round(_return_pct(close, ma5)),
        "distance_20ma": _round(_return_pct(close, ma20)),
        "atr14": _round(atr14),
        "daily_move_atr": _round(daily_move_atr),
    }


def calculate_overextension_penalty(
    factors: Mapping[str, Any], config: ShortScannerConfig = ShortScannerConfig()
) -> tuple[float, list[str]]:
    points = 0.0
    reasons: list[str] = []
    rules = (
        ("return_3d", config.penalty_return_3d_pct, config.penalty_return_3d_points, "3日跌幅過深"),
        ("return_5d", config.penalty_return_5d_pct, config.penalty_return_5d_points, "5日跌幅過深"),
        ("distance_5ma", config.penalty_distance_5ma_pct, config.penalty_distance_5ma_points, "遠離5MA"),
        ("distance_20ma", config.penalty_distance_20ma_pct, config.penalty_distance_20ma_points, "遠離20MA"),
        ("daily_move_atr", config.penalty_daily_move_atr, config.penalty_daily_move_atr_points, "單日跌幅超過ATR門檻"),
        ("daily_return", config.penalty_daily_return_pct, config.penalty_daily_return_points, "單日跌幅過深"),
    )
    for key, threshold, rule_points, label in rules:
        value = _finite_float(factors.get(key))
        if value is not None and value <= threshold:
            points += rule_points
            reasons.append(label)
    maximum = sum((
        config.penalty_return_3d_points,
        config.penalty_return_5d_points,
        config.penalty_distance_5ma_points,
        config.penalty_distance_20ma_points,
        config.penalty_daily_move_atr_points,
        config.penalty_daily_return_points,
    ))
    return round(min(maximum, points), 2), reasons


def _linear_points(value: Optional[float], start: float, full: float, maximum: float) -> float:
    if value is None:
        return 0.0
    if full == start:
        return maximum if value >= full else 0.0
    ratio = (value - start) / (full - start)
    return max(0.0, min(maximum, ratio * maximum))


def _weakness_points(value: Optional[float], full: float, maximum: float) -> float:
    if value is None or value >= 0:
        return 0.0
    if value <= full:
        return maximum
    return maximum * abs(value / full)


def calculate_component_scores(
    factors: Mapping[str, Any],
    *,
    chip: Optional[Mapping[str, Any]] = None,
    config: ShortScannerConfig = ShortScannerConfig(),
) -> dict[str, Any]:
    price_score = 0.0
    if factors.get("failed_breakout"):
        price_score += config.failed_breakout_points
    if factors.get("breakdown"):
        price_score += config.breakdown_points
    price_score += _linear_points(
        _finite_float(factors.get("return_20d")), 0.0,
        config.prior_return_full_pct, config.prior_return_points,
    )
    price_score += _linear_points(
        _finite_float(factors.get("distance_20ma")), 0.0,
        config.prior_distance_full_pct, config.prior_distance_points,
    )
    price_score = min(config.price_structure_weight, price_score)

    close_location = _finite_float(factors.get("close_location"))
    candle_score = (
        (1.0 - max(0.0, min(1.0, close_location))) * config.close_location_points
        if close_location is not None else 0.0
    )
    candle_score += _linear_points(
        _finite_float(factors.get("relative_volume")),
        config.relative_volume_start,
        config.relative_volume_full,
        config.relative_volume_points,
    )
    candle_score += _linear_points(
        _finite_float(factors.get("upper_shadow_ratio")),
        0.0,
        config.upper_shadow_full_ratio,
        config.upper_shadow_points,
    )
    candle_score = min(config.volume_candle_weight, candle_score)

    market_relative = _finite_float(factors.get("market_relative_strength"))
    industry_relative = _finite_float(factors.get("industry_relative_strength"))
    if industry_relative is None:
        relative_score = _weakness_points(
            market_relative, config.relative_weakness_full_pct,
            config.relative_weakness_weight,
        )
    else:
        relative_score = _weakness_points(
            market_relative, config.relative_weakness_full_pct, config.market_relative_points
        ) + _weakness_points(
            industry_relative, config.relative_weakness_full_pct, config.industry_relative_points
        )
    relative_score = min(config.relative_weakness_weight, relative_score)

    chip_status = str((chip or {}).get("data_status") or "missing")
    chip_ratio = _finite_float((chip or {}).get("net_ratio_5d"))
    chip_score: Optional[float]
    if chip_status not in {"available", "partial"} or chip_ratio is None:
        chip_score = None
    else:
        chip_score = _weakness_points(
            chip_ratio, config.chip_bearish_full_ratio, config.capital_chip_weight
        )

    available_weight = (
        config.price_structure_weight
        + config.volume_candle_weight
        + config.relative_weakness_weight
        + (config.capital_chip_weight if chip_score is not None else 0.0)
    )
    component_sum = price_score + candle_score + relative_score + (chip_score or 0.0)
    base_score = component_sum * 100.0 / available_weight if available_weight else 0.0
    penalty, penalty_reasons = calculate_overextension_penalty(factors, config)
    final_score = max(0.0, min(100.0, base_score - penalty))
    return {
        "price_structure_score": round(price_score, 2),
        "volume_candle_score": round(candle_score, 2),
        "relative_weakness_score": round(relative_score, 2),
        "capital_chip_score": round(chip_score, 2) if chip_score is not None else None,
        "chip_score": round(chip_score, 2) if chip_score is not None else None,
        "score_coverage_weight": int(available_weight),
        "base_short_score": round(base_score, 2),
        "overextension_penalty": penalty,
        "penalty_reasons": penalty_reasons,
        "short_score": round(final_score, 2),
    }


def _prior_trend_label(factors: Mapping[str, Any]) -> str:
    return_20 = _finite_float(factors.get("return_20d")) or 0.0
    distance = _finite_float(factors.get("distance_20ma")) or 0.0
    if return_20 >= 5 or distance >= 5:
        return "前期偏強，今日轉弱"
    if return_20 <= -8 and distance <= -5:
        return "已連跌／偏超跌"
    return "整理後轉弱"


def _candidate_reasons(
    factors: Mapping[str, Any], chip: Optional[Mapping[str, Any]], penalty: float
) -> list[str]:
    reasons: list[str] = []
    if factors.get("failed_breakout"):
        reasons.append("高檔突破失敗")
    if factors.get("breakdown"):
        reasons.append("跌破前期支撐")
    relative_volume = _finite_float(factors.get("relative_volume"))
    if relative_volume is not None and relative_volume >= 1.5:
        reasons.append(f"今日量比 {relative_volume:.2f}x")
    close_location = _finite_float(factors.get("close_location"))
    if close_location is not None and close_location <= 0.25:
        reasons.append(f"收盤位於當日區間 {close_location * 100:.0f}%")
    market_relative = _finite_float(factors.get("market_relative_strength"))
    if market_relative is not None and market_relative <= -1.0:
        reasons.append(f"弱於大盤 {abs(market_relative):.1f}%")
    return_5d = _finite_float(factors.get("return_5d"))
    if return_5d is not None and return_5d >= 3.0:
        reasons.append(f"過去5日仍上漲 {return_5d:.1f}%")
    if str((chip or {}).get("label")) == "籌碼偏空":
        reasons.append("法人籌碼轉弱")
    if penalty > 0:
        reasons.append(f"超跌懲罰 -{penalty:g}")
    return reasons or ["量價與相對弱勢綜合排名"]


class ShortCandidatesRepository:
    RESULT_COLUMNS = (
        "data_date", "symbol", "rank", "scanner_version", "name", "market", "industry",
        "open", "high", "low", "close", "daily_return", "close_location", "volume",
        "avg_volume_20", "relative_volume", "market_return", "market_relative_strength",
        "industry_return", "industry_relative_strength", "prior_20d_high", "today_high",
        "today_close", "failed_breakout", "upper_shadow_ratio", "prior_support", "breakdown",
        "breakdown_percent", "return_3d", "return_5d", "return_10d", "return_20d", "ma5",
        "ma20", "distance_5ma", "distance_20ma", "atr14", "daily_move_atr",
        "overextension_penalty", "chip_score", "chip_data_status", "chip_label",
        "institutional_net_5d", "price_structure_score", "volume_candle_score",
        "relative_weakness_score", "capital_chip_score", "score_coverage_weight",
        "base_short_score", "short_score", "day_trade_status", "day_trade_note",
        "can_day_trade", "can_buy_day_trade", "attention_status", "disposition_status",
        "security_status", "prior_trend", "pattern_label", "reasons_json", "raw_json",
        "updated_at",
    )

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    def init_schema(self) -> None:
        conn = self.connect()
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS daily_kbars (
                    code TEXT, date TEXT, open REAL, high REAL, low REAL,
                    close REAL, volume INTEGER, PRIMARY KEY(code, date)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS stock_names (
                    code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT DEFAULT ''
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS institutional_trading (
                    code TEXT, date TEXT, foreign_buy INTEGER, investment_buy INTEGER,
                    dealer_buy INTEGER, foreign_buy_shares INTEGER,
                    investment_buy_shares INTEGER, dealer_buy_shares INTEGER,
                    foreign_net INTEGER, trust_net INTEGER, dealer_prop_net INTEGER,
                    dealer_hedge_net INTEGER, dealer_unknown_net INTEGER,
                    flow_detail_level TEXT, flow_data_source TEXT,
                    PRIMARY KEY(code, date)
                )"""
            )
            ensure_stock_selection_schema(conn)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS short_market_index_daily (
                    date TEXT PRIMARY KEY, symbol TEXT NOT NULL, open REAL, high REAL,
                    low REAL, close REAL, volume REAL, source TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS short_candidate_runs (
                    data_date TEXT NOT NULL, scanner_version TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL,
                    summary_json TEXT NOT NULL, config_json TEXT NOT NULL,
                    warnings_json TEXT NOT NULL,
                    PRIMARY KEY(data_date, scanner_version)
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS short_candidate_results (
                    data_date TEXT NOT NULL, symbol TEXT NOT NULL, rank INTEGER NOT NULL,
                    scanner_version TEXT NOT NULL, name TEXT, market TEXT, industry TEXT,
                    open REAL, high REAL, low REAL, close REAL, daily_return REAL,
                    close_location REAL, volume INTEGER, avg_volume_20 REAL,
                    relative_volume REAL, market_return REAL, market_relative_strength REAL,
                    industry_return REAL, industry_relative_strength REAL,
                    prior_20d_high REAL, today_high REAL, today_close REAL,
                    failed_breakout INTEGER, upper_shadow_ratio REAL, prior_support REAL,
                    breakdown INTEGER, breakdown_percent REAL, return_3d REAL,
                    return_5d REAL, return_10d REAL, return_20d REAL, ma5 REAL, ma20 REAL,
                    distance_5ma REAL, distance_20ma REAL, atr14 REAL, daily_move_atr REAL,
                    overextension_penalty REAL, chip_score REAL, chip_data_status TEXT,
                    chip_label TEXT, institutional_net_5d REAL, price_structure_score REAL,
                    volume_candle_score REAL, relative_weakness_score REAL,
                    capital_chip_score REAL, score_coverage_weight INTEGER,
                    base_short_score REAL, short_score REAL, day_trade_status TEXT,
                    day_trade_note TEXT, can_day_trade INTEGER, can_buy_day_trade INTEGER,
                    attention_status TEXT, disposition_status TEXT, security_status TEXT,
                    prior_trend TEXT, pattern_label TEXT, reasons_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(data_date, symbol, scanner_version)
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_short_results_rank "
                "ON short_candidate_results(data_date, scanner_version, rank)"
            )
            conn.commit()
        finally:
            conn.close()

    def recent_universe_count(self, before_date: str) -> int:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT MAX(n) FROM (SELECT date, COUNT(DISTINCT code) n "
                "FROM daily_kbars WHERE date < ? GROUP BY date ORDER BY date DESC LIMIT 20)",
                (before_date,),
            ).fetchone()
            return int(row[0] or 0) if row else 0
        finally:
            conn.close()

    def upsert_snapshot(self, rows: Iterable[Mapping[str, Any]], data_date: str) -> int:
        payload = list(rows)
        now = datetime.now(TAIPEI).isoformat()
        conn = self.connect()
        try:
            conn.executemany(
                "INSERT INTO daily_kbars(code,date,open,high,low,close,volume) "
                "VALUES(:symbol,:data_date,:open,:high,:low,:close,:volume) "
                "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open, "
                "high=excluded.high, low=excluded.low, close=excluded.close, volume=excluded.volume",
                [{**row, "data_date": data_date} for row in payload],
            )
            conn.executemany(
                "INSERT INTO stock_names(code,name,category) VALUES(:symbol,:name,'') "
                "ON CONFLICT(code) DO UPDATE SET name=excluded.name",
                payload,
            )
            conn.executemany(
                """INSERT INTO security_master(
                    code,name,market,security_type,industry,source,updated_at
                ) VALUES(:symbol,:name,:market,'common_stock',:industry,'fubon_snapshot',:updated_at)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name, market=excluded.market,
                    security_type='common_stock', source='fubon_snapshot', updated_at=excluded.updated_at""",
                [{**row, "updated_at": now} for row in payload],
            )
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    def history_counts(self, symbols: Iterable[str], as_of_date: str) -> dict[str, int]:
        wanted = set(symbols)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT code,COUNT(*) n FROM daily_kbars WHERE date<=? GROUP BY code",
                (as_of_date,),
            ).fetchall()
            return {str(row["code"]): int(row["n"]) for row in rows if row["code"] in wanted}
        finally:
            conn.close()

    def upsert_history(self, symbol: str, rows: Iterable[Mapping[str, Any]]) -> int:
        payload = [{**row, "symbol": symbol} for row in rows]
        conn = self.connect()
        try:
            conn.executemany(
                "INSERT INTO daily_kbars(code,date,open,high,low,close,volume) "
                "VALUES(:symbol,:date,:open,:high,:low,:close,:volume) "
                "ON CONFLICT(code,date) DO UPDATE SET open=excluded.open, high=excluded.high, "
                "low=excluded.low, close=excluded.close, volume=excluded.volume",
                payload,
            )
            conn.commit()
            return len(payload)
        finally:
            conn.close()

    def load_histories(self, symbols: Iterable[str], as_of_date: str) -> dict[str, list[dict[str, Any]]]:
        wanted = set(symbols)
        output = {symbol: [] for symbol in wanted}
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT code,date,open,high,low,close,volume FROM daily_kbars "
                "WHERE date<=? ORDER BY code,date",
                (as_of_date,),
            ).fetchall()
            for row in rows:
                code = str(row["code"])
                if code in wanted:
                    output[code].append(dict(row))
            return output
        finally:
            conn.close()

    def upsert_market_history(self, symbol: str, rows: Iterable[Mapping[str, Any]]) -> None:
        now = datetime.now(TAIPEI).isoformat()
        conn = self.connect()
        try:
            conn.executemany(
                """INSERT INTO short_market_index_daily(
                    date,symbol,open,high,low,close,volume,source,updated_at
                ) VALUES(:date,:symbol,:open,:high,:low,:close,:volume,'fubon_historical',:updated_at)
                ON CONFLICT(date) DO UPDATE SET symbol=excluded.symbol, open=excluded.open,
                    high=excluded.high, low=excluded.low, close=excluded.close,
                    volume=excluded.volume, source=excluded.source, updated_at=excluded.updated_at""",
                [{**row, "symbol": symbol, "updated_at": now} for row in rows],
            )
            conn.commit()
        finally:
            conn.close()

    def market_return(self, as_of_date: str) -> Optional[float]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT date,close FROM short_market_index_daily WHERE date<=? "
                "ORDER BY date DESC LIMIT 2",
                (as_of_date,),
            ).fetchall()
            if len(rows) < 2 or str(rows[0]["date"]) != as_of_date:
                return None
            return _return_pct(float(rows[0]["close"]), float(rows[1]["close"]))
        finally:
            conn.close()

    def load_master(self, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
        wanted = set(symbols)
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT code,name,market,security_type,industry FROM security_master"
            ).fetchall()
            return {str(row["code"]): dict(row) for row in rows if row["code"] in wanted}
        finally:
            conn.close()

    def load_chip_data(self, symbols: Iterable[str], as_of_date: str) -> dict[str, dict[str, Any]]:
        wanted = set(symbols)
        conn = self.connect()
        try:
            dates = [str(row[0]) for row in conn.execute(
                "SELECT DISTINCT date FROM institutional_trading WHERE date<=? "
                "ORDER BY date DESC LIMIT 5", (as_of_date,)
            ).fetchall()]
            if not dates:
                return {}
            placeholders = ",".join("?" for _ in dates)
            rows = conn.execute(
                f"SELECT * FROM institutional_trading WHERE date IN ({placeholders})",
                dates,
            ).fetchall()
        finally:
            conn.close()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            code = str(row["code"])
            if code in wanted:
                grouped.setdefault(code, []).append(row)
        result: dict[str, dict[str, Any]] = {}
        for code, code_rows in grouped.items():
            total = 0.0
            for row in code_rows:
                foreign = row["foreign_net"]
                if foreign is None:
                    foreign = row["foreign_buy_shares"]
                if foreign is None:
                    foreign = float(row["foreign_buy"] or 0) * 1000
                trust = row["trust_net"]
                if trust is None:
                    trust = row["investment_buy_shares"]
                if trust is None:
                    trust = float(row["investment_buy"] or 0) * 1000
                dealer_values = [row["dealer_prop_net"], row["dealer_hedge_net"]]
                if any(value is not None for value in dealer_values):
                    dealer = sum(float(value or 0) for value in dealer_values)
                elif row["dealer_unknown_net"] is not None:
                    dealer = float(row["dealer_unknown_net"])
                elif row["dealer_buy_shares"] is not None:
                    dealer = float(row["dealer_buy_shares"])
                else:
                    dealer = float(row["dealer_buy"] or 0) * 1000
                total += float(foreign or 0) + float(trust or 0) + dealer
            latest_date = dates[0]
            result[code] = {
                "net_shares_5d": total,
                "days": len({str(row["date"]) for row in code_rows}),
                "data_date": latest_date,
                "data_status": (
                    "stale" if latest_date != as_of_date
                    else "available" if len(code_rows) >= len(dates)
                    else "partial"
                ),
            }
        return result

    def save_results(
        self,
        *,
        summary: Mapping[str, Any],
        config: ShortScannerConfig,
        candidates: list[Mapping[str, Any]],
        started_at: str,
        finished_at: str,
        warnings: list[str],
    ) -> None:
        data_date = str(summary["dataDate"])
        version = config.scanner_version
        placeholders = ",".join(f":{column}" for column in self.RESULT_COLUMNS)
        columns = ",".join(self.RESULT_COLUMNS)
        payload = []
        for candidate in candidates:
            row = {column: candidate.get(column) for column in self.RESULT_COLUMNS}
            row["data_date"] = data_date
            row["scanner_version"] = version
            row["failed_breakout"] = int(bool(candidate.get("failed_breakout")))
            row["breakdown"] = int(bool(candidate.get("breakdown")))
            for key in ("can_day_trade", "can_buy_day_trade"):
                value = candidate.get(key)
                row[key] = None if value is None else int(bool(value))
            row["reasons_json"] = json.dumps(candidate.get("reasons") or [], ensure_ascii=False)
            row["raw_json"] = json.dumps(dict(candidate), ensure_ascii=False, allow_nan=False)
            row["updated_at"] = finished_at
            payload.append(row)
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "DELETE FROM short_candidate_results WHERE data_date=? AND scanner_version=?",
                (data_date, version),
            )
            if payload:
                conn.executemany(
                    f"INSERT INTO short_candidate_results({columns}) VALUES({placeholders})",
                    payload,
                )
            conn.execute(
                """INSERT INTO short_candidate_runs(
                    data_date,scanner_version,started_at,finished_at,summary_json,config_json,warnings_json
                ) VALUES(?,?,?,?,?,?,?) ON CONFLICT(data_date,scanner_version) DO UPDATE SET
                    started_at=excluded.started_at, finished_at=excluded.finished_at,
                    summary_json=excluded.summary_json, config_json=excluded.config_json,
                    warnings_json=excluded.warnings_json""",
                (
                    data_date, version, started_at, finished_at,
                    json.dumps(dict(summary), ensure_ascii=False),
                    json.dumps(config.public_dict(), ensure_ascii=False),
                    json.dumps(warnings, ensure_ascii=False),
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def dashboard(
        self,
        *,
        data_date: Optional[str] = None,
        limit: int = 20,
        min_score: float = 0.0,
        sort: str = "score",
        scanner_version: str = "short_v1",
    ) -> dict[str, Any]:
        conn = self.connect()
        try:
            if not data_date:
                row = conn.execute(
                    "SELECT MAX(data_date) FROM short_candidate_runs WHERE scanner_version=?",
                    (scanner_version,),
                ).fetchone()
                data_date = str(row[0] or "") if row else ""
            if not data_date:
                return {
                    "scannerVersion": scanner_version,
                    "dataDate": None,
                    "lastUpdated": None,
                    "summary": {},
                    "config": ShortScannerConfig().public_dict(),
                    "warnings": [],
                    "candidates": [],
                }
            run = conn.execute(
                "SELECT * FROM short_candidate_runs WHERE data_date=? AND scanner_version=?",
                (data_date, scanner_version),
            ).fetchone()
            if not run:
                return {
                    "scannerVersion": scanner_version,
                    "dataDate": data_date,
                    "lastUpdated": None,
                    "summary": {},
                    "config": ShortScannerConfig().public_dict(),
                    "warnings": ["指定日期沒有已保存的 Short Scanner 結果"],
                    "candidates": [],
                }
            order_sql = "rank ASC" if sort == "rank" else "short_score DESC, rank ASC"
            rows = conn.execute(
                f"SELECT raw_json FROM short_candidate_results WHERE data_date=? "
                f"AND scanner_version=? AND short_score>=? ORDER BY {order_sql} LIMIT ?",
                (data_date, scanner_version, float(min_score), int(limit)),
            ).fetchall()
            return {
                "scannerVersion": scanner_version,
                "dataDate": data_date,
                "lastUpdated": str(run["finished_at"]),
                "summary": json.loads(run["summary_json"]),
                "config": json.loads(run["config_json"]),
                "warnings": json.loads(run["warnings_json"]),
                "candidates": [json.loads(row["raw_json"]) for row in rows],
            }
        finally:
            conn.close()


def _normalize_snapshot_payload(payload: Mapping[str, Any], market: str) -> dict[str, Any]:
    data_date = _date_text(payload.get("date"))
    rows: list[dict[str, Any]] = []
    trial_count = 0
    invalid_count = 0
    for raw in payload.get("data") or []:
        if not isinstance(raw, Mapping):
            invalid_count += 1
            continue
        symbol = str(raw.get("symbol") or "").strip().upper()
        open_price = _finite_float(raw.get("openPrice"))
        high = _finite_float(raw.get("highPrice"))
        low = _finite_float(raw.get("lowPrice"))
        close = _finite_float(raw.get("closePrice"))
        if not symbol or None in {open_price, high, low, close} or close <= 0:
            invalid_count += 1
            continue
        if _bool_or_none(raw.get("isTrial")):
            trial_count += 1
            continue
        turnover = _finite_float(raw.get("tradeValue")) or 0.0
        volume = _normalize_volume_lots(raw.get("tradeVolume"), close, turnover)
        rows.append({
            "symbol": symbol,
            "name": str(raw.get("name") or symbol).strip(),
            "market": market,
            "industry": "",
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": volume,
            "trade_value": turnover,
            "change": _finite_float(raw.get("change")),
            "change_percent": _finite_float(raw.get("changePercent")),
            "last_updated": raw.get("lastUpdated"),
        })
    return {
        "date": data_date,
        "time": str(payload.get("time") or ""),
        "market": market,
        "rows": rows,
        "trial_count": trial_count,
        "invalid_count": invalid_count,
    }


def validate_snapshot_completeness(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    prior_universe_count: int,
    config: ShortScannerConfig = ShortScannerConfig(),
) -> str:
    normalized = list(snapshots)
    dates = {str(item.get("date") or "") for item in normalized}
    if len(dates) != 1 or not next(iter(dates), ""):
        raise ShortScannerError("富邦 TSE／OTC Snapshot 資料日期不一致")
    data_date = next(iter(dates))
    market_counts = {str(item.get("market")): len(item.get("rows") or []) for item in normalized}
    if any(market_counts.get(market, 0) < config.min_snapshot_per_market for market in ("TSE", "OTC")):
        raise ShortScannerError(f"{data_date} 市場 Snapshot 檔數不足：{market_counts}")
    total = sum(market_counts.values())
    dynamic_minimum = int(math.ceil(prior_universe_count * config.snapshot_coverage_ratio))
    minimum = max(config.min_snapshot_total, dynamic_minimum)
    if total < minimum:
        raise ShortScannerError(
            f"{data_date} 市場 Snapshot 尚未完整：取得 {total}，完整性門檻 {minimum}"
        )
    if sum(int(item.get("trial_count") or 0) for item in normalized):
        raise ShortScannerError(f"{data_date} Snapshot 仍含試撮資料，請稍後再更新")
    return data_date


def _normalize_historical_payload(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in payload.get("data") or []:
        if not isinstance(raw, Mapping):
            continue
        row_date = _date_text(raw.get("date"))
        close = _finite_float(raw.get("close"))
        open_price = _finite_float(raw.get("open"))
        high = _finite_float(raw.get("high"))
        low = _finite_float(raw.get("low"))
        if not row_date or None in {close, open_price, high, low} or close <= 0:
            continue
        turnover = _finite_float(raw.get("turnover"))
        rows.append({
            "date": row_date,
            "open": float(open_price),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": _normalize_volume_lots(raw.get("volume"), close, turnover),
        })
    by_date = {row["date"]: row for row in rows}
    return [by_date[key] for key in sorted(by_date)]


def _day_trade_fields(details: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    if not details:
        return {
            "day_trade_status": "unknown",
            "day_trade_note": "富邦個股資格查詢失敗；不可假設次日可先賣後買",
            "can_day_trade": None,
            "can_buy_day_trade": None,
        }
    can_day_trade = _bool_or_none(details.get("canDayTrade"))
    can_buy = _bool_or_none(details.get("canBuyDayTrade"))
    if can_day_trade is False:
        status = "not_eligible"
        note = "富邦目前標示不可現股當沖"
    elif can_day_trade is True:
        status = "eligible_current_unverified_next_day"
        note = "富邦目前標示可現沖；資格可能於次交易日改變，並非先賣保證"
    else:
        status = "unknown"
        note = "富邦未回傳完整當沖資格；不可假設次日可先賣後買"
    return {
        "day_trade_status": status,
        "day_trade_note": note,
        "can_day_trade": can_day_trade,
        "can_buy_day_trade": can_buy,
    }


class ShortCandidatesService:
    def __init__(
        self,
        repository: ShortCandidatesRepository,
        config: ShortScannerConfig = ShortScannerConfig(),
    ):
        self.repository = repository
        self.config = config

    @staticmethod
    def _retry(call, attempts: int = 2):
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return call()
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(0.2)
        assert last_error is not None
        raise last_error

    def _load_market_return(self, client: Any, data_date: str) -> float:
        start = (date.fromisoformat(data_date) - timedelta(days=14)).isoformat()
        try:
            payload = self._retry(lambda: client.stock_historical_daily_candles(
                self.config.market_index_symbol, start, data_date
            ))
            rows = _normalize_historical_payload(payload)
            if rows:
                self.repository.upsert_market_history(self.config.market_index_symbol, rows)
        except Exception as exc:
            cached = self.repository.market_return(data_date)
            if cached is not None:
                return cached
            raise ShortScannerError(f"富邦加權指數日 K 取得失敗：{type(exc).__name__}") from exc
        result = self.repository.market_return(data_date)
        if result is None:
            raise ShortScannerError(
                f"富邦 {self.config.market_index_symbol} 尚未提供 {data_date} 完整日 K，請稍後再更新"
            )
        return result

    def _status_sets(self, client: Any, warnings: list[str]) -> dict[str, set[str]]:
        result = {"attention": set(), "disposition": set(), "halted": set()}
        filter_map = {
            "attention": {"isAttention": True},
            "disposition": {"isDisposition": True},
            "halted": {"isHalted": True},
        }
        for market in ("TSE", "OTC"):
            for key, filters in filter_map.items():
                try:
                    payload = self._retry(lambda m=market, f=filters: client.stock_tickers(m, **f))
                    result[key].update(
                        str(row.get("symbol") or "").strip().upper()
                        for row in payload.get("data") or [] if isinstance(row, Mapping)
                    )
                except Exception as exc:
                    warnings.append(f"{market} {key} 名單取得失敗：{type(exc).__name__}")
        return result

    def _backfill_history(
        self, client: Any, symbols: list[str], data_date: str, warnings: list[str]
    ) -> dict[str, Any]:
        start = (date.fromisoformat(data_date) - timedelta(days=self.config.history_calendar_days)).isoformat()
        requested = symbols[: self.config.max_history_requests_per_run]
        failures: list[str] = []
        inserted = 0
        completed = 0
        for symbol in requested:
            try:
                payload = self._retry(
                    lambda code=symbol: client.stock_historical_daily_candles(code, start, data_date)
                )
                rows = _normalize_historical_payload(payload)[-self.config.history_retention_bars:]
                if not rows:
                    raise ShortScannerError("empty history")
                inserted += self.repository.upsert_history(symbol, rows)
                completed += 1
            except Exception as exc:
                failures.append(symbol)
                warnings.append(f"{symbol} 歷史日 K 補取失敗：{type(exc).__name__}")
        return {
            "requested": len(requested),
            "completed": completed,
            "inserted_rows": inserted,
            "failures": failures,
            "deferred": max(0, len(symbols) - len(requested)),
        }

    def refresh(self, client: Any) -> dict[str, Any]:
        started_at = datetime.now(TAIPEI).isoformat()
        warnings: list[str] = []
        print(f"[SHORT_V1] start={started_at} scanner={self.config.scanner_version}")

        snapshots = []
        for market in ("TSE", "OTC"):
            payload = self._retry(lambda m=market: client.stock_snapshot_quotes(m, "COMMONSTOCK"))
            snapshots.append(_normalize_snapshot_payload(payload, market))
        provisional_date = snapshots[0].get("date") or "9999-12-31"
        prior_count = self.repository.recent_universe_count(str(provisional_date))
        data_date = validate_snapshot_completeness(
            snapshots, prior_universe_count=prior_count, config=self.config
        )
        market_return = self._load_market_return(client, data_date)
        status_sets = self._status_sets(client, warnings)

        raw_market_counts = {
            item["market"]: (
                len(item.get("rows") or [])
                + int(item.get("invalid_count") or 0)
                + int(item.get("trial_count") or 0)
            )
            for item in snapshots
        }
        snapshot_invalid = sum(
            int(item.get("invalid_count") or 0) + int(item.get("trial_count") or 0)
            for item in snapshots
        )
        all_snapshot_symbols = {
            row["symbol"] for snapshot in snapshots for row in snapshot["rows"]
        }
        halted_common = status_sets["halted"] & all_snapshot_symbols
        rows_by_symbol: dict[str, dict[str, Any]] = {}
        market_counts = {"TSE": 0, "OTC": 0}
        for snapshot in snapshots:
            for row in snapshot["rows"]:
                if row["symbol"] in halted_common:
                    continue
                rows_by_symbol[row["symbol"]] = row
                market_counts[row["market"]] += 1
        universe = list(rows_by_symbol.values())
        self.repository.upsert_snapshot(universe, data_date)

        symbols = sorted(rows_by_symbol)
        counts = self.repository.history_counts(symbols, data_date)
        needs_history = [
            symbol for symbol in symbols
            if counts.get(symbol, 0) < self.config.min_factor_bars
        ]
        backfill = self._backfill_history(client, needs_history, data_date, warnings)
        histories = self.repository.load_histories(symbols, data_date)
        master = self.repository.load_master(symbols)

        core: dict[str, dict[str, Any]] = {}
        missing_symbols: list[str] = []
        for symbol in symbols:
            factors = calculate_factors(
                histories.get(symbol, []),
                as_of_date=data_date,
                market_return=market_return,
                config=self.config,
            )
            if factors is None:
                missing_symbols.append(symbol)
                continue
            metadata = master.get(symbol) or {}
            industry = str(metadata.get("industry") or "").strip()
            core[symbol] = {
                **rows_by_symbol[symbol],
                **factors,
                "industry": industry,
            }

        industry_returns: dict[str, float] = {}
        for industry in sorted({str(row.get("industry") or "") for row in core.values()}):
            if not industry:
                continue
            values = [
                float(row["daily_return"]) for row in core.values()
                if row.get("industry") == industry and row.get("daily_return") is not None
            ]
            if len(values) >= 3:
                industry_returns[industry] = statistics.median(values)

        chip_by_symbol = self.repository.load_chip_data(core, data_date)
        candidates: list[dict[str, Any]] = []
        liquidity_excluded = 0
        for symbol, factors in core.items():
            industry_return = industry_returns.get(str(factors.get("industry") or ""))
            if industry_return is not None:
                factors["industry_return"] = _round(industry_return)
                factors["industry_relative_strength"] = _round(
                    float(factors["daily_return"]) - industry_return
                )
            avg_volume = float(factors.get("avg_volume_20") or 0)
            if avg_volume < self.config.min_avg_volume_20_lots:
                liquidity_excluded += 1
                continue
            chip = chip_by_symbol.get(symbol)
            if chip:
                denominator = avg_volume * 1000.0
                chip["net_ratio_5d"] = (
                    float(chip["net_shares_5d"]) / denominator if denominator > 0 else None
                )
                ratio = chip.get("net_ratio_5d")
                chip["label"] = (
                    "籌碼偏空" if ratio is not None and ratio <= -0.10
                    else "籌碼偏多" if ratio is not None and ratio >= 0.10
                    else "籌碼中性"
                )
            else:
                chip = {"data_status": "missing", "label": "籌碼缺資料", "net_shares_5d": None}
            if chip.get("data_status") == "stale":
                chip["label"] = "籌碼資料落後"
            scores = calculate_component_scores(factors, chip=chip, config=self.config)
            pattern_parts = []
            if factors.get("failed_breakout"):
                pattern_parts.append("突破失敗")
            if factors.get("breakdown"):
                pattern_parts.append("跌破支撐")
            candidate = {
                **factors,
                **scores,
                "symbol": symbol,
                "name": rows_by_symbol[symbol]["name"],
                "market": rows_by_symbol[symbol]["market"],
                "industry": factors.get("industry") or "unavailable",
                "chip_data_status": chip.get("data_status") or "missing",
                "chip_label": chip.get("label") or "籌碼缺資料",
                "institutional_net_5d": chip.get("net_shares_5d"),
                "prior_trend": _prior_trend_label(factors),
                "pattern_label": "＋".join(pattern_parts) if pattern_parts else "轉弱觀察",
                "attention_status": "注意" if symbol in status_sets["attention"] else "正常",
                "disposition_status": "處置" if symbol in status_sets["disposition"] else "正常",
                "security_status": "UNKNOWN",
                **_day_trade_fields(None),
            }
            candidate["reasons"] = _candidate_reasons(
                factors, chip, float(scores["overextension_penalty"])
            )
            candidates.append(candidate)

        candidates.sort(key=lambda row: (-float(row["short_score"]), row["symbol"]))
        metadata_failures = 0
        abnormal_excluded: set[str] = set(halted_common)
        for candidate in candidates[: self.config.metadata_candidates]:
            try:
                details = self._retry(
                    lambda code=candidate["symbol"]: client.stock_ticker_details(code)
                )
                candidate.update(_day_trade_fields(details))
                candidate["attention_status"] = "注意" if _bool_or_none(details.get("isAttention")) else candidate["attention_status"]
                candidate["disposition_status"] = "處置" if _bool_or_none(details.get("isDisposition")) else candidate["disposition_status"]
                candidate["security_status"] = str(details.get("securityStatus") or "UNKNOWN").upper()
                detail_industry = str(details.get("industry") or "").strip()
                if detail_industry and candidate["industry"] == "unavailable":
                    candidate["industry"] = detail_industry
                if candidate["security_status"] not in {"NORMAL", ""}:
                    abnormal_excluded.add(candidate["symbol"])
            except Exception as exc:
                metadata_failures += 1
                warnings.append(f"{candidate['symbol']} 當沖資格查詢失敗：{type(exc).__name__}")

        if abnormal_excluded:
            candidates = [row for row in candidates if row["symbol"] not in abnormal_excluded]
        candidates.sort(key=lambda row: (-float(row["short_score"]), row["symbol"]))
        for rank, candidate in enumerate(candidates, start=1):
            candidate["rank"] = rank
            candidate["scanner_version"] = self.config.scanner_version
            candidate["data_date"] = data_date

        missing_chip_count = sum(
            row["chip_data_status"] not in {"available", "partial"}
            for row in candidates
        )
        chip_dates = [
            str(row.get("data_date") or "") for row in chip_by_symbol.values()
            if row.get("data_date")
        ]
        chip_data_date = max(chip_dates) if chip_dates else None
        summary = {
            "dataDate": data_date,
            "scannerVersion": self.config.scanner_version,
            "universeTotal": sum(raw_market_counts.values()),
            "tseCount": raw_market_counts.get("TSE", 0),
            "otcCount": raw_market_counts.get("OTC", 0),
            "eligibleUniverse": len(universe),
            "snapshotInvalidExcluded": snapshot_invalid,
            "successCount": len(core),
            "missingDataCount": len(missing_symbols),
            "apiFailureCount": len(backfill["failures"]) + metadata_failures,
            "liquidityExcluded": liquidity_excluded,
            "abnormalExcluded": len(abnormal_excluded),
            "excludedCount": (
                snapshot_invalid + len(missing_symbols)
                + liquidity_excluded + len(abnormal_excluded)
            ),
            "validCandidates": len(candidates),
            "missingChipCount": missing_chip_count,
            "chipDataDate": chip_data_date,
            "historyBackfillRequested": backfill["requested"],
            "historyBackfillCompleted": backfill["completed"],
            "historyBackfillDeferred": backfill["deferred"],
            "marketReturn": _round(market_return),
            "snapshotTimes": {item["market"]: item["time"] for item in snapshots},
            "source": "Fubon Neo Snapshot + Historical; local SQLite factors",
        }
        if backfill["deferred"]:
            warnings.append(
                f"尚有 {backfill['deferred']} 檔歷史資料待下次補取（V1 每次限制 "
                f"{self.config.max_history_requests_per_run} 次，避免超過富邦 Historical 60/min）"
            )
        if chip_data_date and chip_data_date != data_date:
            warnings.append(
                f"法人籌碼最新資料日為 {chip_data_date}，落後行情日 {data_date}；"
                "籌碼分數已標記 unavailable 並排除於總分"
            )
        warnings.append("當沖資格是富邦目前狀態，無法保證次交易日仍可先賣後買")
        finished_at = datetime.now(TAIPEI).isoformat()
        self.repository.save_results(
            summary=summary,
            config=self.config,
            candidates=candidates,
            started_at=started_at,
            finished_at=finished_at,
            warnings=warnings,
        )
        top = [(row["rank"], row["symbol"], row["short_score"]) for row in candidates[:20]]
        print(
            f"[SHORT_V1] finish={finished_at} date={data_date} universe={len(universe)} "
            f"success={len(core)} missing={len(missing_symbols)} liquidity_excluded={liquidity_excluded} "
            f"top20={top}"
        )
        return self.repository.dashboard(limit=20, scanner_version=self.config.scanner_version)
