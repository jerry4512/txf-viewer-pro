"""
broker_fetcher.py

On-demand official-source broker trading fetcher for one stock.

Official sources attempted:
- TWSE broker report system: https://bsr.twse.com.tw/bshtm/bsMenu.aspx
- TPEx broker trading report system: https://www.tpex.org.tw/web/stock/aftertrading/broker_trading/brokerBS.php

Both official services may require validation or change response format.  This
module is deliberately defensive: failures are returned as status dictionaries
and must not break the broker analysis API.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import ssl
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

import broker_analysis

TWSE_MENU_URL = "https://bsr.twse.com.tw/bshtm/bsMenu.aspx"
TWSE_CONTENT_URL = "https://bsr.twse.com.tw/bshtm/bsContent.aspx"
TPEX_PAGE_URL = "https://www.tpex.org.tw/web/stock/aftertrading/broker_trading/brokerBS.php"
TPEX_CANDIDATE_URLS = [
    "https://www.tpex.org.tw/web/stock/aftertrading/broker_trading/brokerBS.php",
    "https://www.tpex.org.tw/www/zh-tw/brokerTrading",
]

_SSL_CTX = ssl.create_default_context()
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml,text/csv,application/json,*/*;q=0.9",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
}


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "table":
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
            text = "".join(self._current_cell).strip()
            text = re.sub(r"\s+", " ", text)
            self._current_row.append(text)
            self._current_cell = None
        elif tag == "tr" and self._current_row is not None and self._current_table is not None:
            if any(c for c in self._current_row):
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._current_table is not None:
            if self._current_table:
                self.tables.append(self._current_table)
            self._current_table = None


def _request(url: str, *, data: dict[str, Any] | None = None, timeout: int = 12, referer: str | None = None) -> tuple[str, str]:
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        raw = resp.read()
        content_type = resp.headers.get("content-type", "")
    encoding = "utf-8"
    m = re.search(r"charset=([^;]+)", content_type, re.I)
    if m:
        encoding = m.group(1).strip()
    for enc in (encoding, "utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(enc), content_type
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), content_type


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    s = str(value).strip().replace(",", "")
    s = re.sub(r"[^0-9\-]", "", s)
    if s in ("", "-"):
        return 0
    try:
        return int(s)
    except Exception:
        return 0


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace(",", "")
    s = re.sub(r"[^0-9\.\-]", "", s)
    if s in ("", "-", "."):
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def _yyyymmdd(date: str) -> str:
    return str(date or "").replace("-", "")


def _hyphen_date(date: str) -> str:
    s = _yyyymmdd(date)
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return date


def _roc_date(date: str) -> str:
    s = _yyyymmdd(date)
    if len(s) != 8:
        return date
    return f"{int(s[:4]) - 1911}/{s[4:6]}/{s[6:8]}"



_MARKET_DETECT_CACHE: dict[str, str] = {}


def _detect_market_from_official_quotes(code: str) -> str:
    if code in _MARKET_DETECT_CACHE:
        return _MARKET_DETECT_CACHE[code]

    # TWSE listed daily quote list used elsewhere in this project.  The service
    # may return CSV even when response=json is supplied, so support both.
    try:
        text, _ = _request("https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY_ALL?response=json", timeout=10)
        try:
            payload = json.loads(text)
            data_rows = payload.get("data", []) if isinstance(payload, dict) else []
            for row in data_rows:
                if row and str(row[0]).strip() == code:
                    _MARKET_DETECT_CACHE[code] = "twse"
                    return "twse"
        except Exception:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, [])
            code_idx = 1 if header and "證券代號" in header[1] else 0
            for row in reader:
                if len(row) > code_idx and str(row[code_idx]).strip() == code:
                    _MARKET_DETECT_CACHE[code] = "twse"
                    return "twse"
    except Exception:
        pass

    # TPEx official open data quote list used elsewhere in this project.
    try:
        text, _ = _request("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_quotes", timeout=10)
        payload = json.loads(text)
        if isinstance(payload, list):
            for row in payload:
                if str((row or {}).get("SecuritiesCompanyCode", "")).strip() == code:
                    _MARKET_DETECT_CACHE[code] = "tpex"
                    return "tpex"
    except Exception:
        pass

    _MARKET_DETECT_CACHE[code] = "unknown"
    return "unknown"

def get_recent_trading_dates(conn: sqlite3.Connection, code: str, days: int = 10) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT DISTINCT date
        FROM daily_kbars
        WHERE code = ?
        ORDER BY date DESC
        LIMIT ?
        """,
        (code, int(days)),
    ).fetchall()
    return [str(r["date"]) for r in rows]


