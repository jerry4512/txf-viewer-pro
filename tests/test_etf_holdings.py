from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import main
from etf_holdings import (
    ETFHoldingsAnalyzer,
    ETFHoldingsRepository,
    ETFHoldingsService,
)
from fubon_market_data import FubonMarketDataClient, FubonMarketDataError


def _row(day, symbol, quantity, weight, name=None, quantity_change=None, weight_change=None):
    return {
        "date": day,
        "stock_symbol": symbol,
        "stock_name": name or symbol,
        "quantity": quantity,
        "quantity_change": quantity_change,
        "weight": weight,
        "weight_change": weight_change,
    }


def _scaling_fixture():
    rows = []
    for symbol, quantity, weight in (
        ("A", 100_000, 1.0),
        ("B", 100_000, 1.0),
        ("C", 100_000, 1.0),
    ):
        rows.append(_row("2026-08-24", symbol, quantity, weight))
    rows.extend((
        _row("2026-08-25", "A", 90_000, 0.90, quantity_change=-10_000, weight_change=-0.10),
        _row("2026-08-25", "B", 110_000, 1.10, quantity_change=10_000, weight_change=0.10),
        _row("2026-08-25", "C", 70_000, 0.70, quantity_change=-30_000, weight_change=-0.30),
    ))
    return rows


def _period_fixture():
    # Twenty ETF disclosure dates with real calendar gaps.  The target has:
    # 5D = 2 adds / 0 reduces, 10D = 3 / 1, 20D = 3 / 3.
    dates = [
        "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03",
        "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
        "2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13",
        "2026-08-14", "2026-08-17", "2026-08-18", "2026-08-19",
        "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25",
    ]
    actions = {
        2: -0.20,
        6: -0.20,
        11: 0.30,
        14: -0.10,
        16: 0.20,
        18: 0.20,
    }
    rows = []
    target_quantity = 100_000
    target_weight = 0.60
    for index, day in enumerate(dates):
        for core_index, symbol in enumerate(("CORE1", "CORE2", "CORE3"), start=1):
            rows.append(_row(
                day,
                symbol,
                1_000_000 + core_index * 100_000,
                4.0 + core_index,
                quantity_change=0,
                weight_change=0.0,
            ))
        previous_quantity = target_quantity
        change = actions.get(index, 0.0)
        target_quantity = round(target_quantity * (1 + change))
        weight_change = 0.10 if change > 0 else (-0.10 if change < 0 else 0.0)
        target_weight += weight_change
        rows.append(_row(
            day,
            "TARGET",
            target_quantity,
            target_weight,
            "期間測試股",
            quantity_change=(target_quantity - previous_quantity) if index else 0,
            weight_change=weight_change,
        ))
    return rows, dates


def _by_symbol(result):
    return {row["stockSymbol"]: row for row in result["holdings"]}


def test_scaling_baseline_classifies_relative_allocation_not_raw_direction():
    result = ETFHoldingsAnalyzer().analyze(_scaling_fixture(), etf_symbol="00981A")
    rows = _by_symbol(result)

    assert result["summary"]["fundScalingBaseline"] == pytest.approx(-0.10)
    assert rows["A"]["behavior"] == "PASSIVE_SCALE"
    assert rows["A"]["relativeAllocationChange"] == pytest.approx(0.0)
    assert rows["B"]["behavior"] == "ACTIVE_ADD"
    assert rows["B"]["relativeAllocationChange"] == pytest.approx(0.20)
    assert rows["C"]["behavior"] == "ACTIVE_REDUCE"
    assert rows["C"]["relativeAllocationChange"] == pytest.approx(-0.20)


def test_new_and_exit_positions_respect_effective_position_threshold():
    rows = [
        _row("2026-08-22", "CORE1", 1_000_000, 5.0),
        _row("2026-08-22", "CORE2", 900_000, 4.0),
        _row("2026-08-22", "CORE3", 800_000, 3.0),
        _row("2026-08-22", "NEW", 1_000, 0.0),
        _row("2026-08-22", "EXIT", 100_000, 0.5),
        _row("2026-08-25", "CORE1", 1_000_000, 5.0),
        _row("2026-08-25", "CORE2", 900_000, 4.0),
        _row("2026-08-25", "CORE3", 800_000, 3.0),
        _row("2026-08-25", "NEW", 100_000, 0.3),
    ]
    result = ETFHoldingsAnalyzer().analyze(rows, etf_symbol="00981A")
    current = _by_symbol(result)
    assert current["NEW"]["behavior"] == "NEW_POSITION"
    assert current["NEW"]["relativeAllocationChange"] is None
    assert current["EXIT"]["behavior"] == "EXIT_POSITION"
    assert current["EXIT"]["relativeAllocationChange"] is None


