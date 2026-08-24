import sqlite3

import tomorrow_strategy as ts
from stock_selection_schema import load_security_master


def test_single_purchase_character_does_not_mean_warrant():
    instrument_type, _ = ts.classify_instrument("2945", "三商家購", "貿易百貨")
    assert instrument_type == "common_stock"


def test_security_master_has_priority_over_name_fallback():
    record = {
        "name": "測試商品", "security_type": "warrant",
        "is_warrant": 1, "is_etf": 0, "is_leveraged": 0,
        "is_inverse": 0, "is_etn": 0, "is_preferred": 0,
    }
    instrument_type, _ = ts.classify_instrument("999999", "普通名稱", "", record)
    assert instrument_type == "warrant"


def test_fixed_fixture_master_marks_2945_as_common_stock(fixed_db):
    conn = sqlite3.connect(fixed_db)
    master = load_security_master(conn)
    conn.close()
    instrument_type, _ = ts.classify_instrument(
        "2945", "三商家購", "貿易百貨", master["2945"]
    )
    assert instrument_type == "common_stock"