def detect_market_type(conn: sqlite3.Connection, code: str) -> str:
    """Return twse, tpex, or unknown without guessing aggressively."""
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("PRAGMA table_info(stock_names)").fetchall()
        cols = {r[1] for r in row}
        if "market" in cols:
            r = conn.execute("SELECT market FROM stock_names WHERE code=? LIMIT 1", (code,)).fetchone()
            market = str(r["market"] or "").lower() if r else ""
            if market in ("twse", "tse", "上市"):
                return "twse"
            if market in ("tpex", "otc", "上櫃"):
                return "tpex"
    except Exception:
        pass

    try:
        row = conn.execute("SELECT category FROM stock_names WHERE code=? LIMIT 1", (code,)).fetchone()
        category = str(row["category"] or "") if row else ""
        if "上櫃" in category or "櫃" in category or "OTC" in category.upper():
            return "tpex"
        if "上市" in category or "TSE" in category.upper():
            return "twse"
    except Exception:
        pass

    return _detect_market_from_official_quotes(code)

def _parse_twse_tables(html: str, code: str, date: str) -> list[dict[str, Any]]:
    parser = _TableParser()
    parser.feed(html)
    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        for cells in table:
            if len(cells) < 4:
                continue
            joined = " ".join(cells)
            if "券商" in joined and ("買進" in joined or "賣出" in joined):
                continue
            # TWSE reports often show left and right broker blocks in one row.
            for offset in (0, 5):
                block = cells[offset:offset + 5]
                if len(block) < 4:
                    continue
                name = block[0].strip()
                if not name or name in ("券商", "證券商"):
                    continue
                buy = _to_int(block[1] if len(block) > 1 else 0)
                sell = _to_int(block[2] if len(block) > 2 else 0)
                net = _to_int(block[3] if len(block) > 3 else buy - sell)
                if buy == 0 and sell == 0 and net == 0:
                    continue
                rows.append({
                    "code": code,
                    "date": _hyphen_date(date),
                    "broker_name": name,
                    "branch_name": "",
                    "buy_qty": buy,
                    "sell_qty": sell,
                    "net_qty": net if net else buy - sell,
                    "source": "twse",
                })
    return rows


def fetch_twse_broker_daily(code: str, date: str) -> list[dict[str, Any]]:
    """Fetch one TWSE stock/day from the official broker report system."""
    date8 = _yyyymmdd(date)
    # Official ASP.NET page normally relies on a form session.  Try the direct
    # content endpoint first; if validation blocks it this function returns [].
    query_url = f"{TWSE_CONTENT_URL}?StockNo={urllib.parse.quote(code)}&Date={date8}"
    try:
        html, _ = _request(query_url, referer=TWSE_MENU_URL)
        rows = _parse_twse_tables(html, code, date)
        if rows:
            return rows
    except Exception:
        pass

    try:
        html, _ = _request(
            TWSE_MENU_URL,
            data={"TextBox_Stkno": code, "TextBox_Date": date8, "btnOK": "查詢"},
            referer=TWSE_MENU_URL,
        )
        return _parse_twse_tables(html, code, date)
    except Exception:
        return []


def _rows_from_json_payload(payload: Any, code: str, date: str, source: str) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "aaData", "tables"):
            val = payload.get(key)
            if isinstance(val, list):
                if key == "tables":
                    for table in val:
                        if isinstance(table, dict) and isinstance(table.get("data"), list):
                            candidates.extend(table["data"])
                else:
                    candidates.extend(val)
    elif isinstance(payload, list):
        candidates = payload

    rows = []
    for item in candidates:
        if isinstance(item, dict):
            broker = item.get("broker_name") or item.get("券商") or item.get("證券商") or item.get("name") or ""
            branch = item.get("branch_name") or item.get("分公司") or item.get("branch") or ""
            buy = _to_int(item.get("buy_qty") or item.get("買進股數") or item.get("買進") or item.get("buy"))
            sell = _to_int(item.get("sell_qty") or item.get("賣出股數") or item.get("賣出") or item.get("sell"))
            net = _to_int(item.get("net_qty") or item.get("買賣超") or item.get("差額") or buy - sell)
        elif isinstance(item, list):
            if len(item) < 4:
                continue
            broker = str(item[0]).strip()
            branch = ""
            buy = _to_int(item[1])
            sell = _to_int(item[2])
            net = _to_int(item[3]) or buy - sell
        else:
            continue
        if not broker or (buy == 0 and sell == 0 and net == 0):
            continue
        rows.append({
            "code": code,
            "date": _hyphen_date(date),
            "broker_name": str(broker).strip(),
            "branch_name": str(branch).strip(),
            "buy_qty": buy,
            "sell_qty": sell,
            "net_qty": net,
            "source": source,
        })
    return rows


