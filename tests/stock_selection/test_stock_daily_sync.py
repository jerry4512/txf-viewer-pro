import sqlite3

import screener


TWSE_FIELDS = [
    "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額",
    "開盤價", "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差",
]
TPEX_FIELDS = [
    "代號", "名稱", "收盤", "漲跌", "開盤", "最高", "最低", "均價",
    "成交股數", "成交金額(元)",
]


def _official_payload(url: str) -> dict:
    if "twse.com.tw" in url:
        return {
            "date": "20260810",
            "stat": "OK",
            "tables": [{
                "fields": TWSE_FIELDS,
                "data": [[
                    "2330", "台積電", "12,345,678", "1", "12,000,000,000",
                    "1,000", "1,020", "995", "1,015",
                    "<p style='color:red'>+</p>", "15",
                ]],
            }],
        }
    return {
        "date": "20260810",
        "stat": "ok",
        "tables": [{
            "fields": TPEX_FIELDS,
            "data": [[
                "6488", "環球晶", "500", "-5", "505", "510", "495", "502",
                "2,345,678", "1,170,000,000",
            ]],
        }],
    }


def test_official_daily_cross_section_parses_both_markets(monkeypatch):
    monkeypatch.setattr(screener, "_fetch_official_json", _official_payload)
    result = screener.fetch_official_stock_daily_kbars("2026-08-10")
    by_code = {row["code"]: row for row in result["records"]}

    assert result["source_status"] == {"twse": "ok", "tpex": "ok"}
    assert result["twse_count"] == 1
    assert result["tpex_count"] == 1
    assert by_code["2330"]["volume"] == 12_345
    assert by_code["2330"]["change_pct"] > 0
    assert by_code["6488"]["volume"] == 2_345
    assert by_code["6488"]["change_pct"] < 0


def test_official_daily_sync_upserts_kbars_and_names(monkeypatch, tmp_path):
    db_path = tmp_path / "official_daily.db"
    monkeypatch.setattr(screener, "DB_PATH", str(db_path))
    monkeypatch.setattr(screener, "_fetch_official_json", _official_payload)

    result = screener.sync_official_stock_daily_kbars(
        "2026-08-10", start_date="2026-08-10"
    )
    assert result["success"] is True
    assert result["inserted_rows"] == 2
    assert result["coverage"]["complete"] is True

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT close FROM daily_kbars WHERE code='2330'").fetchone()[0] == 1015
    assert conn.execute("SELECT name FROM stock_names WHERE code='6488'").fetchone()[0] == "環球晶"
    conn.close()


def test_partial_market_response_is_not_reported_as_complete(monkeypatch, tmp_path):
    db_path = tmp_path / "partial_daily.db"
    monkeypatch.setattr(screener, "DB_PATH", str(db_path))

    def partial_payload(url: str) -> dict:
        payload = _official_payload(url)
        if "tpex.org.tw" in url:
            payload["tables"][0]["data"] = []
        return payload

    monkeypatch.setattr(screener, "_fetch_official_json", partial_payload)
    result = screener.sync_official_stock_daily_kbars(
        "2026-08-10", start_date="2026-08-10"
    )
    assert result["success"] is False
    assert result["failed_dates"][0]["source_status"]["tpex"] == "no_data"

