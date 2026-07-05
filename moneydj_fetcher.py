"""
moneydj_fetcher.py

On-demand MoneyDJ Fubon broker period summary fetcher for one stock.
This module only stores period summary rows and does not feed strategy scores.
"""

from __future__ import annotations

import re
import sqlite3
import ssl
import time
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from typing import Any


SOURCE = "moneydj_fubon"
BASE_URL = "https://fubon-ebrokerdj.fbs.com.tw/z/zc/zco"
PERIOD_TO_D = {"1D": "1", "5D": "2", "10D": "3", "20D": "4"}
PERIOD_TO_DAYS = {"1D": 1, "5D": 5, "10D": 10, "20D": 20}

_SSL_CTX = ssl.create_default_context()
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.9",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_stack = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table_stack += 1
            if self._table_stack == 1:
                self._current_table = []
        elif tag == "tr" and self._current_table is not None:
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("td", "th") and self._current_cell is not None and self._current_row is not None:
            text = "".join(self._current_cell)
            text = re.sub(r"\s+", " ", text).strip()
            self._current_row.append(text)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(c for c in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_stack > 0:
            self._table_stack -= 1
            if self._table_stack == 0 and self._current_table is not None:
                if self._current_table:
                    self.tables.append(self._current_table)
                self._current_table = None


def _normalize_period(period_label: str) -> str:
    period = str(period_label or "5D").upper().strip()
    aliases = {"1": "1D", "5": "5D", "10": "10D", "20": "20D"}
    period = aliases.get(period, period)
    if period not in PERIOD_TO_D:
        raise ValueError("Unsupported MoneyDJ period. Supported: 1D, 5D, 10D, 20D")
    return period


def build_moneydj_url(code: str, period_label: str) -> str:
    period = _normalize_period(period_label)
    d_value = PERIOD_TO_D[period]
    return f"{BASE_URL}/zco_{str(code).strip()}_{d_value}.djhtm"


def _decode_response(raw: bytes, content_type: str) -> tuple[str, str]:
    encodings: list[str] = []
    m = re.search(r"charset=([^;]+)", content_type or "", re.I)
    if m:
        encodings.append(m.group(1).strip())
    encodings.extend(["big5", "cp950", "utf-8-sig", "utf-8"])
    seen = set()
    for enc in encodings:
        enc_l = enc.lower()
        if enc_l in seen:
            continue
        seen.add(enc_l)
        try:
            return raw.decode(enc), enc
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def _request_moneydj(url: str, timeout: int = 15) -> tuple[str, dict[str, Any]]:
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
        content_type = resp.headers.get("content-type", "")
        status = getattr(resp, "status", 200)
    html, encoding = _decode_response(raw, content_type)
    return html, {"http_status": status, "content_type": content_type, "encoding": encoding}


def _html_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def _moneydj_date(value: str) -> str | None:
    s = str(value or "").strip()
    m = re.search(r"(\d{4})/(\d{1,2})/(\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _extract_end_date(html: str) -> str | None:
    m = re.search(r"最後更新日[:：]\s*([0-9/]+)", html or "")
    return _moneydj_date(m.group(1)) if m else None


def _extract_stock_name(html: str, code: str) -> str:
    m = re.search(rf"<tr[^>]*>\s*<td[^>]*>\s*([^<\s(]+)\({re.escape(str(code))}\)\s*券商分點-進出明細", html or "", re.I)
    if m:
        return m.group(1).strip()
    title = _html_title(html)
    m = re.search(rf"([^-\s]+)-?{re.escape(str(code))}", title)
    return m.group(1).strip() if m else ""


def _to_int(value: Any) -> int:
    s = str(value or "").strip().replace(",", "")
    s = re.sub(r"[^0-9\-]", "", s)
    if s in ("", "-"):
        return 0
    try:
        return int(s)
    except Exception:
        return 0


def _to_float(value: Any) -> float:
    s = str(value or "").strip().replace(",", "").replace("%", "")
    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _parse_data_row(row: list[str], offset: int, side: str, code: str, stock_name: str, period: str, end_date: str | None) -> dict[str, Any] | None:
    if len(row) < offset + 5:
        return None
    broker_name = str(row[offset] or "").strip()
    if not broker_name or "券商" in broker_name or "合計" in broker_name or "平均" in broker_name or broker_name.startswith("【"):
        return None
    buy_lots = _to_int(row[offset + 1])
    sell_lots = _to_int(row[offset + 2])
    net_lots = _to_int(row[offset + 3])
    volume_ratio = _to_float(row[offset + 4])
    if side == "sell":
        net_lots = -abs(net_lots)
    return {
        "code": str(code).strip(),
        "stock_name": stock_name,
        "start_date": end_date if period == "1D" else None,
        "end_date": end_date,
        "period_label": period,
        "side": side,
        "broker_name": broker_name,
        "buy_lots": buy_lots,
        "sell_lots": sell_lots,
        "net_lots": net_lots,
        "volume_ratio": volume_ratio,
        "source": SOURCE,
    }


def parse_moneydj_broker_table(html: str, code: str, period_label: str) -> list[dict[str, Any]]:
    period = _normalize_period(period_label)
    if "券商分點-進出明細" not in (html or "") and "券商分點－進出明細" not in (html or ""):
        return []

    parser = _TableParser()
    parser.feed(html or "")
    stock_name = _extract_stock_name(html, code)
    end_date = _extract_end_date(html)
    rows: list[dict[str, Any]] = []

    for table in parser.tables:
        joined = " ".join(" ".join(r) for r in table)
        if "買超券商" not in joined or "賣超券商" not in joined:
            continue
        for row in table:
            if len(row) >= 10:
                buy_row = _parse_data_row(row, 0, "buy", code, stock_name, period, end_date)
                sell_row = _parse_data_row(row, 5, "sell", code, stock_name, period, end_date)
                if buy_row:
                    rows.append(buy_row)
                if sell_row:
                    rows.append(sell_row)
        break
    return rows


def _parse_status(html: str, rows: list[dict[str, Any]]) -> str:
    if rows:
        return "success"
    if "券商分點-進出明細" in (html or "") or "券商分點－進出明細" in (html or ""):
        return "no_table_found"
    return "unexpected_html"


def fetch_moneydj_broker_period(code: str, period_label: str = "5D") -> dict[str, Any]:
    period = _normalize_period(period_label)
    url = build_moneydj_url(code, period)
    trace: dict[str, Any] = {"code": str(code), "period_label": period, "url": url}
    try:
        html, meta = _request_moneydj(url)
        rows = parse_moneydj_broker_table(html, code, period)
        trace.update(meta)
        trace.update({
            "html_title": _html_title(html),
            "parse_status": _parse_status(html, rows),
            "parsed_buy_rows": len([r for r in rows if r.get("side") == "buy"]),
            "parsed_sell_rows": len([r for r in rows if r.get("side") == "sell"]),
            "sample_rows": rows[:4],
            "error_message": "",
        })
        return {"status": "success" if rows else "failed", "rows": rows, "trace": trace}
    except Exception as exc:
        trace.update({
            "http_status": None,
            "content_type": "",
            "html_title": "",
            "encoding": "",
            "parse_status": "fetch_failed",
            "parsed_buy_rows": 0,
            "parsed_sell_rows": 0,
            "sample_rows": [],
            "error_message": str(exc),
        })
        return {"status": "failed", "rows": [], "trace": trace}


def ensure_broker_period_summary_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_period_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL,
            stock_name TEXT,
            start_date TEXT,
            end_date TEXT,
            period_label TEXT NOT NULL,
            side TEXT NOT NULL,
            broker_name TEXT NOT NULL,
            buy_lots INTEGER DEFAULT 0,
            sell_lots INTEGER DEFAULT 0,
            net_lots INTEGER DEFAULT 0,
            volume_ratio REAL DEFAULT 0,
            source TEXT DEFAULT 'moneydj_fubon',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, end_date, period_label, side, broker_name, source)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broker_period_summary_code_period
        ON broker_period_summary(code, period_label, end_date)
        """
    )
    conn.commit()


def _fill_start_dates_from_db(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        if row.get("start_date") or not row.get("end_date"):
            continue
        days = PERIOD_TO_DAYS.get(str(row.get("period_label") or "").upper(), 1)
        conn.row_factory = sqlite3.Row
        dates = conn.execute(
            """
            SELECT DISTINCT date
            FROM daily_kbars
            WHERE code = ? AND date <= ?
            ORDER BY date DESC
            LIMIT ?
            """,
            (row.get("code"), row.get("end_date"), int(days)),
        ).fetchall()
        date_values = [str(d["date"]) for d in dates]
        row["start_date"] = date_values[-1] if date_values else (row.get("end_date") if days == 1 else None)


def upsert_moneydj_period_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_broker_period_summary_table(conn)
    normalized = [r for r in rows if r.get("code") and r.get("end_date") and r.get("broker_name")]
    if not normalized:
        return {"inserted_rows": 0, "updated_rows": 0, "rows": 0}
    _fill_start_dates_from_db(conn, normalized)

    existing_keys = set()
    for r in normalized:
        existing = conn.execute(
            """
            SELECT 1 FROM broker_period_summary
            WHERE code=? AND end_date=? AND period_label=? AND side=? AND broker_name=? AND source=?
            """,
            (r["code"], r["end_date"], r["period_label"], r["side"], r["broker_name"], r.get("source") or SOURCE),
        ).fetchone()
        if existing:
            existing_keys.add((r["code"], r["end_date"], r["period_label"], r["side"], r["broker_name"], r.get("source") or SOURCE))

    conn.executemany(
        """
        INSERT INTO broker_period_summary (
            code, stock_name, start_date, end_date, period_label, side, broker_name,
            buy_lots, sell_lots, net_lots, volume_ratio, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code, end_date, period_label, side, broker_name, source) DO UPDATE SET
            stock_name=excluded.stock_name,
            start_date=excluded.start_date,
            buy_lots=excluded.buy_lots,
            sell_lots=excluded.sell_lots,
            net_lots=excluded.net_lots,
            volume_ratio=excluded.volume_ratio,
            updated_at=CURRENT_TIMESTAMP
        """,
        [
            (
                r["code"], r.get("stock_name"), r.get("start_date"), r.get("end_date"), r.get("period_label"),
                r.get("side"), r.get("broker_name"), int(r.get("buy_lots") or 0), int(r.get("sell_lots") or 0),
                int(r.get("net_lots") or 0), float(r.get("volume_ratio") or 0), r.get("source") or SOURCE,
            )
            for r in normalized
        ],
    )
    conn.commit()
    unique_keys = {(r["code"], r["end_date"], r["period_label"], r["side"], r["broker_name"], r.get("source") or SOURCE) for r in normalized}
    updated_rows = len(existing_keys)
    inserted_rows = len(unique_keys) - updated_rows
    return {"inserted_rows": inserted_rows, "updated_rows": updated_rows, "rows": len(normalized)}


def fetch_and_store_moneydj_period(conn: sqlite3.Connection, code: str, period_label: str = "5D") -> dict[str, Any]:
    result = fetch_moneydj_broker_period(code, period_label)
    rows = result.get("rows") or []
    trace = result.get("trace") or {}
    if not rows:
        return {
            "status": "failed",
            "message": trace.get("error_message") or trace.get("parse_status") or "MoneyDJ parse failed",
            "parsed_rows": 0,
            "inserted_rows": 0,
            "updated_rows": 0,
            "trace": trace,
        }
    upsert = upsert_moneydj_period_rows(conn, rows)
    return {
        "status": "success",
        "message": f"MoneyDJ {trace.get('period_label', period_label)} parsed and stored.",
        "parsed_rows": len(rows),
        "inserted_rows": upsert.get("inserted_rows", 0),
        "updated_rows": upsert.get("updated_rows", 0),
        "trace": trace,
    }


def _has_moneydj_period_for_date(conn: sqlite3.Connection, code: str, period_label: str, data_date: str | None) -> bool:
    if not data_date:
        return False
    ensure_broker_period_summary_table(conn)
    row = conn.execute(
        """
        SELECT 1
        FROM broker_period_summary
        WHERE code=? AND period_label=? AND end_date=? AND source=?
        LIMIT 1
        """,
        (str(code), period_label, str(data_date), SOURCE),
    ).fetchone()
    return row is not None


def sync_moneydj_periods_for_codes(
    conn: sqlite3.Connection,
    codes,
    period_label: str = "5D",
    max_codes: int = 30,
    sleep_sec: float = 1.0,
    skip_existing: bool = True,
    data_date: str | None = None,
) -> dict[str, Any]:
    period = _normalize_period(period_label)
    seen: set[str] = set()
    unique_codes: list[str] = []
    for code in codes or []:
        code_str = str(code or "").strip()
        if not code_str or code_str in seen:
            continue
        seen.add(code_str)
        unique_codes.append(code_str)

    try:
        limit = max(0, int(max_codes))
    except Exception:
        limit = 30
    limited_codes = unique_codes[:limit] if limit else []

    summary: dict[str, Any] = {
        "requested_count": len(unique_codes),
        "limited_count": len(limited_codes),
        "fetched_count": 0,
        "skipped_count": 0,
        "failed_count": 0,
        "fetched_codes": [],
        "skipped_codes": [],
        "failed_items": [],
        "period_label": period,
        "data_date": data_date,
    }

    ensure_broker_period_summary_table(conn)

    for idx, code in enumerate(limited_codes):
        try:
            if skip_existing and _has_moneydj_period_for_date(conn, code, period, data_date):
                summary["skipped_count"] += 1
                summary["skipped_codes"].append(code)
            else:
                result = fetch_and_store_moneydj_period(conn, code, period)
                if result.get("status") == "success":
                    summary["fetched_count"] += 1
                    summary["fetched_codes"].append(code)
                else:
                    summary["failed_count"] += 1
                    trace = result.get("trace") or {}
                    summary["failed_items"].append({
                        "code": code,
                        "message": result.get("message") or "MoneyDJ fetch failed",
                        "parse_status": trace.get("parse_status"),
                        "http_status": trace.get("http_status"),
                    })
        except Exception as exc:
            summary["failed_count"] += 1
            summary["failed_items"].append({
                "code": code,
                "message": str(exc),
                "parse_status": "exception",
                "http_status": None,
            })
        if idx < len(limited_codes) - 1 and sleep_sec:
            try:
                delay = max(0.0, float(sleep_sec))
            except Exception:
                delay = 1.0
            if delay > 0:
                time.sleep(delay)

    return summary


def trace_moneydj_fetch(code: str, period_label: str = "5D") -> dict[str, Any]:
    result = fetch_moneydj_broker_period(code, period_label)
    return result.get("trace") or {}


def _period_text(period_label: str) -> str:
    return {
        "1D": "近 1 日",
        "5D": "近 5 日",
        "10D": "近 10 日",
        "20D": "近 20 日",
    }.get(str(period_label or "").upper(), str(period_label or "區間"))


def _fmt_lots(value: Any) -> str:
    try:
        return f"{abs(int(value or 0)):,}"
    except Exception:
        return "0"


def _fmt_ratio(value: Any) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except Exception:
        return "0.00"


def _build_period_chip_summary(buy_rows: list[dict[str, Any]], sell_rows: list[dict[str, Any]], period_label: str) -> dict[str, str]:
    top_buy = buy_rows[0] if buy_rows else {}
    top_sell = sell_rows[0] if sell_rows else {}
    top_buy_ratio = float(top_buy.get("volume_ratio") or 0)
    top_sell_ratio = float(top_sell.get("volume_ratio") or 0)

    if top_sell_ratio >= top_buy_ratio * 2 and top_sell_ratio >= 5:
        status = "區間賣壓集中"
    elif top_buy_ratio >= top_sell_ratio * 2 and top_buy_ratio >= 5:
        status = "區間買盤集中"
    elif top_buy_ratio >= 3 and top_sell_ratio >= 3:
        status = "多空分歧 / 換手明顯"
    else:
        status = "區間中性"

    period_text = _period_text(period_label)
    buy_name = top_buy.get("broker_name") or "無"
    sell_name = top_sell.get("broker_name") or "無"

    if not top_buy and not top_sell:
        reason = f"{period_text}尚無 MoneyDJ 區間分點資料，暫列為區間中性。"
    elif status == "區間賣壓集中":
        reason = (
            f"{period_text}賣超第一名{sell_name}賣超 {_fmt_lots(top_sell.get('net_lots'))} 張，"
            f"占成交量 {_fmt_ratio(top_sell_ratio)}%，明顯高於買超第一名{buy_name} "
            f"{_fmt_ratio(top_buy_ratio)}%，區間賣壓集中。"
        )
    elif status == "區間買盤集中":
        reason = (
            f"{period_text}買超第一名{buy_name}買超 {_fmt_lots(top_buy.get('net_lots'))} 張，"
            f"占成交量 {_fmt_ratio(top_buy_ratio)}%，明顯高於賣超第一名{sell_name} "
            f"{_fmt_ratio(top_sell_ratio)}%，區間買盤集中。"
        )
    elif status == "多空分歧 / 換手明顯":
        reason = (
            f"{period_text}買超第一名{buy_name}買超 {_fmt_lots(top_buy.get('net_lots'))} 張，"
            f"占成交量 {_fmt_ratio(top_buy_ratio)}%，賣超第一名{sell_name}賣超 "
            f"{_fmt_lots(top_sell.get('net_lots'))} 張，占成交量 {_fmt_ratio(top_sell_ratio)}%，"
            f"買賣雙方力量接近，屬於多空分歧 / 換手明顯。"
        )
    else:
        reason = (
            f"{period_text}買超第一名{buy_name}占成交量 {_fmt_ratio(top_buy_ratio)}%，"
            f"賣超第一名{sell_name}占成交量 {_fmt_ratio(top_sell_ratio)}%，"
            f"集中度未達明顯偏多或偏空門檻，暫列為區間中性。"
        )

    return {"period_chip_status": status, "period_chip_reason": reason}


def get_moneydj_period_summary(conn: sqlite3.Connection, code: str, period_label: str = "5D") -> dict[str, Any]:
    ensure_broker_period_summary_table(conn)
    period = _normalize_period(period_label)
    conn.row_factory = sqlite3.Row
    latest = conn.execute(
        """
        SELECT MAX(end_date) AS end_date
        FROM broker_period_summary
        WHERE code=? AND period_label=? AND source=?
        """,
        (str(code), period, SOURCE),
    ).fetchone()
    end_date = latest["end_date"] if latest and latest["end_date"] else None
    if not end_date:
        summary = _build_period_chip_summary([], [], period)
        return {
            "status": "empty",
            "code": str(code),
            "period_label": period,
            "rows": [],
            "buy_rows": [],
            "sell_rows": [],
            **summary,
        }
    db_rows = conn.execute(
        """
        SELECT code, stock_name, start_date, end_date, period_label, side, broker_name,
               buy_lots, sell_lots, net_lots, volume_ratio, source, updated_at
        FROM broker_period_summary
        WHERE code=? AND period_label=? AND end_date=? AND source=?
        ORDER BY side, ABS(net_lots) DESC, broker_name
        """,
        (str(code), period, end_date, SOURCE),
    ).fetchall()
    rows = [dict(r) for r in db_rows]
    buy_rows = [r for r in rows if r.get("side") == "buy"]
    sell_rows = [r for r in rows if r.get("side") == "sell"]
    summary = _build_period_chip_summary(buy_rows, sell_rows, period)
    return {
        "status": "success",
        "code": str(code),
        "period_label": period,
        "start_date": rows[0].get("start_date") if rows else None,
        "end_date": end_date,
        "rows": rows,
        "buy_rows": buy_rows,
        "sell_rows": sell_rows,
        **summary,
    }