def _parse_html_rows(html: str, code: str, date: str, source: str) -> list[dict[str, Any]]:
    parser = _TableParser()
    parser.feed(html)
    rows: list[dict[str, Any]] = []
    for table in parser.tables:
        for cells in table:
            if len(cells) < 4:
                continue
            joined = " ".join(cells)
            if "券商" in joined and ("買" in joined or "賣" in joined):
                continue
            broker = cells[0].strip()
            if not broker:
                continue
            buy = _to_int(cells[1])
            sell = _to_int(cells[2])
            net = _to_int(cells[3]) or buy - sell
            if buy == 0 and sell == 0 and net == 0:
                continue
            rows.append({
                "code": code,
                "date": _hyphen_date(date),
                "broker_name": broker,
                "branch_name": "",
                "buy_qty": buy,
                "sell_qty": sell,
                "net_qty": net,
                "source": source,
            })
    return rows


def fetch_tpex_broker_daily(code: str, date: str) -> list[dict[str, Any]]:
    """Fetch one TPEx stock/day from official TPEx broker trading endpoints."""
    params_list = [
        {"l": "zh-tw", "o": "json", "stk_code": code, "d": _roc_date(date)},
        {"response": "json", "code": code, "date": _yyyymmdd(date)},
        {"code": code, "date": _yyyymmdd(date)},
    ]
    for base in TPEX_CANDIDATE_URLS:
        for params in params_list:
            url = base + "?" + urllib.parse.urlencode(params)
            try:
                text, content_type = _request(url, referer=TPEX_PAGE_URL)
                if "json" in content_type.lower() or text.lstrip().startswith(("{", "[")):
                    try:
                        rows = _rows_from_json_payload(json.loads(text), code, date, "tpex")
                        if rows:
                            return rows
                    except Exception:
                        pass
                rows = _parse_html_rows(text, code, date, "tpex")
                if rows:
                    return rows
            except Exception:
                continue
    return []


def normalize_fetched_broker_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for r in rows or []:
        code = str(r.get("code") or "").strip()
        date = _hyphen_date(str(r.get("date") or "").strip())
        broker_name = str(r.get("broker_name") or "").strip()
        branch_name = str(r.get("branch_name") or "").strip()
        buy_qty = _to_int(r.get("buy_qty"))
        sell_qty = _to_int(r.get("sell_qty"))
        net_qty = _to_int(r.get("net_qty")) or buy_qty - sell_qty
        if not code or not date or not broker_name:
            continue
        normalized.append({
            "code": code,
            "date": date,
            "broker_name": broker_name,
            "branch_name": branch_name or "-",
            "buy_qty": buy_qty,
            "sell_qty": sell_qty,
            "net_qty": net_qty,
            "buy_amount": _to_float(r.get("buy_amount")),
            "sell_amount": _to_float(r.get("sell_amount")),
            "net_amount": _to_float(r.get("net_amount")),
            "source": str(r.get("source") or "official").strip(),
        })
    return normalized


