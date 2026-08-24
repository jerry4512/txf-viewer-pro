import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

import main


def test_trading_doctor_bfi82u_returns_twse_table(monkeypatch):
    monkeypatch.setattr(
        main,
        "_fetch_twse_bfi82u",
        lambda day_date: {
            "stat": "OK",
            "date": day_date,
            "title": "測試三大法人買賣金額統計表",
            "hints": "單位：元",
            "fields": ["單位名稱", "買進金額", "賣出金額", "買賣差額"],
            "data": [["合計", "300", "100", "200"]],
            "notes": ["測試說明"],
            "source_url": "https://www.twse.com.tw/example",
        },
    )

    payload = asyncio.run(main.get_trading_doctor_bfi82u("20260812"))

    assert payload["success"] is True
    assert payload["date"] == "20260812"
    assert payload["data"] == [["合計", "300", "100", "200"]]
    assert payload["source"] == "TWSE BFI82U"


def test_trading_doctor_bfi82u_rejects_invalid_date():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.get_trading_doctor_bfi82u("2026-08-12"))

    assert exc_info.value.status_code == 400


def test_trading_doctor_bfi82u_rejects_empty_report(monkeypatch):
    monkeypatch.setattr(
        main,
        "_fetch_twse_bfi82u",
        lambda day_date: {"stat": "OK", "fields": [], "data": []},
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(main.get_trading_doctor_bfi82u("20260812"))

    assert exc_info.value.status_code == 404


def test_taifex_futures_html_parser(monkeypatch):
    html_text = """
        <table class="table_f table-sticky-3">
          <tbody>
            <tr>
              <td rowspan="3">1</td><td rowspan="3">臺股期貨</td><td>外資及陸資</td>
              <td>48,988</td><td>444,383,293</td><td>46,743</td><td>424,053,049</td>
              <td>2,245</td><td>20,330,245</td><td>10,937</td><td>99,659,435</td>
              <td>97,570</td><td>888,766,736</td><td>-86,633</td><td>-789,107,301</td>
            </tr>
          </tbody>
        </table>
    """
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return html_text.encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["body"] = request.data.decode("ascii")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    payload = main._fetch_taifex_futures_institutional("20260812", "TXF")

    assert "queryDate=2026%2F08%2F12" in captured["body"]
    assert "commodityId=TXF" in captured["body"]
    assert payload["fields"][0] == "日期"
    assert payload["data"] == [[
        "2026/08/12", "臺股期貨", "外資及陸資",
        "48988", "444383293", "46743", "424053049", "2245", "20330245",
        "10937", "99659435", "97570", "888766736", "-86633", "-789107301",
    ]]


def test_taifex_after_hours_html_parser(monkeypatch):
    html_text = """
        <table class="table_f table-sticky-3">
          <tbody>
            <tr>
              <td rowspan="3">1</td><td rowspan="3">臺股期貨</td><td>外資</td>
              <td>5,678</td><td>52,000,000</td><td>4,321</td><td>39,000,000</td>
              <td>1,357</td><td>13,000,000</td>
            </tr>
          </tbody>
        </table>
    """
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return html_text.encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode("ascii")
        return FakeResponse()

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    payload = main._fetch_taifex_futures_institutional(
        "20260813",
        "TAIWAN_INDEX",
        after_hours=True,
    )

    assert captured["url"].endswith("/futContractsDateAh")
    assert "queryDate=2026%2F08%2F13" in captured["body"]
    assert payload["fields"][-1] == "多空交易契約金額淨額(千元)"
    assert payload["data"] == [[
        "2026/08/13", "臺股期貨", "外資", "5678", "52000000",
        "4321", "39000000", "1357", "13000000",
    ]]


def test_trading_doctor_taifex_returns_contract_table(monkeypatch):
    monkeypatch.setattr(
        main,
        "_fetch_taifex_futures_institutional",
        lambda day_date, commodity_id: {
            "date": day_date,
            "fields": ["日期", "商品名稱", "身份別", "多空未平倉口數淨額"],
            "data": [["2026/08/12", "臺股期貨", "外資及陸資", "-86633"]],
            "source_url": "https://www.taifex.com.tw/cht/3/futContractsDate",
        },
    )

    payload = asyncio.run(
        main.get_trading_doctor_taifex_futures("20260812", "TXF")
    )

    assert payload["success"] is True
    assert payload["commodity_id"] == "TXF"
    assert payload["commodity_name"] == "臺股期貨"
    assert payload["data"][0][-1] == "-86633"


def test_trading_doctor_taifex_defaults_to_three_taiwan_index_products(monkeypatch):
    captured = {}

    def fake_fetch(day_date, commodity_id):
        captured["commodity_id"] = commodity_id
        return {
            "date": day_date,
            "fields": ["商品名稱", "身份別", "多空未平倉口數淨額"],
            "data": [
                ["臺股期貨", "外資", "-86249"],
                ["小型臺指期貨", "外資", "-24680"],
                ["微型臺指期貨", "外資", "1200"],
            ],
            "source_url": "https://www.taifex.com.tw/cht/3/futContractsDate",
        }

    monkeypatch.setattr(main, "_fetch_taifex_futures_institutional", fake_fetch)
    monkeypatch.setattr(
        main,
        "_save_taifex_oi_history",
        lambda day_date, summary: captured.update({
            "saved_date": day_date,
            "saved_summary": summary,
        }) or True,
    )
    monkeypatch.setattr(main, "_load_taifex_oi_history", lambda: [])

    payload = asyncio.run(
        main.get_trading_doctor_taifex_futures("20260813")
    )

    assert captured["commodity_id"] == "TAIWAN_INDEX"
    assert payload["commodity_name"] == "台指／小台／微型台指"
    assert [row[0] for row in payload["data"]] == [
        "臺股期貨",
        "小型臺指期貨",
        "微型臺指期貨",
    ]
    assert payload["foreign_oi_summary"] == {
        "txf_foreign_net_oi": -86249,
        "mxf_foreign_net_oi": -24680,
        "tmf_foreign_net_oi": 1200,
        "equivalent_net_oi": -92359.0,
        "formula": "TXF + MXF / 4 + TMF / 20",
    }
    assert captured["saved_date"] == "20260813"
    assert captured["saved_summary"] == payload["foreign_oi_summary"]


def test_taifex_foreign_oi_summary_uses_only_foreign_open_interest_net():
    fields = ["商品名稱", "身份別", "多空交易口數淨額", "多空未平倉口數淨額"]
    rows = [
        ["臺股期貨", "自營商", "999", "888"],
        ["臺股期貨", "外資", "777", "-80000"],
        ["小型臺指期貨", "外資及陸資", "666", "-16000"],
        ["微型臺指期貨", "外資", "555", "2000"],
    ]

    summary = main._build_taifex_foreign_oi_summary(fields, rows)

    assert summary["txf_foreign_net_oi"] == -80000
    assert summary["mxf_foreign_net_oi"] == -16000
    assert summary["tmf_foreign_net_oi"] == 2000
    assert summary["equivalent_net_oi"] == -83900.0


def test_trading_doctor_taifex_rejects_invalid_contract():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main.get_trading_doctor_taifex_futures("20260812", "INVALID")
        )

    assert exc_info.value.status_code == 400


