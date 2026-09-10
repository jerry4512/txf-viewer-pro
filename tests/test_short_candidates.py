from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from short_candidates import (
    ShortCandidatesRepository,
    ShortCandidatesService,
    ShortScannerConfig,
    ShortScannerError,
    _normalize_snapshot_payload,
    calculate_component_scores,
    calculate_factors,
    validate_snapshot_completeness,
)


def _history(end="2026-08-20", count=21, *, volume=1000):
    end_date = date.fromisoformat(end)
    rows = []
    for index in range(count):
        day = end_date - timedelta(days=count - index - 1)
        close = 100.0 + index
        rows.append({
            "date": day.isoformat(),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": volume,
        })
    return rows


def test_factors_exclude_today_from_prior_windows_and_future_rows():
    rows = _history(end="2026-08-19", count=20)
    rows.append({
        "date": "2026-08-20",
        "open": 121,
        "high": 125,
        "low": 98,
        "close": 99,
        "volume": 4000,
    })
    rows.append({
        "date": "2026-08-21",
        "open": 999,
        "high": 1000,
        "low": 998,
        "close": 999,
        "volume": 999999,
    })

    factors = calculate_factors(
        rows, as_of_date="2026-08-20", market_return=1.5
    )

    assert factors is not None
    assert factors["avg_volume_20"] == 1000
    assert factors["relative_volume"] == 4
    assert factors["prior_20d_high"] == 120
    assert factors["prior_support"] == 109
    assert factors["failed_breakout"] is True
    assert factors["breakdown"] is True
    assert factors["close_location"] == pytest.approx(1 / 27, abs=1e-6)
    assert factors["return_3d"] == pytest.approx((99 / 117 - 1) * 100, abs=1e-4)
    assert factors["return_5d"] == pytest.approx((99 / 115 - 1) * 100, abs=1e-4)
    assert factors["return_10d"] == pytest.approx((99 / 110 - 1) * 100, abs=1e-4)
    assert factors["return_20d"] == pytest.approx(-1.0, abs=1e-4)
    assert factors["distance_5ma"] is not None
    assert factors["distance_20ma"] is not None
    assert factors["atr14"] is not None
    assert factors["market_relative_strength"] == pytest.approx(
        factors["daily_return"] - 1.5, abs=1e-4
    )


def test_flat_candle_zero_volume_and_insufficient_history_are_safe():
    rows = _history(end="2026-08-19", count=20, volume=0)
    rows.append({
        "date": "2026-08-20", "open": 100, "high": 100,
        "low": 100, "close": 100, "volume": 0,
    })
    factors = calculate_factors(rows, as_of_date="2026-08-20", market_return=0)
    assert factors["close_location"] == 0.5
    assert factors["upper_shadow_ratio"] == 0
    assert factors["relative_volume"] is None
    assert calculate_factors(
        rows[-10:], as_of_date="2026-08-20", market_return=0
    ) is None


def test_component_scores_and_penalty_are_bounded_and_explainable():
    factors = {
        "failed_breakout": True,
        "breakdown": True,
        "return_20d": 12,
        "distance_20ma": 9,
        "close_location": 0.05,
        "relative_volume": 3,
        "upper_shadow_ratio": 0.6,
        "market_relative_strength": -4,
        "industry_relative_strength": -4,
        "return_3d": -9,
        "return_5d": -13,
        "distance_5ma": -6,
        "daily_move_atr": -2,
        "daily_return": -8,
    }
    scores = calculate_component_scores(
        factors,
        chip={"data_status": "available", "net_ratio_5d": -0.6},
    )
    assert scores["price_structure_score"] == 35
    assert scores["volume_candle_score"] <= 25
    assert scores["relative_weakness_score"] == 25
    assert scores["capital_chip_score"] == 15
    assert scores["overextension_penalty"] == 17
    assert scores["short_score"] == pytest.approx(scores["base_short_score"] - 17)

    missing = calculate_component_scores(factors, chip={"data_status": "missing"})
    assert missing["capital_chip_score"] is None
    assert missing["score_coverage_weight"] == 85


def test_snapshot_completeness_accepts_last_trading_day_and_rejects_mismatch():
    config = replace(
        ShortScannerConfig(), min_snapshot_total=2, min_snapshot_per_market=1
    )
    payload = {
        "date": "2026-08-14",
        "time": "140000",
        "data": [{
            "symbol": "2330", "name": "台積電", "openPrice": 100,
            "highPrice": 105, "lowPrice": 99, "closePrice": 101,
            "tradeVolume": 2_000_000, "tradeValue": 202_000_000,
            "isTrial": False,
        }],
    }
    tse = _normalize_snapshot_payload(payload, "TSE")
    otc = _normalize_snapshot_payload(
        {**payload, "data": [{**payload["data"][0], "symbol": "6488"}]}, "OTC"
    )
    assert tse["rows"][0]["volume"] == 2000
    assert validate_snapshot_completeness(
        [tse, otc], prior_universe_count=2, config=config
    ) == "2026-08-14"

    otc["date"] = "2026-08-13"
    with pytest.raises(ShortScannerError, match="日期不一致"):
        validate_snapshot_completeness(
            [tse, otc], prior_universe_count=2, config=config
        )