def upsert_fetched_broker_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    broker_analysis.ensure_broker_tables(conn)
    normalized = normalize_fetched_broker_rows(rows)
    if not normalized:
        return {"inserted_or_updated": 0, "rows": 0, "inserted_rows": 0, "updated_rows": 0, "dates": []}

    existing_keys = set()
    for r in normalized:
        hit = conn.execute(
            """
            SELECT 1 FROM broker_trading_daily
            WHERE code=? AND date=? AND broker_name=? AND branch_name=?
            LIMIT 1
            """,
            (r["code"], r["date"], r["broker_name"], r["branch_name"]),
        ).fetchone()
        if hit:
            existing_keys.add((r["code"], r["date"], r["broker_name"], r["branch_name"]))

    conn.executemany(
        """
        INSERT INTO broker_trading_daily (
            code, date, broker_name, branch_name, buy_qty, sell_qty, net_qty,
            buy_amount, sell_amount, net_amount, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code, date, broker_name, branch_name) DO UPDATE SET
            buy_qty=excluded.buy_qty,
            sell_qty=excluded.sell_qty,
            net_qty=excluded.net_qty,
            buy_amount=excluded.buy_amount,
            sell_amount=excluded.sell_amount,
            net_amount=excluded.net_amount,
            source=excluded.source,
            updated_at=CURRENT_TIMESTAMP
        """,
        [
            (
                r["code"], r["date"], r["broker_name"], r["branch_name"],
                r["buy_qty"], r["sell_qty"], r["net_qty"],
                r["buy_amount"], r["sell_amount"], r["net_amount"], r["source"],
            )
            for r in normalized
        ],
    )
    conn.commit()
    unique_keys = {(r["code"], r["date"], r["broker_name"], r["branch_name"]) for r in normalized}
    updated_rows = len(unique_keys & existing_keys)
    inserted_rows = len(unique_keys) - updated_rows
    return {
        "inserted_or_updated": len(normalized),
        "rows": len(normalized),
        "inserted_rows": inserted_rows,
        "updated_rows": updated_rows,
        "dates": sorted({r["date"] for r in normalized}, reverse=True),
    }



def _request_trace(url: str, *, data: dict[str, Any] | None = None, timeout: int = 12, referer: str | None = None) -> tuple[str, dict[str, Any]]:
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    body = None
    method = "GET"
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        method = "POST"
    req = urllib.request.Request(url, data=body, headers=headers)
    meta = {
        "url": url,
        "method": method,
        "http_status": None,
        "final_url": None,
        "redirected": False,
        "content_type": "",
        "content_length": 0,
        "error_message": "",
    }
    raw = b""
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
            raw = resp.read()
            meta["http_status"] = int(getattr(resp, "status", 200) or 200)
            meta["final_url"] = resp.geturl()
            meta["redirected"] = bool(meta["final_url"] and meta["final_url"] != url)
            meta["content_type"] = resp.headers.get("content-type", "")
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        meta["http_status"] = int(exc.code)
        meta["final_url"] = exc.geturl()
        meta["redirected"] = bool(meta["final_url"] and meta["final_url"] != url)
        meta["content_type"] = exc.headers.get("content-type", "") if exc.headers else ""
        meta["error_message"] = str(exc)
    except Exception as exc:
        meta["error_message"] = str(exc)
        return "", meta

    meta["content_length"] = len(raw)
    encoding = "utf-8"
    m = re.search(r"charset=([^;]+)", meta["content_type"], re.I)
    if m:
        encoding = m.group(1).strip()
    for enc in (encoding, "utf-8-sig", "big5", "cp950"):
        try:
            return raw.decode(enc), meta
        except Exception:
            continue
    return raw.decode("utf-8", errors="replace"), meta


def _classify_parse_status(text: str, rows: list[dict[str, Any]], content_type: str, parse_error: str = "") -> str:
    if parse_error:
        return "parse_error"
    stripped = (text or "").strip()
    if not stripped:
        return "empty_response"
    lower = stripped.lower()
    validation_terms = ["captcha", "驗證", "驗證碼", "recaptcha", "請輸入", "viewstate"]
    if any(term.lower() in lower for term in validation_terms):
        return "validation_or_form_page" if not rows else "parsed_with_validation_markers"
    if rows:
        return "ok"
    if "html" in (content_type or "").lower() or "<html" in lower:
        return "html_parse_no_rows"
    if "json" in (content_type or "").lower() or lower.startswith(("{", "[")):
        return "json_parse_no_rows"
    return "format_unrecognized"


