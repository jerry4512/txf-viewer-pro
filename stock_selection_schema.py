"""Schema helpers used by stock-selection V2 correctness fixes.

This module only owns storage metadata.  It deliberately does not change the
existing A/B1/B2/C, Donchian-cost-line, or MACD strategy definitions.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Mapping, Optional, Tuple


SECURITY_MASTER_DDL = """
CREATE TABLE IF NOT EXISTS security_master (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    market TEXT,
    security_type TEXT NOT NULL DEFAULT 'common_stock',
    industry TEXT,
    listing_date TEXT,
    delisting_date TEXT,
    is_etf INTEGER NOT NULL DEFAULT 0,
    is_leveraged INTEGER NOT NULL DEFAULT 0,
    is_inverse INTEGER NOT NULL DEFAULT 0,
    is_etn INTEGER NOT NULL DEFAULT 0,
    is_warrant INTEGER NOT NULL DEFAULT 0,
    is_preferred INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'stock_names_migration',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_INSTITUTION_SHARE_COLUMNS = (
    ("foreign_buy_shares", "INTEGER"),
    ("investment_buy_shares", "INTEGER"),
    ("dealer_buy_shares", "INTEGER"),
    # Capital Flow V2 uses exact net shares.  Legacy *_buy columns remain in
    # lots for formal-score regression compatibility.
    ("foreign_net", "INTEGER"),
    ("trust_net", "INTEGER"),
    ("dealer_prop_net", "INTEGER"),
    ("dealer_hedge_net", "INTEGER"),
    ("dealer_unknown_net", "INTEGER"),
    ("flow_detail_level", "TEXT"),
    ("flow_data_source", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return bool(row)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def conservative_fallback_classification(
    symbol: str, name: str, industry: str
) -> Tuple[str, bool]:
    """Conservative fallback when no authoritative security-master row exists.

    A single Chinese character such as ``購`` or ``售`` is intentionally not a
    warrant signal.  This keeps ``2945 三商家購`` classified as common stock.
    """
    sym = str(symbol or "").strip()
    nm = str(name or "").strip()
    ind = str(industry or "").strip()
    nm_upper = nm.upper()
    ind_upper = ind.upper()
    is_ky = "KY" in nm_upper

    if "ETN" in nm_upper or "ETN" in ind_upper:
        return "etn", is_ky

    # Only explicit product metadata is safe as a name-based warrant fallback.
    if "權證" in nm or "權證" in ind:
        return "warrant", is_ky

    if (
        nm.endswith("甲特")
        or nm.endswith("乙特")
        or nm.endswith("丙特")
        or "特別股" in nm
        or (
            len(nm) >= 2
            and nm[-1] == "特"
            and not nm.endswith("特化")
            and not nm.endswith("特材")
        )
    ):
        return "preferred_stock", is_ky

    is_etf = (sym.startswith("00") and len(sym) <= 7) or "ETF" in nm_upper or "ETF" in ind_upper
    if is_etf:
        if any(kw in nm for kw in ("反向", "反1", "放空", "空方")) or sym.upper().endswith("R"):
            return "reverse_etf", is_ky
        if any(kw in nm_upper for kw in ("正2", "2倍", "槓桿", "2X", "正向2", "兩倍")):
            return "leveraged_etf", is_ky
        return "etf", is_ky

    return "common_stock", is_ky


def classification_from_master(
    record: Optional[Mapping[str, Any]], symbol: str, name: str, industry: str
) -> Tuple[str, bool]:
    """Resolve type from security_master first, then use conservative fallback."""
    if not record:
        return conservative_fallback_classification(symbol, name, industry)

    def flag(key: str) -> bool:
        try:
            return bool(int(record.get(key) or 0))
        except (TypeError, ValueError):
            return bool(record.get(key))

    if flag("is_warrant"):
        instrument_type = "warrant"
    elif flag("is_etn"):
        instrument_type = "etn"
    elif flag("is_preferred"):
        instrument_type = "preferred_stock"
    elif flag("is_inverse"):
        instrument_type = "reverse_etf"
    elif flag("is_leveraged"):
        instrument_type = "leveraged_etf"
    elif flag("is_etf"):
        instrument_type = "etf"
    else:
        instrument_type = str(record.get("security_type") or "common_stock").strip()
        if instrument_type not in {
            "common_stock", "etf", "reverse_etf", "leveraged_etf",
            "etn", "warrant", "preferred_stock", "other",
        }:
            instrument_type = "other"

    master_name = str(record.get("name") or name or "")
    return instrument_type, "KY" in master_name.upper()


def ensure_stock_selection_schema(conn: sqlite3.Connection) -> None:
    """Apply idempotent Milestone-1 schema migrations."""
    conn.execute(SECURITY_MASTER_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_master_type "
        "ON security_master(security_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_security_master_market "
        "ON security_master(market)"
    )

    if _table_exists(conn, "institutional_trading"):
        existing = _table_columns(conn, "institutional_trading")
        for column, sql_type in _INSTITUTION_SHARE_COLUMNS:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE institutional_trading ADD COLUMN {column} {sql_type}"
                )

        # Historical rows can safely recover foreign/trust exact shares from
        # the Milestone-1 raw columns.  Historical dealer totals cannot be
        # split into proprietary vs hedge, so they are explicitly stored as
        # dealer_unknown_net instead of being mislabelled as directional flow.
        conn.execute(
            """
            UPDATE institutional_trading
            SET foreign_net = COALESCE(
                    foreign_net, foreign_buy_shares, foreign_buy * 1000
                ),
                trust_net = COALESCE(
                    trust_net, investment_buy_shares, investment_buy * 1000
                ),
                dealer_unknown_net = CASE
                    WHEN dealer_prop_net IS NULL AND dealer_hedge_net IS NULL
                    THEN COALESCE(
                        dealer_unknown_net, dealer_buy_shares, dealer_buy * 1000
                    )
                    ELSE dealer_unknown_net
                END,
                flow_detail_level = COALESCE(flow_detail_level, 'legacy_combined'),
                flow_data_source = COALESCE(flow_data_source, 'legacy_migration')
            """
        )

    backfill_security_master(conn)
    conn.commit()


def backfill_security_master(conn: sqlite3.Connection) -> None:
    """Seed security_master from existing stock_names without overwriting curated rows."""
    if not _table_exists(conn, "stock_names"):
        return
    rows = conn.execute(
        "SELECT code, name, COALESCE(category, '') FROM stock_names"
    ).fetchall()
    payload = []
    for code, name, industry in rows:
        instrument_type, _ = conservative_fallback_classification(code, name, industry)
        payload.append(
            (
                str(code), str(name or code), instrument_type, str(industry or ""),
                int(instrument_type in {"etf", "leveraged_etf", "reverse_etf"}),
                int(instrument_type == "leveraged_etf"),
                int(instrument_type == "reverse_etf"),
                int(instrument_type == "etn"),
                int(instrument_type == "warrant"),
                int(instrument_type == "preferred_stock"),
            )
        )
    conn.executemany(
        """
        INSERT OR IGNORE INTO security_master (
            code, name, security_type, industry,
            is_etf, is_leveraged, is_inverse, is_etn, is_warrant, is_preferred,
            source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stock_names_migration')
        """,
        payload,
    )


def load_security_master(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    ensure_stock_selection_schema(conn)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM security_master").fetchall()
    return {str(row["code"]): dict(row) for row in rows}
