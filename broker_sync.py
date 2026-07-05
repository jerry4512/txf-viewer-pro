"""
broker_sync.py

Import officially downloaded TWSE / TPEx broker trading CSV files into
broker_trading_daily.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sqlite3
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

import broker_analysis

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "stock_cache.db")

ENCODING_CANDIDATES = ("utf-8-sig", "utf-8", "big5", "cp950")
TPEX_TITLE = "券商買賣證券成交價量資訊"
TWSE_TITLE = "券商買賣股票成交價量資訊"


class BrokerCsvError(ValueError):
    """Raised when an official broker CSV cannot be imported safely."""


def _read_csv_text(path: str | os.PathLike[str]) -> tuple[str, str]:
    raw = Path(path).read_bytes()
    last_error: Exception | None = None
    for encoding in ENCODING_CANDIDATES:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError as exc:
            last_error = exc
    raise BrokerCsvError(f"Unable to decode CSV with {', '.join(ENCODING_CANDIDATES)}: {last_error}")


def _normalize_text(value: Any) -> str:
    text = str(value or "")
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\u3000", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _to_int(value: Any) -> int:
    text = _normalize_text(value).replace(",", "")
    text = re.sub(r"[^0-9\-]", "", text)
    if text in ("", "-"):
        return 0
    return int(text)


def _parse_broker(value: Any) -> tuple[str, str, str]:
    text = _normalize_text(value)
    match = re.match(r"^(\d{4})\s*(.*)$", text)
    if match:
        broker_id = match.group(1)
        branch_name = re.sub(r"\s+", "", _normalize_text(match.group(2))) or broker_id
    else:
        broker_id = ""
        branch_name = re.sub(r"\s+", "", text)
    broker_name = branch_name
    return broker_id, broker_name, branch_name


def _parse_roc_date(raw: str) -> str:
    match = re.search(r"(\d{3})(\d{2})(\d{2})", raw)
    if not match:
        raise BrokerCsvError(f"No ROC date found in filename: {raw}")
    roc_year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    western = roc_year + 1911
    try:
        return date(western, month, day).isoformat()
    except ValueError as exc:
        raise BrokerCsvError(f"Invalid ROC date in filename: {raw}") from exc


def _resolve_trade_date(path: str | os.PathLike[str], manual_date: str | None) -> str:
    if manual_date:
        try:
            return date.fromisoformat(manual_date).isoformat()
        except ValueError as exc:
            raise BrokerCsvError(f"Invalid --date value, expected YYYY-MM-DD: {manual_date}") from exc
    filename = Path(path).name
    try:
        return _parse_roc_date(filename)
    except BrokerCsvError as exc:
        raise BrokerCsvError("Unable to determine trade date from filename. Please pass --date YYYY-MM-DD.") from exc


def _detect_source(first_line: str) -> tuple[str, str]:
    title = _normalize_text(first_line)
    if TPEX_TITLE in title:
        return "tpex", "official_csv_tpex"
    if TWSE_TITLE in title:
        return "twse", "official_csv_twse"
    raise BrokerCsvError(f"Unsupported official CSV format: {title}")


def _extract_code(source_type: str, row: list[str]) -> str:
    if len(row) < 2:
        raise BrokerCsvError("Missing stock code row in CSV")
    label = _normalize_text(row[0])
    value = _normalize_text(row[1])
    expected = "證券代碼" if source_type == "tpex" else "股票代碼"
    if expected not in label:
        raise BrokerCsvError(f"Unexpected stock code label: {label}")
    match = re.search(r"\d{4,6}", value)
    if not match:
        raise BrokerCsvError(f"Unable to parse stock code from: {value}")
    return match.group(0)


def _add_detail(agg: dict[tuple[str, str], dict[str, Any]], broker_cell: Any, buy: Any, sell: Any) -> None:
    broker_id, broker_name, branch_name = _parse_broker(broker_cell)
    if not branch_name:
        return
    buy_qty = _to_int(buy)
    sell_qty = _to_int(sell)
    if buy_qty == 0 and sell_qty == 0:
        return
    key = (broker_name, branch_name)
    item = agg.setdefault(
        key,
        {
            "broker_id": broker_id,
            "broker_name": broker_name,
            "branch_name": branch_name,
            "buy_qty": 0,
            "sell_qty": 0,
        },
    )
    if broker_id and not item.get("broker_id"):
        item["broker_id"] = broker_id
    item["buy_qty"] += buy_qty
    item["sell_qty"] += sell_qty


def _parse_tpex_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows[3:]:
        if len(row) < 5:
            continue
        _add_detail(agg, row[1], row[3], row[4])
    return list(agg.values())


def _parse_twse_rows(rows: list[list[str]]) -> list[dict[str, Any]]:
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows[3:]:
        for offset in (0, 6):
            block = row[offset:offset + 5]
            if len(block) < 5:
                continue
            _add_detail(agg, block[1], block[3], block[4])
    return list(agg.values())


def parse_official_broker_csv(path: str | os.PathLike[str], trade_date: str | None = None) -> dict[str, Any]:
    text, encoding = _read_csv_text(path)
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 3:
        raise BrokerCsvError("CSV has fewer than three header rows")

    source_type, source = _detect_source(rows[0][0] if rows[0] else "")
    code = _extract_code(source_type, rows[1])
    resolved_date = _resolve_trade_date(path, trade_date)
    parsed = _parse_tpex_rows(rows) if source_type == "tpex" else _parse_twse_rows(rows)

    normalized_rows: list[dict[str, Any]] = []
    for item in parsed:
        buy_qty = int(item["buy_qty"])
        sell_qty = int(item["sell_qty"])
        normalized_rows.append(
            {
                "code": code,
                "date": resolved_date,
                "broker_id": item.get("broker_id", ""),
                "broker_name": item["broker_name"],
                "branch_name": item["branch_name"],
                "buy_qty": buy_qty,
                "sell_qty": sell_qty,
                "net_qty": buy_qty - sell_qty,
                "source": source,
            }
        )

    return {
        "path": str(path),
        "encoding": encoding,
        "format": source_type,
        "source": source,
        "code": code,
        "date": resolved_date,
        "rows": normalized_rows,
        "branch_count": len(normalized_rows),
    }


def upsert_broker_csv_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> dict[str, Any]:
    broker_analysis.ensure_broker_tables(conn)
    if not rows:
        return {"rows": 0, "inserted_rows": 0, "updated_rows": 0, "dates": [], "codes": []}

    existing_keys = set()
    for row in rows:
        hit = conn.execute(
            """
            SELECT 1 FROM broker_trading_daily
            WHERE code = ? AND date = ? AND broker_name = ? AND branch_name = ?
            LIMIT 1
            """,
            (row["code"], row["date"], row["broker_name"], row["branch_name"]),
        ).fetchone()
        if hit:
            existing_keys.add((row["code"], row["date"], row["broker_name"], row["branch_name"]))

    conn.executemany(
        """
        INSERT INTO broker_trading_daily (
            code, date, broker_id, broker_name, branch_name,
            buy_qty, sell_qty, net_qty, source, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(code, date, broker_name, branch_name) DO UPDATE SET
            broker_id = excluded.broker_id,
            buy_qty = excluded.buy_qty,
            sell_qty = excluded.sell_qty,
            net_qty = excluded.net_qty,
            source = excluded.source,
            updated_at = CURRENT_TIMESTAMP
        """,
        [
            (
                row["code"],
                row["date"],
                row.get("broker_id", ""),
                row["broker_name"],
                row["branch_name"],
                int(row["buy_qty"]),
                int(row["sell_qty"]),
                int(row["net_qty"]),
                row["source"],
            )
            for row in rows
        ],
    )
    conn.commit()

    unique_keys = {(row["code"], row["date"], row["broker_name"], row["branch_name"]) for row in rows}
    return {
        "rows": len(unique_keys),
        "inserted_rows": len(unique_keys - existing_keys),
        "updated_rows": len(unique_keys & existing_keys),
        "dates": sorted({row["date"] for row in rows}, reverse=True),
        "codes": sorted({row["code"] for row in rows}),
    }


def import_official_broker_csv(
    csv_path: str | os.PathLike[str],
    trade_date: str | None = None,
    db_path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> dict[str, Any]:
    parsed = parse_official_broker_csv(csv_path, trade_date)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        upsert_result = upsert_broker_csv_rows(conn, parsed["rows"])
    finally:
        conn.close()
    return {**parsed, "upsert": upsert_result}


def main() -> None:
    parser = argparse.ArgumentParser(description="Import official TWSE/TPEx broker trading CSV into broker_trading_daily.")
    parser.add_argument("--csv", required=True, help="Path to official downloaded broker CSV")
    parser.add_argument("--date", help="Trade date as YYYY-MM-DD when filename has no ROC date")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLite DB path (default: {DEFAULT_DB_PATH})")
    args = parser.parse_args()

    try:
        result = import_official_broker_csv(args.csv, trade_date=args.date, db_path=args.db)
    except BrokerCsvError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    upsert = result["upsert"]
    print(
        "Imported official broker CSV: "
        f"code={result['code']} date={result['date']} format={result['format']} "
        f"encoding={result['encoding']} branches={result['branch_count']} "
        f"inserted={upsert['inserted_rows']} updated={upsert['updated_rows']} source={result['source']}"
    )


if __name__ == "__main__":
    main()