def _trace_attempt(source: str, date: str, url: str, *, parser, method_data: dict[str, Any] | None = None, referer: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    text, meta = _request_trace(url, data=method_data, referer=referer)
    rows: list[dict[str, Any]] = []
    parse_error = ""
    try:
        rows = normalize_fetched_broker_rows(parser(text, meta.get("content_type", "")))
    except Exception as exc:
        parse_error = str(exc)
        rows = []
    attempt = {
        "date": date,
        "source": source,
        "url": url,
        "method": meta.get("method"),
        "http_status": meta.get("http_status"),
        "final_url": meta.get("final_url"),
        "redirected": meta.get("redirected"),
        "content_type": meta.get("content_type"),
        "content_length": meta.get("content_length"),
        "parse_status": _classify_parse_status(text, rows, meta.get("content_type", ""), parse_error),
        "parsed_rows": len(rows),
        "inserted_rows": 0,
        "updated_rows": 0,
        "error_message": parse_error or meta.get("error_message", ""),
    }
    return rows, attempt


def _twse_trace_attempts(code: str, date: str) -> list[tuple[str, dict[str, Any] | None, str]]:
    date8 = _yyyymmdd(date)
    direct_url = f"{TWSE_CONTENT_URL}?StockNo={urllib.parse.quote(code)}&Date={date8}"
    return [
        (direct_url, None, TWSE_MENU_URL),
        (TWSE_MENU_URL, {"TextBox_Stkno": code, "TextBox_Date": date8, "btnOK": "query"}, TWSE_MENU_URL),
    ]


def _parse_twse_for_trace(code: str, date: str):
    return lambda text, content_type: _parse_twse_tables(text, code, date)


def _parse_tpex_for_trace(code: str, date: str):
    def parser(text: str, content_type: str) -> list[dict[str, Any]]:
        if "json" in (content_type or "").lower() or text.lstrip().startswith(("{", "[")):
            try:
                rows = _rows_from_json_payload(json.loads(text), code, date, "tpex")
                if rows:
                    return rows
            except Exception:
                pass
        return _parse_html_rows(text, code, date, "tpex")
    return parser


def trace_broker_fetch_for_stock(conn: sqlite3.Connection, code: str, days: int = 10, write: bool = True) -> dict[str, Any]:
    broker_analysis.ensure_broker_tables(conn)
    stock = broker_analysis.resolve_stock_query(conn, code) or {"code": code, "name": "", "category": ""}
    resolved_code = stock.get("code") or code
    market = detect_market_type(conn, resolved_code)
    recent_dates = get_recent_trading_dates(conn, resolved_code, days)
    attempts: list[dict[str, Any]] = []
    fetched_dates: list[str] = []
    total_inserted = 0
    total_updated = 0

    if market not in ("twse", "tpex"):
        return {
            "code": resolved_code,
            "stock_name": stock.get("name", ""),
            "market_type": market,
            "recent_trading_dates": recent_dates,
            "attempts": [],
            "final_status": "unsupported",
            "final_message": "Unable to determine TWSE/TPEx market; fetch skipped.",
        }

    if not recent_dates:
        return {
            "code": resolved_code,
            "stock_name": stock.get("name", ""),
            "market_type": market,
            "recent_trading_dates": [],
            "attempts": [],
            "final_status": "failed",
            "final_message": "No recent trading dates found in daily_kbars.",
        }

    for date in recent_dates:
        date_rows: list[dict[str, Any]] = []
        if market == "twse":
            for url, data, referer in _twse_trace_attempts(resolved_code, date):
                rows, attempt = _trace_attempt("twse", date, url, parser=_parse_twse_for_trace(resolved_code, date), method_data=data, referer=referer)
                if rows and write:
                    upsert = upsert_fetched_broker_rows(conn, rows)
                    attempt["inserted_rows"] = int(upsert.get("inserted_rows") or 0)
                    attempt["updated_rows"] = int(upsert.get("updated_rows") or 0)
                    total_inserted += attempt["inserted_rows"]
                    total_updated += attempt["updated_rows"]
                attempts.append(attempt)
                if rows:
                    date_rows = rows
                    break
        else:
            params_list = [
                {"l": "zh-tw", "o": "json", "stk_code": resolved_code, "d": _roc_date(date)},
                {"response": "json", "code": resolved_code, "date": _yyyymmdd(date)},
                {"code": resolved_code, "date": _yyyymmdd(date)},
            ]
            stop_date = False
            for base in TPEX_CANDIDATE_URLS:
                for params in params_list:
                    url = base + "?" + urllib.parse.urlencode(params)
                    rows, attempt = _trace_attempt("tpex", date, url, parser=_parse_tpex_for_trace(resolved_code, date), referer=TPEX_PAGE_URL)
                    if rows and write:
                        upsert = upsert_fetched_broker_rows(conn, rows)
                        attempt["inserted_rows"] = int(upsert.get("inserted_rows") or 0)
                        attempt["updated_rows"] = int(upsert.get("updated_rows") or 0)
                        total_inserted += attempt["inserted_rows"]
                        total_updated += attempt["updated_rows"]
                    attempts.append(attempt)
                    if rows:
                        date_rows = rows
                        stop_date = True
                        break
                if stop_date:
                    break
        if date_rows:
            fetched_dates.append(date)

    if fetched_dates and len(fetched_dates) == len(recent_dates):
        final_status = "success"
        final_message = f"Fetched broker rows for {len(fetched_dates)} trading dates."
    elif fetched_dates:
        final_status = "partial"
        final_message = f"Fetched broker rows for {len(fetched_dates)} of {len(recent_dates)} trading dates."
    else:
        final_status = "failed"
        final_message = "Official source returned no parsable broker rows."

    return {
        "code": resolved_code,
        "stock_name": stock.get("name", ""),
        "market_type": market,
        "recent_trading_dates": recent_dates,
        "attempts": attempts,
        "inserted_rows": total_inserted,
        "updated_rows": total_updated,
        "final_status": final_status,
        "final_message": final_message,
    }

def _existing_broker_dates(conn: sqlite3.Connection, code: str, trading_dates: list[str]) -> set[str]:
    if not trading_dates:
        return set()
    broker_analysis.ensure_broker_tables(conn)
    ph = ",".join("?" for _ in trading_dates)
    rows = conn.execute(
        f"""
        SELECT DISTINCT date
        FROM broker_trading_daily
        WHERE code = ? AND date IN ({ph})
        """,
        [code, *trading_dates],
    ).fetchall()
    return {str(r[0]) for r in rows}


def fetch_broker_data_for_stock(conn: sqlite3.Connection, code: str, days: int = 10, include_debug: bool = False) -> dict[str, Any]:
    """Fetch missing recent broker rows for one stock, then upsert them."""
    broker_analysis.ensure_broker_tables(conn)
    market = detect_market_type(conn, code)
    if market not in ("twse", "tpex"):
        return {
            "status": "unsupported",
            "market": market,
            "message": "無法判斷上市/上櫃市場，未自動抓取",
            "fetched_dates": [],
            "rows": 0,
        }

    trading_dates = get_recent_trading_dates(conn, code, days)
    if not trading_dates:
        return {
            "status": "failed",
            "market": market,
            "message": "找不到該股票最近交易日，未自動抓取",
            "fetched_dates": [],
            "rows": 0,
        }

    existing = _existing_broker_dates(conn, code, trading_dates)
    missing_dates = [d for d in trading_dates if d not in existing]
    if not missing_dates:
        return {
            "status": "not_needed",
            "market": market,
            "message": "最近交易日分點資料已存在",
            "fetched_dates": [],
            "rows": 0,
        }

    all_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    fetched_dates: list[str] = []
    for date in missing_dates:
        try:
            if market == "twse":
                rows = fetch_twse_broker_daily(code, date)
            else:
                rows = fetch_tpex_broker_daily(code, date)
            rows = normalize_fetched_broker_rows(rows)
            if rows:
                all_rows.extend(rows)
                fetched_dates.append(date)
        except Exception as exc:
            errors.append(f"{date}: {exc}")

    upsert_result = upsert_fetched_broker_rows(conn, all_rows) if all_rows else {"rows": 0, "dates": []}
    if fetched_dates and len(fetched_dates) == len(missing_dates):
        status = "success"
        message = f"已自動補齊 {len(fetched_dates)} 個交易日分點資料"
    elif fetched_dates:
        status = "partial"
        message = f"已自動補齊 {len(fetched_dates)} 個交易日分點資料，部分日期官方來源暫無資料"
    else:
        status = "failed"
        message = "官方來源暫無資料或抓取失敗，請稍後再試或使用 CSV 匯入"
    return {
        "status": status,
        "market": market,
        "message": message,
        "fetched_dates": fetched_dates,
        "missing_dates": missing_dates,
        "rows": int(upsert_result.get("rows") or 0),
        "errors": errors[:5],
    }
