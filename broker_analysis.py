"""
broker_analysis.py

Standalone key-broker analysis helpers for the stock viewer.  This module is
intentionally read-only for strategy outputs: it creates broker tables when
needed, reads broker rows, and returns a stable API payload even when no broker
data has been imported yet.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any


NO_DATA_STATUS = "無資料"


def ensure_broker_tables(conn: sqlite3.Connection) -> None:
    """Create broker tables and indexes without touching existing strategy tables."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_trading_daily (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            broker_id TEXT,
            broker_name TEXT NOT NULL,
            branch_id TEXT,
            branch_name TEXT NOT NULL,
            buy_qty INTEGER DEFAULT 0,
            sell_qty INTEGER DEFAULT 0,
            net_qty INTEGER DEFAULT 0,
            buy_amount REAL DEFAULT 0,
            sell_amount REAL DEFAULT 0,
            net_amount REAL DEFAULT 0,
            source TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, date, broker_name, branch_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_watchlist (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broker_name TEXT NOT NULL,
            branch_name TEXT NOT NULL,
            broker_type TEXT,
            note TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(broker_name, branch_name)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_daily_code_date
        ON broker_trading_daily(code, date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_daily_code_branch_date
        ON broker_trading_daily(code, broker_name, branch_name, date)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_daily_branch
        ON broker_trading_daily(broker_name, branch_name)
        """
    )
    conn.commit()


def _dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def resolve_stock_query(conn: sqlite3.Connection, query: str) -> dict[str, str] | None:
    """Resolve a stock code or name query against stock_names."""
    q = str(query or "").strip()
    if not q:
        return None

    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT code, name, category FROM stock_names WHERE code = ? LIMIT 1",
        (q,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT code, name, category FROM stock_names WHERE name = ? LIMIT 1",
            (q,),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            SELECT code, name, category
            FROM stock_names
            WHERE name LIKE ? OR code LIKE ?
            ORDER BY CASE WHEN code LIKE ? THEN 0 ELSE 1 END, code
            LIMIT 1
            """,
            (f"%{q}%", f"%{q}%", f"{q}%"),
        ).fetchone()
    if row is None:
        return None
    return {
        "code": str(row["code"] or ""),
        "name": str(row["name"] or ""),
        "category": str(row["category"] or ""),
    }


def _latest_broker_date(conn: sqlite3.Connection, code: str, as_of_date: str | None) -> str | None:
    if as_of_date:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM broker_trading_daily WHERE code = ? AND date <= ?",
            (code, as_of_date),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(date) AS d FROM broker_trading_daily WHERE code = ?",
            (code,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


def get_broker_rows(conn: sqlite3.Connection, code: str, as_of_date: str | None, days: int) -> list[dict[str, Any]]:
    """Return rows for the latest N trading dates available in broker_trading_daily."""
    conn.row_factory = sqlite3.Row
    latest = _latest_broker_date(conn, code, as_of_date)
    if not latest:
        return []
    dates = [
        r["date"]
        for r in conn.execute(
            """
            SELECT DISTINCT date
            FROM broker_trading_daily
            WHERE code = ? AND date <= ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, latest, int(days)),
        ).fetchall()
    ]
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""
        SELECT code, date, broker_id, broker_name, branch_id, branch_name,
               buy_qty, sell_qty, net_qty, buy_amount, sell_amount, net_amount, source
        FROM broker_trading_daily
        WHERE code = ? AND date IN ({placeholders})
        ORDER BY date DESC, ABS(net_qty) DESC
        """,
        [code, *dates],
    ).fetchall()
    return [_dict(r) for r in rows]


def _get_daily_kbars(conn: sqlite3.Connection, code: str, as_of_date: str | None, days: int = 10) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    if as_of_date:
        rows = conn.execute(
            """
            SELECT code, date, open, high, low, close, volume
            FROM daily_kbars
            WHERE code = ? AND date <= ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, as_of_date, days),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT code, date, open, high, low, close, volume
            FROM daily_kbars
            WHERE code = ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, days),
        ).fetchall()
    return [_dict(r) for r in rows]


def _broker_dates_in_recent_trading_days(conn: sqlite3.Connection, code: str, as_of_date: str | None, days: int) -> set[str]:
    conn.row_factory = sqlite3.Row
    daily_kbars = _get_daily_kbars(conn, code, as_of_date, days)
    recent_dates = [str(r.get("date") or "") for r in daily_kbars if r.get("date")]
    if as_of_date and as_of_date not in recent_dates:
        recent_dates = [as_of_date, *[d for d in recent_dates if d < as_of_date]][: int(days)]
    if not recent_dates:
        rows = conn.execute(
            """
            SELECT DISTINCT date
            FROM broker_trading_daily
            WHERE code = ? AND (? IS NULL OR date <= ?)
            ORDER BY date DESC
            LIMIT ?
            """,
            (code, as_of_date, as_of_date, int(days)),
        ).fetchall()
        recent_dates = [str(r["date"]) for r in rows]
    if not recent_dates:
        return set()
    placeholders = ",".join("?" for _ in recent_dates)
    rows = conn.execute(
        f"""
        SELECT DISTINCT date
        FROM broker_trading_daily
        WHERE code = ? AND date IN ({placeholders})
        """,
        [code, *recent_dates],
    ).fetchall()
    return {str(r["date"]) for r in rows}


def _data_completeness_warning(available_days_5d: int, available_days_10d: int) -> str:
    if available_days_5d < 3:
        return "目前分點資料少於 3 個交易日，僅適合看單日買賣超，不適合判斷連續買賣。"
    if available_days_5d < 5:
        return "目前分點資料未滿 5 個交易日，5日分點判斷仍不完整。"
    if available_days_10d < 10:
        return "10日分點判斷資料尚未完整。"
    return ""


def _display_name(row_or_key: Any) -> str:
    if isinstance(row_or_key, tuple):
        return f"{row_or_key[0]}-{row_or_key[1]}"
    return f"{row_or_key.get('broker_name', '')}-{row_or_key.get('branch_name', '')}"


def _aggregate(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    date_signs: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in rows:
        key = (str(r.get("broker_name") or ""), str(r.get("branch_name") or ""))
        item = agg.setdefault(
            key,
            {
                "broker_name": key[0],
                "branch_name": key[1],
                "display_name": _display_name(key),
                "net": 0,
                "buy_qty": 0,
                "sell_qty": 0,
                "abs_net": 0,
            },
        )
        net = int(r.get("net_qty") or 0)
        item["net"] += net
        item["buy_qty"] += int(r.get("buy_qty") or 0)
        item["sell_qty"] += int(r.get("sell_qty") or 0)
        item["abs_net"] += abs(net)
        date_signs[key][str(r.get("date") or "")] = 1 if net > 0 else -1 if net < 0 else 0
    for key, item in agg.items():
        signs = date_signs[key]
        item["buy_days"] = sum(1 for s in signs.values() if s > 0)
        item["sell_days"] = sum(1 for s in signs.values() if s < 0)
        item["active_days"] = sum(1 for s in signs.values() if s != 0)
    return agg


def _volume_sum(daily_kbars: list[dict[str, Any]], days: int) -> float:
    return float(sum(float(r.get("volume") or 0) for r in daily_kbars[:days]))


def _volume_sum_for_broker_dates(daily_kbars: list[dict[str, Any]], broker_rows: list[dict[str, Any]], days: int) -> float:
    volume_by_date = {
        str(r.get("date") or ""): float(r.get("volume") or 0)
        for r in daily_kbars[:days]
        if r.get("date")
    }
    broker_dates = {
        str(r.get("date") or "")
        for r in broker_rows
        if r.get("date")
    }
    matched_volume = sum(volume_by_date[d] for d in broker_dates if d in volume_by_date)
    if matched_volume > 0:
        return float(matched_volume)
    return _volume_sum(daily_kbars, days)


def _detect_volume_unit(daily_kbars: list[dict[str, Any]], rows: list[dict[str, Any]]) -> str:
    """Return lots or shares for daily_kbars.volume.

    Official broker CSV quantities are shares.  The local daily_kbars table in
    this app is usually stored in lots, so compare overlapping dates when
    possible and fall back to the magnitude commonly seen in the cache.
    """
    volume_by_date = {
        str(r.get("date") or ""): float(r.get("volume") or 0)
        for r in daily_kbars
        if r.get("date") and float(r.get("volume") or 0) > 0
    }
    broker_by_date: dict[str, dict[str, int]] = defaultdict(lambda: {"buy": 0, "sell": 0})
    for row in rows:
        d = str(row.get("date") or "")
        if not d:
            continue
        broker_by_date[d]["buy"] += int(row.get("buy_qty") or 0)
        broker_by_date[d]["sell"] += int(row.get("sell_qty") or 0)

    ratios = []
    for d, broker_qty in broker_by_date.items():
        volume = volume_by_date.get(d)
        if not volume:
            continue
        broker_shares = max(broker_qty["buy"], broker_qty["sell"])
        if broker_shares > 0:
            ratios.append(broker_shares / volume)

    if ratios:
        ratios.sort()
        median_ratio = ratios[len(ratios) // 2]
        if median_ratio > 100:
            return "lots"
        if median_ratio < 10:
            return "shares"

    positive_volumes = [v for v in volume_by_date.values() if v > 0]
    if positive_volumes:
        positive_volumes.sort()
        median_volume = positive_volumes[len(positive_volumes) // 2]
        if median_volume < 1_000_000:
            return "lots"
    return "shares"


def _broker_qty_for_volume_ratio(net_qty_shares: int, volume_unit: str) -> float:
    if volume_unit == "lots":
        return float(net_qty_shares) / 1000.0
    return float(net_qty_shares)


def _volume_ratio_pct(net_qty_shares: int, total_volume: float, volume_unit: str) -> float:
    if not total_volume:
        return 0.0
    broker_qty = _broker_qty_for_volume_ratio(net_qty_shares, volume_unit)
    return abs(broker_qty) / total_volume * 100


def classify_latest_action(net_5d: int, net_10d: int, buy_days_5d: int, sell_days_5d: int) -> str:
    if net_5d > 0 and net_10d > 0 and buy_days_5d >= 3:
        return "連續買超"
    if net_5d > 0:
        return "偏買"
    if net_5d < 0 and sell_days_5d >= 3:
        return "連續賣超"
    if net_5d < 0:
        return "偏賣"
    return "中性"


def classify_broker_type(net_5d: int, net_10d: int, buy_days_5d: int, sell_days_5d: int) -> str:
    if net_5d > 0 and net_10d > 0 and buy_days_5d >= 3:
        return "波段累積"
    if net_5d > 0 and buy_days_5d >= 2:
        return "短線偏多"
    if net_5d < 0 and sell_days_5d >= 3:
        return "籌碼轉弱"
    if net_10d > 0 and net_5d <= 0:
        return "可能換手"
    if net_5d < 0:
        return "偏空賣壓"
    return "中性觀察"


def _score_5d(agg5: dict[tuple[str, str], dict[str, Any]], total_volume_5d: float, volume_unit: str) -> int:
    if not agg5:
        return 0
    top_buy = sorted(agg5.values(), key=lambda x: x["net"], reverse=True)
    top_sell = sorted(agg5.values(), key=lambda x: x["net"])
    score = 0
    strong_buy = [x for x in top_buy if x["net"] > 0]
    strong_sell = [x for x in top_sell if x["net"] < 0]
    if strong_buy and strong_buy[0]["buy_days"] >= 3:
        score += 3
    if len([x for x in strong_buy[:3] if x["buy_days"] >= 3]) >= 1:
        score += 2
    if total_volume_5d > 0 and strong_buy:
        ratio = _volume_ratio_pct(int(strong_buy[0]["net"]), total_volume_5d, volume_unit)
        if ratio >= 6:
            score += 5
        elif ratio >= 3:
            score += 3
    if len(strong_buy) >= 2:
        score += 2
    if strong_sell:
        if strong_sell[0]["sell_days"] >= 3:
            score -= 5
        if total_volume_5d > 0 and _volume_ratio_pct(int(strong_sell[0]["net"]), total_volume_5d, volume_unit) >= 5:
            score -= 3
    return max(-20, min(20, score))


def _score_10d(agg10: dict[tuple[str, str], dict[str, Any]], agg5: dict[tuple[str, str], dict[str, Any]]) -> int:
    if not agg10:
        return 0
    score = 0
    top_buy = sorted(agg10.values(), key=lambda x: x["net"], reverse=True)
    top_sell = sorted(agg10.values(), key=lambda x: x["net"])
    if top_buy and top_buy[0]["net"] > 0 and top_buy[0]["buy_days"] >= 6:
        score += 4
    if len([x for x in top_buy[:3] if x["net"] > 0 and x["buy_days"] >= 3]) >= 1:
        score += 2
    for key, item10 in agg10.items():
        item5 = agg5.get(key)
        if item10["net"] > 0 and item5 and item5["net"] > 0:
            score += 4
            break
    if top_sell and top_sell[0]["net"] < 0 and top_sell[0]["sell_days"] >= 2:
        score -= 6
    if top_buy and top_buy[0]["net"] > 0 and agg5.get((top_buy[0]["broker_name"], top_buy[0]["branch_name"]), {}).get("net", 0) <= 0:
        score -= 3
    return max(-20, min(20, score))


def _status(score5: int, score10: int, has_data: bool) -> str:
    if not has_data:
        return NO_DATA_STATUS
    if score5 >= 10 and score10 >= 8:
        return "強勢累積"
    if score5 >= 5:
        return "偏多"
    if score5 >= 1:
        return "小幅偏多"
    if score5 >= -4:
        return "中性"
    if score5 >= -9:
        return "籌碼轉弱"
    return "分點賣壓"


def _rank_rows(items: list[dict[str, Any]], total_volume_5d: float, volume_unit: str, reverse: bool) -> list[dict[str, Any]]:
    ordered = sorted(items, key=lambda x: x["net"], reverse=reverse)[:5]
    out = []
    for idx, item in enumerate(ordered, 1):
        net = int(item["net"])
        ratio = round(_volume_ratio_pct(net, total_volume_5d, volume_unit), 2)
        out.append(
            {
                "rank": idx,
                "broker_name": item["broker_name"],
                "branch_name": item["branch_name"],
                "display_name": item["display_name"],
                "net_5d": net,
                "volume_ratio_5d": ratio,
                "judgement": "集中買超" if net > 0 and ratio >= 3 else "偏買" if net > 0 else "集中賣超" if ratio >= 3 else "偏賣",
            }
        )
    return out


def _warnings(agg5: dict[tuple[str, str], dict[str, Any]], total_volume_5d: float, volume_unit: str) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    if not agg5:
        warnings.append({"type": "no_data", "level": "info", "message": "目前沒有可用的分點資料。"})
        return warnings
    sellers = sorted([x for x in agg5.values() if x["net"] < 0], key=lambda x: x["net"])
    if sellers:
        worst = sellers[0]
        ratio = _volume_ratio_pct(int(worst["net"]), total_volume_5d, volume_unit)
        if worst["sell_days"] >= 3 or ratio >= 5:
            warnings.append(
                {
                    "type": "sell_pressure",
                    "level": "warning",
                    "message": f"{worst['display_name']} 近 5 日賣超 {abs(int(worst['net'])):,} 股，留意分點賣壓。",
                }
            )
    buyers = sorted([x for x in agg5.values() if x["net"] > 0], key=lambda x: x["net"], reverse=True)
    if buyers and total_volume_5d:
        ratio = _volume_ratio_pct(int(buyers[0]["net"]), total_volume_5d, volume_unit)
        if ratio >= 6:
            warnings.append(
                {
                    "type": "concentration",
                    "level": "info",
                    "message": f"{buyers[0]['display_name']} 近 5 日買超占成交量 {ratio:.1f}%，籌碼集中度偏高。",
                }
            )
    return warnings


def generate_broker_conclusion(summary: dict[str, Any], warnings: list[dict[str, str]]) -> str:
    if not summary.get("available"):
        return "目前沒有分點資料，暫不判斷關鍵分點狀態。"
    status = summary.get("broker_status", NO_DATA_STATUS)
    score5 = summary.get("broker_score_5d", 0)
    score10 = summary.get("broker_score_10d", 0)
    brokers = summary.get("main_key_brokers") or []
    broker_text = "、".join(brokers[:3]) if brokers else "尚無明顯主導分點"
    warn_text = f"；{warnings[0]['message']}" if warnings else ""
    return f"分點狀態為 {status}，5D 分數 {score5}、10D 分數 {score10}，主要觀察 {broker_text}{warn_text}"


def calculate_key_broker_stats(
    rows_5d: list[dict[str, Any]],
    rows_10d: list[dict[str, Any]],
    daily_kbars: list[dict[str, Any]],
    available_days_5d: int = 0,
    available_days_10d: int = 0,
) -> dict[str, Any]:
    agg5 = _aggregate(rows_5d)
    agg10 = _aggregate(rows_10d)
    vol5 = _volume_sum_for_broker_dates(daily_kbars, rows_5d, 5)
    volume_unit = _detect_volume_unit(daily_kbars, rows_5d)
    has_data = bool(rows_5d or rows_10d)
    score5 = _score_5d(agg5, vol5, volume_unit)
    score10 = _score_10d(agg10, agg5)
    status = _status(score5, score10, has_data)

    key_items = sorted(agg10.values() or agg5.values(), key=lambda x: abs(x["net"]), reverse=True)[:5]
    key_brokers = []
    for item in key_items:
        key = (item["broker_name"], item["branch_name"])
        item5 = agg5.get(key, {"net": 0, "buy_days": 0, "sell_days": 0})
        net5 = int(item5.get("net", 0))
        net10 = int(item.get("net", 0))
        buy_days = int(item5.get("buy_days", 0))
        sell_days = int(item5.get("sell_days", 0))
        key_brokers.append(
            {
                "broker_name": item["broker_name"],
                "branch_name": item["branch_name"],
                "display_name": item["display_name"],
                "net_5d": net5,
                "net_10d": net10,
                "buy_days_5d": buy_days,
                "latest_action": classify_latest_action(net5, net10, buy_days, sell_days),
                "broker_type": classify_broker_type(net5, net10, buy_days, sell_days),
                "judgement": "偏多" if net5 > 0 else "偏空" if net5 < 0 else "中性",
            }
        )

    buy_items = [x for x in agg5.values() if x["net"] > 0]
    sell_items = [x for x in agg5.values() if x["net"] < 0]
    top_buy = _rank_rows(buy_items, vol5, volume_unit, True)
    top_sell = _rank_rows(sell_items, vol5, volume_unit, False)
    warnings = _warnings(agg5, vol5, volume_unit)
    main_warning = next((w["message"] for w in warnings if w.get("level") == "warning"), "")
    summary = {
        "available": has_data,
        "available_days_5d": available_days_5d,
        "available_days_10d": available_days_10d,
        "data_completeness_warning": _data_completeness_warning(available_days_5d, available_days_10d) if has_data else "",
        "volume_unit": volume_unit,
        "broker_status": status,
        "broker_score_5d": score5,
        "broker_score_10d": score10,
        "main_key_brokers": [x["display_name"] for x in key_brokers[:3]],
        "main_warning": main_warning,
    }
    summary["conclusion"] = generate_broker_conclusion(summary, warnings)
    return {
        "summary": summary,
        "key_brokers": key_brokers,
        "top_buy_brokers_5d": top_buy,
        "top_sell_brokers_5d": top_sell,
        "warnings": warnings,
    }


def analyze_key_brokers(conn: sqlite3.Connection, query: str, as_of_date: str | None = None) -> dict[str, Any]:
    ensure_broker_tables(conn)
    stock = resolve_stock_query(conn, query)
    if not stock:
        return {"status": "error", "message": "找不到股票代號或名稱", "query": query}

    code = stock["code"]
    data_date = _latest_broker_date(conn, code, as_of_date)
    rows_5d = get_broker_rows(conn, code, data_date, 5) if data_date else []
    rows_10d = get_broker_rows(conn, code, data_date, 10) if data_date else []
    daily_kbars = _get_daily_kbars(conn, code, data_date, 10) if data_date else _get_daily_kbars(conn, code, None, 10)
    available_days_5d = len(_broker_dates_in_recent_trading_days(conn, code, data_date, 5)) if data_date else 0
    available_days_10d = len(_broker_dates_in_recent_trading_days(conn, code, data_date, 10)) if data_date else 0
    stats = calculate_key_broker_stats(rows_5d, rows_10d, daily_kbars, available_days_5d, available_days_10d)
    available = bool(rows_5d or rows_10d)

    if not available:
        stats["summary"].update(
            {
                "available": False,
                "available_days_5d": available_days_5d,
                "available_days_10d": available_days_10d,
                "data_completeness_warning": "",
                "broker_status": NO_DATA_STATUS,
                "broker_score_5d": 0,
                "broker_score_10d": 0,
                "main_key_brokers": [],
                "main_warning": "目前沒有此股票的分點資料。",
                "conclusion": "目前沒有分點資料，暫不判斷關鍵分點狀態。",
            }
        )
        stats["warnings"] = [
            {"type": "no_data", "level": "info", "message": "目前沒有此股票的分點資料。"}
        ]

    return {
        "status": "success",
        "query": query,
        "stock": stock,
        "data_date": data_date,
        "summary": stats["summary"],
        "key_brokers": stats["key_brokers"],
        "top_buy_brokers_5d": stats["top_buy_brokers_5d"],
        "top_sell_brokers_5d": stats["top_sell_brokers_5d"],
        "warnings": stats["warnings"],
        "available": available,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
