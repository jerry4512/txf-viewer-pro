import sqlite3

import pandas as pd
import pytest

import tomorrow_strategy as ts


@pytest.mark.parametrize(
    "volume,amount,expected",
    [
        (3000, 100_000_000, "high"),
        (1000, 50_000_000, "normal"),
        (999, 50_000_000, "low_amount_pass"),
        (999, 49_999_999, "low"),
    ],
)
def test_liquidity_thresholds_are_preserved(volume, amount, expected):
    assert ts._classify_liquidity(volume, amount) == expected


def test_amount_ma_is_mean_of_daily_amount(fixed_db, fixture_spec):
    conn = sqlite3.connect(fixed_db)
    frame = pd.read_sql_query(
        "SELECT * FROM daily_kbars WHERE code='2945' AND date<=? ORDER BY date",
        conn, params=[fixture_spec["as_of_date"]],
    )
    conn.close()
    result = ts._analyze_stock("2945", "三商家購", "貿易百貨", frame)
    daily_amount = frame["close"] * frame["volume"] * 1000
    expected20 = daily_amount.tail(20).mean()
    expected5 = daily_amount.tail(5).mean()
    old_approximation = frame.iloc[-1]["close"] * frame["volume"].tail(20).mean() * 1000
    assert result["amount_ma20"] == pytest.approx(expected20, abs=1)
    assert result["amount_ma5"] == pytest.approx(expected5, abs=1)
    assert result["amount_ma20"] != pytest.approx(old_approximation, abs=1)
    assert result["liquidity_trend"] == pytest.approx(expected5 / expected20, abs=1e-4)