def test_repository_daily_and_scanner_upserts_replace_duplicate_dates(tmp_path):
    repository = ShortCandidatesRepository(str(tmp_path / "short.db"))
    first = {
        "symbol": "2330", "name": "台積電", "market": "TSE", "industry": "24",
        "open": 100, "high": 105, "low": 99, "close": 101, "volume": 2000,
    }
    repository.upsert_snapshot([first], "2026-08-20")
    repository.upsert_snapshot([{**first, "close": 102}], "2026-08-20")
    histories = repository.load_histories(["2330"], "2026-08-20")
    assert len(histories["2330"]) == 1
    assert histories["2330"][0]["close"] == 102

    config = ShortScannerConfig()
    candidate = {
        "symbol": "2330", "rank": 1, "name": "台積電", "market": "TSE",
        "short_score": 80, "close": 102, "reasons": ["test"],
    }
    kwargs = {
        "summary": {"dataDate": "2026-08-20"}, "config": config,
        "started_at": "2026-08-20T14:00:00+08:00",
        "finished_at": "2026-08-20T14:01:00+08:00", "warnings": [],
    }
    repository.save_results(candidates=[candidate], **kwargs)
    repository.save_results(candidates=[{**candidate, "short_score": 81}], **kwargs)
    dashboard = repository.dashboard()
    assert len(dashboard["candidates"]) == 1
    assert dashboard["candidates"][0]["short_score"] == 81


class _FakeFubonClient:
    def __init__(self):
        self.snapshot_rows = {
            "TSE": [{
                "symbol": "2330", "name": "台積電", "openPrice": 119,
                "highPrice": 123, "lowPrice": 117, "closePrice": 118,
                "tradeVolume": 3_000_000, "tradeValue": 354_000_000,
                "change": -1, "changePercent": -0.84, "isTrial": False,
            }],
            "OTC": [{
                "symbol": "6488", "name": "環球晶", "openPrice": 119,
                "highPrice": 124, "lowPrice": 116, "closePrice": 117,
                "tradeVolume": 2_000_000, "tradeValue": 234_000_000,
                "change": -2, "changePercent": -1.68, "isTrial": False,
            }],
        }

    def stock_snapshot_quotes(self, market, security_type):
        assert security_type == "COMMONSTOCK"
        return {"date": "2026-08-20", "time": "140000", "data": self.snapshot_rows[market]}

    def stock_historical_daily_candles(self, symbol, start, end):
        assert symbol == "IX0001"
        return {"data": [
            {"date": "2026-08-19", "open": 100, "high": 101, "low": 99,
             "close": 100, "volume": 1_000_000},
            {"date": "2026-08-20", "open": 100, "high": 102, "low": 99,
             "close": 101, "volume": 1_000_000},
        ]}

    def stock_tickers(self, market, **filters):
        return {"date": "2026-08-20", "data": []}

    def stock_ticker_details(self, symbol):
        return {
            "symbol": symbol, "industry": "24", "securityType": "01",
            "securityStatus": "NORMAL", "canDayTrade": True,
            "canBuyDayTrade": True, "isAttention": False,
            "isDisposition": False,
        }


def test_service_refresh_uses_cache_and_persists_ranked_results(tmp_path):
    repository = ShortCandidatesRepository(str(tmp_path / "service.db"))
    for symbol in ("2330", "6488"):
        repository.upsert_history(symbol, _history(end="2026-08-19", count=20, volume=1000))
    config = replace(
        ShortScannerConfig(), min_snapshot_total=2, min_snapshot_per_market=1,
        min_avg_volume_20_lots=1, max_history_requests_per_run=0,
        metadata_candidates=2,
    )
    result = ShortCandidatesService(repository, config).refresh(_FakeFubonClient())

    assert result["dataDate"] == "2026-08-20"
    assert result["summary"]["universeTotal"] == 2
    assert result["summary"]["validCandidates"] == 2
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["rank"] == 1
    assert result["candidates"][0]["day_trade_status"] == "eligible_current_unverified_next_day"
    assert result["candidates"][0]["chip_data_status"] == "missing"


def test_short_candidates_api_reads_cache_and_requires_login_for_refresh(monkeypatch):
    dashboard = {
        "scannerVersion": "short_v1", "dataDate": "2026-08-20",
        "lastUpdated": "2026-08-20T16:00:00+08:00", "summary": {},
        "config": {}, "warnings": [], "candidates": [],
    }

    class FakeRepository:
        def dashboard(self, **kwargs):
            return {**dashboard, "query": kwargs}

    class FakeService:
        config = ShortScannerConfig()
        repository = FakeRepository()

        def refresh(self, client):
            return dashboard

    monkeypatch.setattr(main, "_short_candidates_service", FakeService())
    monkeypatch.setattr(main, "is_logged_in", False)
    monkeypatch.setattr(main, "api", SimpleNamespace(stock_rest=None))
    client = TestClient(main.app)

    cached = client.get("/api/short-candidates?date=2026-08-20&limit=10")
    assert cached.status_code == 200
    assert cached.json()["query"]["data_date"] == "2026-08-20"
    assert cached.json()["query"]["limit"] == 10
    unauthorized = client.post("/api/short-candidates/refresh")
    assert unauthorized.status_code == 401

    monkeypatch.setattr(main, "is_logged_in", True)
    monkeypatch.setattr(main, "api", SimpleNamespace(stock_rest=object()))
    refreshed = client.post("/api/short-candidates/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["dataDate"] == "2026-08-20"