def test_consecutive_low_weight_accumulation_becomes_fast_candidate():
    rows = []
    for day in ("2026-08-20", "2026-08-21", "2026-08-24"):
        for index, symbol in enumerate(("CORE1", "CORE2", "CORE3"), start=1):
            rows.append(_row(day, symbol, 1_000_000 + index * 10_000, 3.0 + index))
    rows.extend((
        _row("2026-08-20", "FAST", 1_000, 0.0, "快速建倉股"),
        _row("2026-08-21", "FAST", 100_000, 0.20, "快速建倉股"),
        _row("2026-08-24", "FAST", 220_000, 0.42, "快速建倉股"),
    ))
    result = ETFHoldingsAnalyzer().analyze(rows, etf_symbol="00981A")
    fast = _by_symbol(result)["FAST"]
    assert fast["behavior"] == "ACTIVE_ADD"
    assert fast["intent"] == "FAST_ACCUMULATION"
    assert fast["metrics"]["5"]["accumulationEventCount"] == 2


def test_selected_period_uses_disclosure_dates_and_changes_analysis():
    rows, dates = _period_fixture()
    results = {
        period: ETFHoldingsAnalyzer().analyze(
            rows, etf_symbol="00981A", selected_period=period
        )
        for period in (5, 10, 20)
    }
    targets = {period: _by_symbol(result)["TARGET"] for period, result in results.items()}

    assert [results[period]["analysisWindow"]["usedDisclosureDateCount"] for period in (5, 10, 20)] == [5, 10, 20]
    assert results[5]["analysisWindow"]["disclosureDates"] == dates[-5:]
    assert results[10]["analysisWindow"]["disclosureDates"] == dates[-10:]
    assert results[20]["analysisWindow"]["disclosureDates"] == dates
    assert [(targets[p]["activeAddCount"], targets[p]["activeReduceCount"]) for p in (5, 10, 20)] == [(2, 0), (3, 1), (3, 3)]
    assert [targets[p]["cumulativeRelativeAllocationChange"] for p in (5, 10, 20)] == pytest.approx(
        [0.4, 0.6, 0.2], abs=1e-5
    )
    assert len({targets[p]["convictionScore"] for p in (5, 10, 20)}) == 3
    assert [targets[p]["intent"] for p in (5, 10, 20)] == [
        "FAST_ACCUMULATION", "CONVICTION_RISING", "TACTICAL",
    ]
    # Snapshot fields are latest-day facts and intentionally stay unchanged.
    assert len({targets[p]["quantity"] for p in (5, 10, 20)}) == 1
    assert len({targets[p]["behavior"] for p in (5, 10, 20)}) == 1


def test_repository_preserves_raw_fields_and_history(tmp_path):
    repository = ETFHoldingsRepository(str(tmp_path / "etf.db"))
    payload = {
        "symbol": "00981A",
        "type": "EQUITY",
        "exchange": "TWSE",
        "market": "TSE",
        "data": [{
            "date": "2026-08-25",
            "components": [{
                "symbol": "2330",
                "name": "台積電",
                "quantity": 11_884_000,
                "quantityChange": 0,
                "weight": 10.09,
                "weightChange": -0.03,
                "futureUsefulField": "preserved",
            }],
        }],
    }
    saved = repository.save_response(
        payload,
        date_from="2026-08-25",
        date_to="2026-08-25",
        sdk_version="2.2.9",
    )
    history = repository.load_history("00981A")
    assert saved["rows"] == 1
    assert history[0]["quantity"] == 11_884_000
    assert '"futureUsefulField":"preserved"' in history[0]["raw_json"]


def test_fubon_adapter_calls_official_sdk_function():
    calls = []

    class Ownership:
        def etf_holdings(self, **params):
            calls.append(params)
            return {"symbol": "00981A", "data": []}

    client = FubonMarketDataClient()
    client.stock_rest = SimpleNamespace(ownership=Ownership())
    result = client.etf_holdings(
        symbol="00981A", start="2026-08-01", end="2026-08-25", sort="asc"
    )
    assert result["data"] == []
    assert calls == [{
        "symbol": "00981A",
        "from": "2026-08-01",
        "to": "2026-08-25",
        "sort": "asc",
    }]


def test_fubon_adapter_handles_unsupported_sdk_shape():
    client = FubonMarketDataClient()
    client.stock_rest = SimpleNamespace()
    with pytest.raises(FubonMarketDataError, match="最低需要 2.2.9"):
        client.etf_holdings(
            symbol="00981A", start="2026-08-01", end="2026-08-25"
        )


class _FakeETFClient:
    version = "2.2.9"

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error

    def etf_holdings(self, **_kwargs):
        if self.error:
            raise self.error
        return self.payload


