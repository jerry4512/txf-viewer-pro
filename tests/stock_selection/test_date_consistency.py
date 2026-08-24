import integrated_strategy as integrated
import main
import market_status
import tomorrow_strategy as ts


def test_strategy_filters_future_rows_and_uses_exact_as_of_date(
    monkeypatch, fixed_db, fixture_spec, market_frame
):
    monkeypatch.setattr(ts, "_DB_PATH", str(fixed_db))
    monkeypatch.setattr(market_status, "fetch_market_index_daily", lambda: market_frame.copy())
    result = ts.run_tomorrow_strategy(as_of_date=fixture_spec["as_of_date"])
    assert result["strategy_valid"] is True
    assert result["as_of_date"] == fixture_spec["as_of_date"]
    assert result["stock_kbar_date"] == fixture_spec["as_of_date"]
    all_rows = (
        result["buy_candidates"] + result["high_priority_watch"]
        + result["other_watch"] + result["excluded"] + result["etf_candidates"]
    )
    row_2945 = next(row for row in all_rows if row.get("symbol") == "2945")
    assert row_2945.get("close") != 999
    assert row_2945.get("instrument_type") == "common_stock"


def test_stock_date_mismatch_fails_closed(
    monkeypatch, fixed_db, market_frame
):
    monkeypatch.setattr(ts, "_DB_PATH", str(fixed_db))
    monkeypatch.setattr(market_status, "fetch_market_index_daily", lambda: market_frame.copy())
    result = ts.run_tomorrow_strategy(as_of_date="2026-04-13")
    assert result["strategy_valid"] is False
    assert result["buy_candidates"] == []


def test_market_date_mismatch_fails_closed(
    monkeypatch, fixed_db, fixture_spec, market_frame
):
    monkeypatch.setattr(ts, "_DB_PATH", str(fixed_db))
    monkeypatch.setattr(
        market_status, "fetch_market_index_daily", lambda: market_frame.iloc[:-1].copy()
    )
    result = ts.run_tomorrow_strategy(as_of_date=fixture_spec["as_of_date"])
    assert result["strategy_valid"] is False
    assert result["buy_candidates"] == []


def test_institutional_query_is_point_in_time(monkeypatch, fixed_db, fixture_spec):
    monkeypatch.setattr(integrated, "_DB_PATH", str(fixed_db))
    result = integrated._get_chip_data(["2945"], fixture_spec["as_of_date"])["2945"]
    assert result["foreign_5d"] == 20
    assert result["trust_5d"] == 25
    assert result["foreign_5d"] < 999
    assert result["foreign_5d_shares"] == 22_000


def test_invalid_strategy_blocks_result_validation_and_telegram():
    invalid_result = {
        "strategy_valid": False,
        "as_of_date": "2026-04-10",
        "stock_kbar_date": "2026-04-09",
        "data_errors": ["stock date mismatch"],
        "market_regime": {
            "status": "data_invalid",
            "data_date": "2026-04-10",
            "metrics": {"data_available": False, "regime_error": True,
                        "actual_data_date": "2026-04-09",
                        "expected_data_date": "2026-04-10"},
        },
        "buy_candidates": [{"stock_id": "SHOULD_NOT_SEND"}],
    }
    validation = main.validate_result_data_date(invalid_result)
    tg = main.build_tg_pick_list(invalid_result)
    assert validation["critical_ok"] is False
    assert validation["strategy_valid"] is False
    assert tg["blocked"] is True
    assert tg["tg_picks"] == []
