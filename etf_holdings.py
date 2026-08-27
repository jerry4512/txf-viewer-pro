"""00981A-compatible ETF holdings cache and relative-allocation analyzer.

Research hypothesis
-------------------
This feature studies an active ETF manager's *relative allocation changes*, not
raw share-count changes.  The most interesting candidate is a stock that starts
at a low (or zero) weight and is actively accumulated over several disclosure
dates.  That pattern may reflect increasing conviction about growth, an
industry trend, or risk/reward.  It creates a research candidate only: price
behaviour still decides whether and when a trade is appropriate.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Optional


TAIPEI = timezone(timedelta(hours=8))

SUPPORTED_ETFS = {
    "00981A": "統一台股增長",
}

BEHAVIOR_LABELS = {
    "NEW_POSITION": "🆕 新建倉",
    "ACTIVE_ADD": "🟢 主動加碼",
    "PASSIVE_SCALE": "⚪ ETF規模調整",
    "HOLD": "⚪ 維持",
    "ACTIVE_REDUCE": "🔴 主動減碼",
    "EXIT_POSITION": "❌ 清倉",
}

INTENT_LABELS = {
    "FAST_ACCUMULATION": "🔥 快速建倉",
    "CONVICTION_RISING": "↑ 信念上升",
    "CORE_HOLDING": "→ 核心持有",
    "CONVICTION_DECLINING": "↓ 信念下降",
    "TACTICAL": "⚠️ 疑似戰術／短線操作",
    "WATCH": "持續觀察",
}


@dataclass(frozen=True)
class ETFAnalyzerConfig:
    """All research thresholds live here so none are hidden in the UI."""

    # A position must pass both thresholds to be treated as economically real.
    minimum_effective_quantity: int = 10_000
    minimum_effective_weight: float = 0.03

    # Baseline sample: liquid, existing positions without one-day extremes.
    baseline_minimum_quantity: int = 20_000
    baseline_minimum_weight: float = 0.05
    baseline_maximum_abs_change: float = 0.60
    baseline_mad_z: float = 3.5
    minimum_baseline_sample: int = 3

    passive_relative_tolerance: float = 0.04
    passive_weight_change_tolerance: float = 0.15
    active_relative_threshold: float = 0.08
    weight_confirmation_tolerance: float = 0.02
    strong_relative_threshold: float = 0.15

    fast_accumulation_add_events: int = 2
    fast_accumulation_relative: float = 0.15
    low_weight_threshold: float = 1.0
    core_weight_threshold: float = 5.0
    tactical_window: int = 5

    history_calendar_days: int = 75
    dashboard_windows: tuple[int, ...] = (5, 10, 20)

    score_base: int = 50
    score_behavior: dict[str, int] = field(default_factory=lambda: {
        "NEW_POSITION": 15,
        "ACTIVE_ADD": 10,
        "PASSIVE_SCALE": 0,
        "HOLD": 0,
        "ACTIVE_REDUCE": -10,
        "EXIT_POSITION": -25,
    })
    score_second_add: int = 8
    score_third_add: int = 12
    score_second_reduce: int = -10
    score_third_reduce: int = -14
    score_positive_weight_trend: int = 6
    score_negative_weight_trend: int = -6
    score_strong_relative: int = 10
    score_strong_negative_relative: int = -10
    score_low_weight_accumulation: int = 10
    score_core_holding: int = 8
    score_tactical_exit: int = -12


DEFAULT_CONFIG = ETFAnalyzerConfig()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _round(value: Optional[float], digits: int = 6) -> Optional[float]:
    return round(value, digits) if value is not None and math.isfinite(value) else None


class ETFHoldingsRepository:
    """SQLite repository preserving raw holdings and reproducible signals."""

    def __init__(self, db_path: str, stock_db_path: Optional[str] = None):
        self.db_path = db_path
        self.stock_db_path = stock_db_path
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=60.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def initialize(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS etf_holdings_daily (
                    etf_symbol       TEXT NOT NULL,
                    date             TEXT NOT NULL,
                    stock_symbol     TEXT NOT NULL,
                    stock_name       TEXT,
                    quantity         INTEGER NOT NULL,
                    quantity_change  INTEGER,
                    weight           REAL NOT NULL,
                    weight_change    REAL,
                    raw_json         TEXT NOT NULL,
                    fetched_at       TEXT NOT NULL,
                    PRIMARY KEY (etf_symbol, date, stock_symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_etf_holdings_symbol_date
                    ON etf_holdings_daily(etf_symbol, date);

                CREATE TABLE IF NOT EXISTS etf_holdings_sync (
                    etf_symbol       TEXT PRIMARY KEY,
                    last_from        TEXT,
                    last_to          TEXT,
                    last_data_date   TEXT,
                    last_fetched_at  TEXT,
                    sdk_version      TEXT,
                    response_meta    TEXT,
                    status           TEXT NOT NULL DEFAULT 'idle',
                    error            TEXT
                );

                CREATE TABLE IF NOT EXISTS etf_signals (
                    signal_date                 TEXT NOT NULL,
                    etf_symbol                  TEXT NOT NULL,
                    stock_symbol                TEXT NOT NULL,
                    analysis_period             INTEGER NOT NULL,
                    conviction_score            REAL NOT NULL,
                    behavior                    TEXT NOT NULL,
                    intent                      TEXT NOT NULL,
                    current_weight              REAL NOT NULL,
                    relative_allocation_change  REAL,
                    price_at_signal              REAL,
                    raw_json                     TEXT NOT NULL,
                    created_at                   TEXT NOT NULL,
                    PRIMARY KEY (
                        signal_date, etf_symbol, stock_symbol, analysis_period
                    )
                );
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(etf_signals)").fetchall()
            }
            if "analysis_period" not in columns:
                # Older v1 databases keyed signals by date/symbol only.  Keep
                # those rows as 5-day research results, then make period part
                # of the identity so 5D/10D/20D cannot overwrite each other.
                conn.executescript(
                    """
                    DROP INDEX IF EXISTS idx_etf_signals_research;
                    ALTER TABLE etf_signals RENAME TO etf_signals_legacy;
                    CREATE TABLE etf_signals (
                        signal_date                 TEXT NOT NULL,
                        etf_symbol                  TEXT NOT NULL,
                        stock_symbol                TEXT NOT NULL,
                        analysis_period             INTEGER NOT NULL,
                        conviction_score            REAL NOT NULL,
                        behavior                    TEXT NOT NULL,
                        intent                      TEXT NOT NULL,
                        current_weight              REAL NOT NULL,
                        relative_allocation_change  REAL,
                        price_at_signal              REAL,
                        raw_json                     TEXT NOT NULL,
                        created_at                   TEXT NOT NULL,
                        PRIMARY KEY (
                            signal_date, etf_symbol, stock_symbol, analysis_period
                        )
                    );
                    INSERT INTO etf_signals (
                        signal_date, etf_symbol, stock_symbol, analysis_period,
                        conviction_score, behavior, intent, current_weight,
                        relative_allocation_change, price_at_signal,
                        raw_json, created_at
                    )
                    SELECT
                        signal_date, etf_symbol, stock_symbol, 5,
                        conviction_score, behavior, intent, current_weight,
                        relative_allocation_change, price_at_signal,
                        raw_json, created_at
                    FROM etf_signals_legacy;
                    DROP TABLE etf_signals_legacy;
                    """
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_etf_signals_research
                ON etf_signals(
                    etf_symbol, signal_date, analysis_period, conviction_score
                )
                """
            )

    def save_response(
        self,
        payload: dict[str, Any],
        *,
        date_from: str,
        date_to: str,
        sdk_version: str,
    ) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        if not symbol:
            raise ValueError("ETF Holdings response 缺少 symbol")
        days = payload.get("data")
        if not isinstance(days, list):
            raise ValueError("ETF Holdings response.data 必須是陣列")

        fetched_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
        saved_rows = 0
        saved_dates: list[str] = []
        with self.connect() as conn:
            for day in days:
                if not isinstance(day, dict):
                    continue
                data_date = str(day.get("date") or "")
                components = day.get("components")
                if not data_date or not isinstance(components, list):
                    continue
                # A refresh is authoritative for that disclosure date. Deleting
                # first prevents a removed holding from lingering in the cache.
                conn.execute(
                    "DELETE FROM etf_holdings_daily WHERE etf_symbol=? AND date=?",
                    (symbol, data_date),
                )
                for component in components:
                    if not isinstance(component, dict):
                        continue
                    stock_symbol = str(component.get("symbol") or "").strip()
                    if not stock_symbol:
                        continue
                    quantity_change = component.get("quantityChange")
                    weight_change = component.get("weightChange")
                    conn.execute(
                        """
                        INSERT INTO etf_holdings_daily (
                            etf_symbol, date, stock_symbol, stock_name,
                            quantity, quantity_change, weight, weight_change,
                            raw_json, fetched_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            symbol,
                            data_date,
                            stock_symbol,
                            str(component.get("name") or ""),
                            _safe_int(component.get("quantity")),
                            None if quantity_change is None else _safe_int(quantity_change),
                            _safe_float(component.get("weight")),
                            None if weight_change is None else _safe_float(weight_change),
                            json.dumps(component, ensure_ascii=False, separators=(",", ":")),
                            fetched_at,
                        ),
                    )
                    saved_rows += 1
                saved_dates.append(data_date)

            meta = {
                key: payload.get(key)
                for key in ("symbol", "type", "exchange", "market")
            }
            last_data_date = max(saved_dates) if saved_dates else self.latest_date(symbol)
            conn.execute(
                """
                INSERT INTO etf_holdings_sync (
                    etf_symbol, last_from, last_to, last_data_date,
                    last_fetched_at, sdk_version, response_meta, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'success', NULL)
                ON CONFLICT(etf_symbol) DO UPDATE SET
                    last_from=excluded.last_from,
                    last_to=excluded.last_to,
                    last_data_date=excluded.last_data_date,
                    last_fetched_at=excluded.last_fetched_at,
                    sdk_version=excluded.sdk_version,
                    response_meta=excluded.response_meta,
                    status='success', error=NULL
                """,
                (
                    symbol, date_from, date_to, last_data_date, fetched_at,
                    sdk_version, json.dumps(meta, ensure_ascii=False),
                ),
            )
        return {
            "symbol": symbol,
            "dates": len(set(saved_dates)),
            "rows": saved_rows,
            "lastDataDate": max(saved_dates) if saved_dates else None,
            "fetchedAt": fetched_at,
        }

    def record_error(self, symbol: str, message: str, sdk_version: str = "") -> None:
        fetched_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO etf_holdings_sync (
                    etf_symbol, last_fetched_at, sdk_version, status, error
                ) VALUES (?, ?, ?, 'error', ?)
                ON CONFLICT(etf_symbol) DO UPDATE SET
                    last_fetched_at=excluded.last_fetched_at,
                    sdk_version=excluded.sdk_version,
                    status='error', error=excluded.error
                """,
                (symbol.upper(), fetched_at, sdk_version, message[:500]),
            )

    def latest_date(self, symbol: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT MAX(date) AS value FROM etf_holdings_daily WHERE etf_symbol=?",
                (symbol.upper(),),
            ).fetchone()
        return str(row["value"]) if row and row["value"] else None

    def sync_status(self, symbol: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM etf_holdings_sync WHERE etf_symbol=?",
                (symbol.upper(),),
            ).fetchone()
        return dict(row) if row else {}

    def load_history(self, symbol: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM etf_holdings_daily
                WHERE etf_symbol=? ORDER BY date ASC, stock_symbol ASC
                """,
                (symbol.upper(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def price_metrics(
        self,
        stock_symbols: Iterable[str],
        as_of_date: str,
    ) -> dict[str, dict[str, Any]]:
        symbols = sorted({str(value) for value in stock_symbols if value})
        if not self.stock_db_path or not symbols or not os.path.exists(self.stock_db_path):
            return {}
        placeholders = ",".join("?" for _ in symbols)
        query = f"""
            SELECT code, date, close FROM daily_kbars
            WHERE code IN ({placeholders}) AND date <= ?
            ORDER BY code ASC, date DESC
        """
        try:
            conn = sqlite3.connect(self.stock_db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query, (*symbols, as_of_date)).fetchall()
            conn.close()
        except (sqlite3.Error, OSError):
            return {}

        by_code: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_code.setdefault(str(row["code"]), []).append(row)
        result: dict[str, dict[str, Any]] = {}
        for code, prices in by_code.items():
            latest = _safe_float(prices[0]["close"])
            metrics: dict[str, Any] = {
                "currentPrice": latest or None,
                "priceDate": str(prices[0]["date"]),
                "priceSource": "stock_cache.daily_kbars",
            }
            for window in (5, 10, 20):
                previous = _safe_float(prices[window]["close"]) if len(prices) > window else 0.0
                metrics[f"return{window}D"] = (
                    _round((latest / previous - 1) * 100, 3)
                    if latest > 0 and previous > 0 else None
                )
            result[code] = metrics
        return result

    def save_signals(self, dashboard: dict[str, Any]) -> None:
        signal_date = str(dashboard.get("dataDate") or "")
        symbol = str(dashboard.get("etfSymbol") or "")
        analysis_period = _safe_int(dashboard.get("selectedPeriod"), 5)
        if not signal_date or not symbol:
            return
        created_at = datetime.now(TAIPEI).isoformat(timespec="seconds")
        with self.connect() as conn:
            for row in dashboard.get("holdings") or []:
                payload = {
                    key: row.get(key)
                    for key in (
                        "stockName", "quantity", "quantityChange", "weight",
                        "weightChange", "relativeAllocationChange", "behavior",
                        "activeAddCount", "activeReduceCount",
                        "cumulativeRelativeAllocationChange", "intent",
                        "convictionScore", "priceDate",
                    )
                }
                conn.execute(
                    """
                    INSERT INTO etf_signals (
                        signal_date, etf_symbol, stock_symbol, analysis_period,
                        conviction_score, behavior, intent, current_weight,
                        relative_allocation_change, price_at_signal,
                        raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        signal_date, etf_symbol, stock_symbol, analysis_period
                    ) DO UPDATE SET
                        conviction_score=excluded.conviction_score,
                        behavior=excluded.behavior,
                        intent=excluded.intent,
                        current_weight=excluded.current_weight,
                        relative_allocation_change=excluded.relative_allocation_change,
                        price_at_signal=excluded.price_at_signal,
                        raw_json=excluded.raw_json,
                        created_at=excluded.created_at
                    """,
                    (
                        signal_date, symbol, row.get("stockSymbol"), analysis_period,
                        _safe_float(row.get("convictionScore")), row.get("behavior"),
                        row.get("intent"), _safe_float(row.get("weight")),
                        row.get("cumulativeRelativeAllocationChange"),
                        row.get("currentPrice"),
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                        created_at,
                    ),
                )


class ETFHoldingsAnalyzer:
    """Transparent rule-based ETF manager behaviour classifier."""

    def __init__(self, config: ETFAnalyzerConfig = DEFAULT_CONFIG):
        self.config = config

    def _is_effective(self, quantity: int, weight: float) -> bool:
        return (
            quantity >= self.config.minimum_effective_quantity
            and weight >= self.config.minimum_effective_weight
        )

    def _baseline(self, previous: dict[str, dict], current: dict[str, dict]) -> tuple[float, int]:
        changes: list[float] = []
        for symbol in previous.keys() & current.keys():
            prev = previous[symbol]
            cur = current[symbol]
            prev_quantity = _safe_int(prev.get("quantity"))
            current_quantity = _safe_int(cur.get("quantity"))
            if (
                prev_quantity < self.config.baseline_minimum_quantity
                or current_quantity <= 0
                or min(_safe_float(prev.get("weight")), _safe_float(cur.get("weight")))
                < self.config.baseline_minimum_weight
            ):
                continue
            change = current_quantity / prev_quantity - 1
            if abs(change) <= self.config.baseline_maximum_abs_change:
                changes.append(change)
        if not changes:
            return 0.0, 0

        # Median + MAD rejection resists real active adds/reduces contaminating
        # the subscription/redemption scaling estimate.
        median = statistics.median(changes)
        deviations = [abs(value - median) for value in changes]
        mad = statistics.median(deviations)
        filtered = changes
        if mad > 1e-12 and len(changes) >= self.config.minimum_baseline_sample:
            robust_sigma = 1.4826 * mad
            filtered = [
                value for value in changes
                if abs(value - median) <= self.config.baseline_mad_z * robust_sigma
            ]
        return float(statistics.median(filtered or changes)), len(filtered or changes)

    def _classify(
        self,
        *,
        previous_quantity: int,
        current_quantity: int,
        previous_weight: float,
        current_weight: float,
        raw_change: Optional[float],
        relative_change: Optional[float],
        weight_change: float,
        baseline: float,
    ) -> tuple[str, str]:
        previous_effective = self._is_effective(previous_quantity, previous_weight)
        current_effective = self._is_effective(current_quantity, current_weight)
        if not previous_effective and current_effective:
            return "NEW_POSITION", "由低於有效部位門檻提升為實質持股"
        if previous_effective and not current_effective:
            return "EXIT_POSITION", "原有實質部位已降至有效部位門檻以下"
        if not previous_effective and not current_effective:
            return "HOLD", "持股仍低於有效部位門檻"

        relative = relative_change or 0.0
        if (
            relative >= self.config.active_relative_threshold
            and weight_change >= -self.config.weight_confirmation_tolerance
        ):
            return "ACTIVE_ADD", "扣除 ETF 整體縮放後仍明顯增配，且權重未反向惡化"
        if (
            relative <= -self.config.active_relative_threshold
            and weight_change <= self.config.weight_confirmation_tolerance
        ):
            return "ACTIVE_REDUCE", "扣除 ETF 整體縮放後仍明顯減配，且權重未反向改善"
        if (
            raw_change is not None
            and abs(baseline) >= 0.01
            and abs(relative) <= self.config.passive_relative_tolerance
            and abs(weight_change) <= self.config.passive_weight_change_tolerance
        ):
            return "PASSIVE_SCALE", "持股變化接近 ETF 整體申贖縮放基準，且權重變化未出現矛盾訊號"
        return "HOLD", "相對配置變化未跨越主動增減門檻"

    def _window_metrics(
        self,
        transitions: list[dict[str, Any]],
        window: int,
        disclosure_dates: list[str],
    ) -> dict[str, Any]:
        # Slice by the ETF's available disclosure dates, never by calendar-day
        # subtraction or by a stock's own history length.
        selected_dates = disclosure_dates[-window:]
        selected_set = set(selected_dates)
        rows = [row for row in transitions if row["date"] in selected_set]
        add_count = sum(row["behavior"] == "ACTIVE_ADD" for row in rows)
        reduce_count = sum(row["behavior"] == "ACTIVE_REDUCE" for row in rows)
        new_count = sum(row["behavior"] == "NEW_POSITION" for row in rows)
        exit_count = sum(row["behavior"] == "EXIT_POSITION" for row in rows)
        cumulative = sum(
            row["relativeAllocationChange"]
            for row in rows if row["relativeAllocationChange"] is not None
        )
        positive_weights = sum(_safe_float(row["weightChange"]) > 0 for row in rows)
        negative_weights = sum(_safe_float(row["weightChange"]) < 0 for row in rows)
        initial_weight = _safe_float(rows[0].get("previousWeight")) if rows else 0.0
        final_weight = _safe_float(rows[-1].get("weight")) if rows else initial_weight
        return {
            "window": window,
            "usedDisclosureDateCount": len(selected_dates),
            "disclosureDates": selected_dates,
            "activeAddCount": add_count,
            "activeReduceCount": reduce_count,
            "newPositionCount": new_count,
            "exitPositionCount": exit_count,
            "accumulationEventCount": add_count + new_count,
            "cumulativeRelativeAllocationChange": _round(cumulative),
            "positiveWeightChangeCount": positive_weights,
            "negativeWeightChangeCount": negative_weights,
            "initialWeight": _round(initial_weight),
            "weightChange": _round(final_weight - initial_weight),
        }

    def _score(
        self,
        current: dict[str, Any],
        metrics: dict[str, Any],
    ) -> tuple[int, list[dict[str, Any]]]:
        cfg = self.config
        score = cfg.score_base
        breakdown: list[dict[str, Any]] = [{"rule": "基礎分", "points": cfg.score_base}]

        def add(rule: str, points: int) -> None:
            nonlocal score
            if points:
                score += points
                breakdown.append({"rule": rule, "points": points})

        add(BEHAVIOR_LABELS[current["behavior"]], cfg.score_behavior[current["behavior"]])
        add_count = metrics["activeAddCount"]
        reduce_count = metrics["activeReduceCount"]
        if add_count >= 2:
            add("期間第 2 次主動加碼", cfg.score_second_add)
        if add_count >= 3:
            add("期間第 3 次以上主動加碼", cfg.score_third_add)
        if reduce_count >= 2:
            add("期間連續主動減碼", cfg.score_second_reduce)
        if reduce_count >= 3:
            add("期間第 3 次以上主動減碼", cfg.score_third_reduce)
        if metrics["positiveWeightChangeCount"] >= 2:
            add("權重持續提高", cfg.score_positive_weight_trend)
        if metrics["negativeWeightChangeCount"] >= 2:
            add("權重持續降低", cfg.score_negative_weight_trend)
        cumulative = _safe_float(metrics["cumulativeRelativeAllocationChange"])
        if cumulative >= cfg.strong_relative_threshold:
            add("相對配置明顯提高", cfg.score_strong_relative)
        elif cumulative <= -cfg.strong_relative_threshold:
            add("相對配置明顯降低", cfg.score_strong_negative_relative)
        if (
            metrics["initialWeight"] <= cfg.low_weight_threshold
            and metrics["accumulationEventCount"] >= cfg.fast_accumulation_add_events
        ):
            add("低權重開始累積", cfg.score_low_weight_accumulation)
        if (
            current["weight"] >= cfg.core_weight_threshold
            and add_count == 0 and reduce_count == 0
        ):
            add("高權重穩定核心", cfg.score_core_holding)
        if metrics["newPositionCount"] and metrics["exitPositionCount"]:
            add("短期建倉後退出", cfg.score_tactical_exit)
        return max(0, min(100, int(round(score)))), breakdown

    def _intent(
        self,
        current: dict[str, Any],
        metrics: dict[str, Any],
        window: int,
    ) -> tuple[str, str]:
        cfg = self.config
        if metrics["newPositionCount"] and metrics["exitPositionCount"]:
            return "TACTICAL", f"{window} 個揭露日內新建倉後又退出，暫視為戰術操作"
        if metrics["activeAddCount"] >= 2 and metrics["activeReduceCount"] >= 2:
            return "TACTICAL", f"{window} 個揭露日內同時多次增配與減配，方向反覆"
        if (
            metrics["initialWeight"] <= cfg.low_weight_threshold
            and metrics["accumulationEventCount"] >= cfg.fast_accumulation_add_events
            and _safe_float(metrics["cumulativeRelativeAllocationChange"])
            >= cfg.fast_accumulation_relative
            and metrics["positiveWeightChangeCount"] >= 2
            and metrics["activeReduceCount"] == 0
        ):
            return "FAST_ACCUMULATION", f"由低權重起步，{window} 個揭露日內多次主動提高相對配置"
        if metrics["activeAddCount"] >= 2:
            return "CONVICTION_RISING", f"{window} 個揭露日內多次主動增配，配置信念上升"
        if metrics["activeReduceCount"] >= 2:
            return "CONVICTION_DECLINING", f"{window} 個揭露日內多次主動減配，配置信念下降"
        if (
            current["weight"] >= cfg.core_weight_threshold
            and metrics["activeAddCount"] <= 1
            and metrics["activeReduceCount"] <= 1
            and abs(_safe_float(metrics["cumulativeRelativeAllocationChange"])) < 0.12
        ):
            return "CORE_HOLDING", f"{window} 個揭露日內維持高權重且相對配置穩定"
        if current["behavior"] in {"NEW_POSITION", "ACTIVE_ADD"}:
            return "CONVICTION_RISING", "最新揭露日顯示主動建立或提高配置"
        if current["behavior"] in {"ACTIVE_REDUCE", "EXIT_POSITION"}:
            return "CONVICTION_DECLINING", "最新揭露日顯示主動降低或退出配置"
        return "WATCH", "目前未形成連續增減配訊號"

    def analyze(
        self,
        raw_rows: list[dict[str, Any]],
        *,
        etf_symbol: str,
        selected_period: int = 5,
        price_metrics: Optional[dict[str, dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if not raw_rows:
            raise ValueError("ETF 持股快取尚無資料")
        selected_period = selected_period if selected_period in (1, 5, 10, 20) else 5
        by_date: dict[str, dict[str, dict[str, Any]]] = {}
        for row in raw_rows:
            data_date = str(row.get("date") or "")
            symbol = str(row.get("stock_symbol") or row.get("stockSymbol") or "")
            if not data_date or not symbol:
                continue
            by_date.setdefault(data_date, {})[symbol] = {
                "stockSymbol": symbol,
                "stockName": str(row.get("stock_name") or row.get("stockName") or ""),
                "quantity": _safe_int(row.get("quantity")),
                "quantityChange": (
                    None if row.get("quantity_change", row.get("quantityChange")) is None
                    else _safe_int(row.get("quantity_change", row.get("quantityChange")))
                ),
                "weight": _safe_float(row.get("weight")),
                "weightChange": _optional_float(row.get("weight_change", row.get("weightChange"))),
                "raw": row.get("raw_json") or row.get("raw"),
            }
        dates = sorted(by_date)
        if not dates:
            raise ValueError("ETF 持股快取沒有有效揭露日期")

        transition_by_symbol: dict[str, list[dict[str, Any]]] = {}
        baselines: list[dict[str, Any]] = []
        for index, data_date in enumerate(dates):
            current_map = by_date[data_date]
            previous_map = by_date[dates[index - 1]] if index else {}
            baseline, sample_size = self._baseline(previous_map, current_map) if index else (0.0, 0)
            baselines.append({
                "date": data_date,
                "fundScalingBaseline": _round(baseline),
                "sampleSize": sample_size,
            })
            for symbol in sorted(previous_map.keys() | current_map.keys()):
                current = current_map.get(symbol, {})
                previous = previous_map.get(symbol, {})
                current_quantity = _safe_int(current.get("quantity"))
                previous_quantity = _safe_int(previous.get("quantity"))
                current_weight = _safe_float(current.get("weight"))
                previous_weight = _safe_float(previous.get("weight"))
                raw_change = (
                    current_quantity / previous_quantity - 1
                    if previous_quantity > 0 else None
                )
                relative_change = raw_change - baseline if raw_change is not None else None
                api_weight_change = current.get("weightChange")
                weight_change = (
                    _safe_float(api_weight_change)
                    if api_weight_change is not None
                    else current_weight - previous_weight
                )
                if index == 0:
                    behavior, reason = "HOLD", "首個可用揭露日，沒有前期基準可比較"
                else:
                    behavior, reason = self._classify(
                        previous_quantity=previous_quantity,
                        current_quantity=current_quantity,
                        previous_weight=previous_weight,
                        current_weight=current_weight,
                        raw_change=raw_change,
                        relative_change=relative_change,
                        weight_change=weight_change,
                        baseline=baseline,
                    )
                # A symbolic holding crossing the effective-position threshold
                # can produce meaningless four/five-digit percentages.  Keep
                # the raw quantity change, but let NEW/EXIT carry the signal and
                # exclude that transition from cumulative relative percentages.
                if behavior in {"NEW_POSITION", "EXIT_POSITION"}:
                    relative_change = None
                transition_by_symbol.setdefault(symbol, []).append({
                    "date": data_date,
                    "previousDate": dates[index - 1] if index else None,
                    "stockSymbol": symbol,
                    "stockName": str(current.get("stockName") or previous.get("stockName") or ""),
                    "quantity": current_quantity,
                    "quantityChange": (
                        current.get("quantityChange")
                        if current.get("quantityChange") is not None
                        else current_quantity - previous_quantity
                    ),
                    "previousQuantity": previous_quantity,
                    "weight": _round(current_weight),
                    "weightChange": _round(weight_change),
                    "previousWeight": _round(previous_weight),
                    "rawHoldingChangePct": _round(raw_change),
                    "fundScalingBaseline": _round(baseline),
                    "relativeAllocationChange": _round(relative_change),
                    "behavior": behavior,
                    "behaviorLabel": BEHAVIOR_LABELS[behavior],
                    "behaviorReason": reason,
                })

        latest_date = dates[-1]
        prices = price_metrics or {}
        holdings: list[dict[str, Any]] = []
        for symbol, transitions in transition_by_symbol.items():
            current = transitions[-1]
            # Only current holdings and positions that exited on the latest date
            # belong in today's dashboard.
            if current["date"] != latest_date:
                continue
            chosen_window = selected_period
            analysis_windows = sorted({*self.config.dashboard_windows, chosen_window})
            metrics_by_window = {
                window: self._window_metrics(transitions, window, dates)
                for window in analysis_windows
            }
            metrics5 = metrics_by_window[5]
            metrics10 = metrics_by_window[10]
            scores: dict[int, int] = {}
            score_breakdowns: dict[int, list[dict[str, Any]]] = {}
            intents_by_window: dict[int, tuple[str, str]] = {}
            for window, metrics in metrics_by_window.items():
                scores[window], score_breakdowns[window] = self._score(current, metrics)
                intents_by_window[window] = self._intent(current, metrics, window)
            selected_metrics = metrics_by_window[chosen_window]
            intent, intent_reason = intents_by_window[chosen_window]
            row = dict(current)
            row.update({
                "analysisPeriod": chosen_window,
                "usedDisclosureDateCount": selected_metrics["usedDisclosureDateCount"],
                "activeAddCount": selected_metrics["activeAddCount"],
                "activeReduceCount": selected_metrics["activeReduceCount"],
                "newPositionCount": selected_metrics["newPositionCount"],
                "exitPositionCount": selected_metrics["exitPositionCount"],
                "cumulativeRelativeAllocationChange": selected_metrics["cumulativeRelativeAllocationChange"],
                "periodWeightChange": selected_metrics["weightChange"],
                "intent": intent,
                "intentLabel": INTENT_LABELS[intent],
                "intentReason": intent_reason,
                "metrics": {str(key): value for key, value in metrics_by_window.items()},
                "activeAddCount5D": metrics5["activeAddCount"],
                "activeReduceCount5D": metrics5["activeReduceCount"],
                "activeAddCount10D": metrics10["activeAddCount"],
                "activeReduceCount10D": metrics10["activeReduceCount"],
                "activeAddCount20D": metrics_by_window[20]["activeAddCount"],
                "activeReduceCount20D": metrics_by_window[20]["activeReduceCount"],
                "cumulativeRelativeAllocationChange5D": metrics5["cumulativeRelativeAllocationChange"],
                "cumulativeRelativeAllocationChange10D": metrics10["cumulativeRelativeAllocationChange"],
                "cumulativeRelativeAllocationChange20D": metrics_by_window[20]["cumulativeRelativeAllocationChange"],
                "convictionScore": scores[chosen_window],
                "convictionScore5D": scores[5],
                "convictionScore10D": scores[10],
                "convictionScore20D": scores[20],
                "scoreBreakdown": score_breakdowns[chosen_window],
                "scoreBreakdowns": {
                    str(key): value for key, value in score_breakdowns.items()
                },
                "intentsByWindow": {
                    str(key): {
                        "intent": value[0],
                        "intentLabel": INTENT_LABELS[value[0]],
                        "intentReason": value[1],
                    }
                    for key, value in intents_by_window.items()
                },
                "history": transitions,
            })
            row.update(prices.get(symbol) or {
                "currentPrice": None,
                "priceDate": None,
                "priceSource": None,
                "return5D": None,
                "return10D": None,
                "return20D": None,
            })
            holdings.append(row)

        priority = {
            "FAST_ACCUMULATION": 5,
            "CONVICTION_RISING": 4,
            "CORE_HOLDING": 3,
            "WATCH": 2,
            "CONVICTION_DECLINING": 1,
            "TACTICAL": 0,
        }
        holdings.sort(
            key=lambda row: (
                priority.get(row["intent"], 0),
                row["convictionScore"],
                row["weight"],
            ),
            reverse=True,
        )
        latest_baseline = baselines[-1]
        selected_disclosure_dates = dates[-selected_period:]
        summary = {
            "newPositions": sum(row["behavior"] == "NEW_POSITION" for row in holdings),
            "activeAdds": sum(row["behavior"] == "ACTIVE_ADD" for row in holdings),
            "activeReduces": sum(row["behavior"] == "ACTIVE_REDUCE" for row in holdings),
            "passiveScales": sum(row["behavior"] == "PASSIVE_SCALE" for row in holdings),
            "exits": sum(row["behavior"] == "EXIT_POSITION" for row in holdings),
            "fastAccumulations": sum(row["intent"] == "FAST_ACCUMULATION" for row in holdings),
            "convictionRising": sum(row["intent"] == "CONVICTION_RISING" for row in holdings),
            "convictionDeclining": sum(row["intent"] == "CONVICTION_DECLINING" for row in holdings),
            "fundScalingBaseline": latest_baseline["fundScalingBaseline"],
            "baselineSampleSize": latest_baseline["sampleSize"],
        }
        intents = {
            key: [
                {"stockSymbol": row["stockSymbol"], "stockName": row["stockName"]}
                for row in holdings if row["intent"] == key
            ]
            for key in INTENT_LABELS
        }
        return {
            "status": "success",
            "etfSymbol": etf_symbol,
            "etfName": SUPPORTED_ETFS.get(etf_symbol, etf_symbol),
            "dataDate": latest_date,
            "availableDates": dates,
            "selectedPeriod": selected_period,
            "analysisWindow": {
                "requestedPeriod": selected_period,
                "usedDisclosureDateCount": len(selected_disclosure_dates),
                "disclosureDates": selected_disclosure_dates,
                "from": selected_disclosure_dates[0] if selected_disclosure_dates else None,
                "to": latest_date,
            },
            "summary": summary,
            "intents": intents,
            "baselines": baselines,
            "holdings": holdings,
            "config": asdict(self.config),
            "disclaimer": "ETF 決定看誰，價格決定何時買；信念分數不是買進訊號。",
        }


class ETFHoldingsService:
    def __init__(
        self,
        repository: ETFHoldingsRepository,
        analyzer: Optional[ETFHoldingsAnalyzer] = None,
    ):
        self.repository = repository
        self.analyzer = analyzer or ETFHoldingsAnalyzer()

    def refresh(
        self,
        market_client: Any,
        symbol: str,
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        end = date.fromisoformat(date_to) if date_to else datetime.now(TAIPEI).date()
        start = (
            date.fromisoformat(date_from)
            if date_from else end - timedelta(days=self.analyzer.config.history_calendar_days)
        )
        payload = market_client.etf_holdings(
            symbol=symbol,
            start=start.isoformat(),
            end=end.isoformat(),
            sort="asc",
        )
        return self.repository.save_response(
            payload,
            date_from=start.isoformat(),
            date_to=end.isoformat(),
            sdk_version=str(getattr(market_client, "version", "unknown")),
        )

    def dashboard(self, symbol: str, period: int = 5) -> dict[str, Any]:
        symbol = symbol.strip().upper()
        history = self.repository.load_history(symbol)
        if not history:
            raise ValueError("ETF 持股資料目前無法取得：本地尚無快取")
        latest_date = max(str(row["date"]) for row in history)
        stock_symbols = {str(row["stock_symbol"]) for row in history}
        prices = self.repository.price_metrics(stock_symbols, latest_date)
        result = self.analyzer.analyze(
            history,
            etf_symbol=symbol,
            selected_period=period,
            price_metrics=prices,
        )
        sync = self.repository.sync_status(symbol)
        result["lastUpdated"] = sync.get("last_fetched_at")
        result["cache"] = {
            "source": "sqlite",
            "database": os.path.basename(self.repository.db_path),
            "sdkVersion": sync.get("sdk_version"),
            "lastRequestedFrom": sync.get("last_from"),
            "lastRequestedTo": sync.get("last_to"),
            "syncStatus": sync.get("status") or "cached",
            "syncError": sync.get("error"),
        }
        self.repository.save_signals(result)
        return result

    def detail(self, symbol: str, stock_symbol: str, period: int = 5) -> dict[str, Any]:
        dashboard = self.dashboard(symbol, period)
        stock_symbol = stock_symbol.strip().upper()
        row = next(
            (item for item in dashboard["holdings"] if item["stockSymbol"] == stock_symbol),
            None,
        )
        if row is None:
            raise KeyError(f"{symbol} 找不到持股 {stock_symbol}")
        return {
            "status": "success",
            "etfSymbol": dashboard["etfSymbol"],
            "etfName": dashboard["etfName"],
            "dataDate": dashboard["dataDate"],
            "lastUpdated": dashboard["lastUpdated"],
            "stock": row,
            "disclaimer": dashboard["disclaimer"],
        }