def test_trading_doctor_taifex_after_hours_returns_night_table(monkeypatch):
    captured = {}

    def fake_fetch(day_date, commodity_id, after_hours=False):
        captured.update({
            "day_date": day_date,
            "commodity_id": commodity_id,
            "after_hours": after_hours,
        })
        return {
            "date": day_date,
            "fields": list(main._TAIFEX_AFTER_HOURS_FIELDS),
            "data": [[
                "2026/08/13", "臺股期貨", "外資", "5678", "52000000",
                "4321", "39000000", "1357", "13000000",
            ]],
            "source_url": "https://www.taifex.com.tw/cht/3/futContractsDateAh",
        }

    monkeypatch.setattr(main, "_fetch_taifex_futures_institutional", fake_fetch)

    payload = asyncio.run(
        main.get_trading_doctor_taifex_futures_after_hours("20260813")
    )

    assert captured == {
        "day_date": "20260813",
        "commodity_id": "TAIWAN_INDEX",
        "after_hours": True,
    }
    assert payload["success"] is True
    assert payload["source"] == "TAIFEX 三大法人夜盤"
    assert payload["data"][0][-2] == "1357"


def test_trading_doctor_taifex_rejects_empty_report(monkeypatch):
    monkeypatch.setattr(
        main,
        "_fetch_taifex_futures_institutional",
        lambda day_date, commodity_id: {
            "date": day_date,
            "fields": ["日期"],
            "data": [],
        },
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            main.get_trading_doctor_taifex_futures("20260812", "TXF")
        )

    assert exc_info.value.status_code == 404


def test_taifex_oi_history_keeps_only_newest_fourteen_dates(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_STOCK_DB_PATH", str(tmp_path / "stock_cache.db"))
    start = datetime(2026, 8, 10)

    for offset in range(21):
        day_date = (start + timedelta(days=offset)).strftime("%Y%m%d")
        main._save_taifex_oi_history(day_date, {
            "txf_foreign_net_oi": -80000 - offset,
            "mxf_foreign_net_oi": -16000 - offset,
            "tmf_foreign_net_oi": 2000 + offset,
            "equivalent_net_oi": -83900 - offset,
        })

    rows = main._load_taifex_oi_history()

    assert len(rows) == 14
    assert rows[0]["date"] == "20260830"
    assert rows[-1]["date"] == "20260817"
    assert all(row["date"] != "20260810" for row in rows)


def test_taifex_oi_history_upserts_the_same_date(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_STOCK_DB_PATH", str(tmp_path / "stock_cache.db"))
    first = {
        "txf_foreign_net_oi": -80000,
        "mxf_foreign_net_oi": -16000,
        "tmf_foreign_net_oi": 2000,
        "equivalent_net_oi": -83900,
    }
    updated = {**first, "txf_foreign_net_oi": -81000, "equivalent_net_oi": -84900}

    main._save_taifex_oi_history("20260813", first)
    main._save_taifex_oi_history("20260813", updated)
    rows = main._load_taifex_oi_history()

    assert len(rows) == 1
    assert rows[0]["txf_foreign_net_oi"] == -81000
    assert rows[0]["equivalent_net_oi"] == -84900


def test_taifex_oi_history_calculates_rounded_day_session_change(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(main, "_STOCK_DB_PATH", str(tmp_path / "stock_cache.db"))
    main._save_taifex_oi_history("20260810", {
        "txf_foreign_net_oi": -89201,
        "mxf_foreign_net_oi": -3456,
        "tmf_foreign_net_oi": 10070,
        "equivalent_net_oi": -89561.5,
    })
    main._save_taifex_oi_history("20260811", {
        "txf_foreign_net_oi": -88924,
        "mxf_foreign_net_oi": -3835,
        "tmf_foreign_net_oi": 13362,
        "equivalent_net_oi": -89214.65,
    })

    rows = main._load_taifex_oi_history()

    assert rows[0]["date"] == "20260811"
    assert rows[0]["equivalent_net_oi_rounded"] == -89215
    assert rows[0]["day_session_change"] == 346
    assert rows[1]["date"] == "20260810"
    assert rows[1]["equivalent_net_oi_rounded"] == -89561
    assert rows[1]["day_session_change"] is None