def _api_payload():
    by_date = {}
    for row in _scaling_fixture():
        component = {
            "symbol": row["stock_symbol"],
            "name": row["stock_name"],
            "quantity": row["quantity"],
            "quantityChange": row["quantity_change"],
            "weight": row["weight"],
            "weightChange": row["weight_change"],
        }
        by_date.setdefault(row["date"], []).append(component)
    return {
        "symbol": "00981A",
        "type": "EQUITY",
        "exchange": "TWSE",
        "market": "TSE",
        "data": [
            {"date": day, "components": components}
            for day, components in sorted(by_date.items())
        ],
    }


def _payload_from_rows(rows):
    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append({
            "symbol": row["stock_symbol"],
            "name": row["stock_name"],
            "quantity": row["quantity"],
            "quantityChange": row["quantity_change"],
            "weight": row["weight"],
            "weightChange": row["weight_change"],
        })
    return {
        "symbol": "00981A",
        "type": "EQUITY",
        "exchange": "TWSE",
        "market": "TSE",
        "data": [
            {"date": day, "components": components}
            for day, components in sorted(by_date.items())
        ],
    }


def test_api_refresh_and_history_range(monkeypatch, tmp_path):
    service = ETFHoldingsService(ETFHoldingsRepository(str(tmp_path / "api.db")))
    monkeypatch.setattr(main, "_etf_holdings_service", service)
    monkeypatch.setattr(main, "api", _FakeETFClient(_api_payload()))
    monkeypatch.setattr(main, "is_logged_in", True)
    client = TestClient(main.app)

    refreshed = client.post("/api/etf/holdings/refresh", json={"symbol": "00981A"})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh"]["dates"] == 2

    dashboard = client.get("/api/etf/holdings?symbol=00981A&period=5")
    assert dashboard.status_code == 200
    assert dashboard.json()["availableDates"] == ["2026-08-24", "2026-08-25"]


def test_api_period_regression_returns_independent_window_analysis(monkeypatch, tmp_path):
    rows, dates = _period_fixture()
    repository = ETFHoldingsRepository(str(tmp_path / "period-api.db"))
    repository.save_response(
        _payload_from_rows(rows),
        date_from=dates[0],
        date_to=dates[-1],
        sdk_version="2.2.9",
    )
    service = ETFHoldingsService(repository)
    monkeypatch.setattr(main, "_etf_holdings_service", service)
    client = TestClient(main.app)

    responses = {
        period: client.get(
            f"/api/etf/holdings?symbol=00981A&period={period}"
        )
        for period in (5, 10, 20)
    }
    assert all(response.status_code == 200 for response in responses.values())
    bodies = {period: response.json() for period, response in responses.items()}
    targets = {
        period: next(
            row for row in body["holdings"] if row["stockSymbol"] == "TARGET"
        )
        for period, body in bodies.items()
    }
    assert [bodies[p]["selectedPeriod"] for p in (5, 10, 20)] == [5, 10, 20]
    assert [bodies[p]["analysisWindow"]["usedDisclosureDateCount"] for p in (5, 10, 20)] == [5, 10, 20]
    assert [(targets[p]["activeAddCount"], targets[p]["activeReduceCount"]) for p in (5, 10, 20)] == [(2, 0), (3, 1), (3, 3)]
    assert len({targets[p]["convictionScore"] for p in (5, 10, 20)}) == 3
    assert len({targets[p]["intent"] for p in (5, 10, 20)}) == 3

    detail = client.get("/api/etf/holdings/00981A/stocks/TARGET?period=10")
    assert detail.status_code == 200
    assert detail.json()["stock"]["analysisPeriod"] == 10

    with repository.connect() as conn:
        stored_periods = [
            row["analysis_period"]
            for row in conn.execute(
                """
                SELECT analysis_period FROM etf_signals
                WHERE etf_symbol='00981A' AND stock_symbol='TARGET'
                ORDER BY analysis_period
                """
            ).fetchall()
        ]
    assert stored_periods == [5, 10, 20]


def test_api_empty_and_upstream_error_do_not_crash(monkeypatch, tmp_path):
    service = ETFHoldingsService(ETFHoldingsRepository(str(tmp_path / "empty.db")))
    monkeypatch.setattr(main, "_etf_holdings_service", service)
    monkeypatch.setattr(main, "api", _FakeETFClient(error=RuntimeError("permission denied")))
    monkeypatch.setattr(main, "is_logged_in", True)
    client = TestClient(main.app)

    response = client.get("/api/etf/holdings?symbol=00981A")
    assert response.status_code == 503
    assert "permission denied" in response.json()["detail"]
