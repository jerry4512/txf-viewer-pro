from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from stock_selection_schema import ensure_stock_selection_schema


FIXTURE_SPEC = Path(__file__).parent / "fixtures" / "fixed_db_fixture.json"


def load_fixture_spec() -> dict:
    return json.loads(FIXTURE_SPEC.read_text(encoding="utf-8"))


def make_market_frame(as_of_date: str, bars: int = 70) -> pd.DataFrame:
    dates = pd.bdate_range(end=as_of_date, periods=bars)
    rows = []
    for i, date in enumerate(dates):
        close = 20000 + i * 18
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": close - 8,
            "high": close + 35,
            "low": close - 35,
            "close": close,
            "volume": 1_000_000 + i * 1000,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def fixture_spec() -> dict:
    return load_fixture_spec()


@pytest.fixture
def fixed_db(tmp_path: Path, fixture_spec: dict) -> Path:
    db_path = tmp_path / "stock_selection_fixture.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE daily_kbars (
            code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
            volume INTEGER, PRIMARY KEY(code, date)
        );
        CREATE TABLE stock_names (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT DEFAULT ''
        );
        CREATE TABLE institutional_trading (
            code TEXT, date TEXT, foreign_buy INTEGER, investment_buy INTEGER,
            dealer_buy INTEGER, foreign_buy_shares INTEGER,
            investment_buy_shares INTEGER, dealer_buy_shares INTEGER,
            PRIMARY KEY(code, date)
        );
        CREATE TABLE market_index_daily (
            date TEXT PRIMARY KEY, open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL, ma20 REAL, ma60 REAL,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE broker_period_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL, stock_name TEXT, start_date TEXT, end_date TEXT,
            period_label TEXT NOT NULL, side TEXT NOT NULL, broker_name TEXT NOT NULL,
            buy_lots INTEGER DEFAULT 0, sell_lots INTEGER DEFAULT 0,
            net_lots INTEGER DEFAULT 0, volume_ratio REAL DEFAULT 0,
            source TEXT DEFAULT 'moneydj_fubon', created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, end_date, period_label, side, broker_name, source)
        );
        """
    )

    dates = pd.bdate_range(end=fixture_spec["as_of_date"], periods=fixture_spec["bars"])
    for security in fixture_spec["securities"]:
        code = security["code"]
        conn.execute(
            "INSERT INTO stock_names(code,name,category) VALUES(?,?,?)",
            (code, security["name"], security["industry"]),
        )
        rows = []
        for i, date in enumerate(dates):
            close = security["base_close"] + security["daily_step"] * i
            volume = security["base_volume"] + (i % 5) * 100
            rows.append((
                code, date.strftime("%Y-%m-%d"), close - 0.2, close + 1.0,
                close - 1.0, close, volume,
            ))
        conn.executemany(
            "INSERT INTO daily_kbars VALUES(?,?,?,?,?,?,?)", rows
        )

    # Deliberate future row proves that as_of_date SQL filtering is effective.
    conn.execute(
        "INSERT INTO daily_kbars VALUES(?,?,?,?,?,?,?)",
        ("2945", "2026-04-13", 998, 1000, 997, 999, 9999),
    )

    market = make_market_frame(fixture_spec["as_of_date"], fixture_spec["bars"])
    conn.executemany(
        "INSERT INTO market_index_daily(date,open,high,low,close,volume,amount) "
        "VALUES(?,?,?,?,?,?,0)",
        [tuple(row[c] for c in ("date", "open", "high", "low", "close", "volume"))
         for _, row in market.iterrows()],
    )
    conn.executemany(
        """
        INSERT INTO institutional_trading(
            code,date,foreign_buy,investment_buy,dealer_buy,
            foreign_buy_shares,investment_buy_shares,dealer_buy_shares
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        fixture_spec["institutional_rows"],
    )

    ensure_stock_selection_schema(conn)
    for security in fixture_spec["securities"]:
        instrument_type = security["security_type"]
        conn.execute(
            """
            UPDATE security_master SET market=?, security_type=?,
                is_etf=?, is_leveraged=?, is_inverse=?, is_etn=?,
                is_warrant=?, is_preferred=?, source='fixed_test_fixture'
            WHERE code=?
            """,
            (
                security["market"], instrument_type,
                int(instrument_type in ("etf", "leveraged_etf", "reverse_etf")),
                int(instrument_type == "leveraged_etf"),
                int(instrument_type == "reverse_etf"),
                int(instrument_type == "etn"),
                int(instrument_type == "warrant"),
                int(instrument_type == "preferred_stock"),
                security["code"],
            ),
        )
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def market_frame(fixture_spec: dict) -> pd.DataFrame:
    return make_market_frame(fixture_spec["as_of_date"], fixture_spec["bars"])

