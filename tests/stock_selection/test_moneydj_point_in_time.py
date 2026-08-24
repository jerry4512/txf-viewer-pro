import sqlite3

import moneydj_fetcher


def test_moneydj_summary_never_selects_future_end_date(fixed_db, fixture_spec):
    conn = sqlite3.connect(fixed_db)
    conn.executemany(
        """
        INSERT INTO broker_period_summary(
            code,stock_name,start_date,end_date,period_label,side,broker_name,
            buy_lots,sell_lots,net_lots,volume_ratio,source
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'moneydj_fubon')
        """,
        [
            ("2945", "三商家購", "2026-04-01", "2026-04-10", "5D", "buy", "A", 100, 0, 100, 5.0),
            ("2945", "三商家購", "2026-04-01", "2026-04-10", "5D", "sell", "B", 0, 50, -50, 2.5),
            ("2945", "三商家購", "2026-04-13", "2026-04-13", "5D", "buy", "FUTURE", 999, 0, 999, 99.0),
        ],
    )
    conn.commit()
    result = moneydj_fetcher.get_moneydj_period_summary(
        conn, "2945", "5D", as_of_date=fixture_spec["as_of_date"]
    )
    conn.close()
    assert result["end_date"] == fixture_spec["as_of_date"]
    assert all(row["broker_name"] != "FUTURE" for row in result["rows"])

