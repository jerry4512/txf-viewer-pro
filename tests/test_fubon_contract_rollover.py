from datetime import datetime

import pytest

import fubon_market_data as market


@pytest.mark.parametrize("root", ["TXF", "MXF", "TMF"])
@pytest.mark.parametrize("hour,minute,expected", [(13, 29, "I6"), (13, 30, "J6"), (22, 0, "J6")])
def test_expiry_day_front_month(root, hour, minute, expected, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 16, hour, minute, tzinfo=market.TAIPEI)

    monkeypatch.setattr(market, "datetime", Clock)
    client = market.FubonMarketDataClient()
    client.rest = object()
    monkeypatch.setattr(client, "_query_tickers", lambda _: [
        {"symbol": root + suffix, "endDate": expiry}
        for suffix, expiry in [("I6", "2026-09-16"), ("J6", "2026-10-21"), ("K6", "2026-11-18")]
    ])
    assert client.resolve_contract(root + "R1").target_code == root + expected
    assert client.resolve_contract(root + "R2").target_code == root + ("J6" if expected == "I6" else "K6")


def test_cached_second_month_rolls_across_cutoff(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 16, 13, 31, tzinfo=market.TAIPEI)

    monkeypatch.setattr(market, "datetime", Clock)
    monkeypatch.setattr(market.time, "monotonic", lambda: 1000)
    client = market.FubonMarketDataClient()
    client.rest = object()
    client._contracts["TXFR2"] = (880, market.FubonContract(
        "TXFR2", "TXFJ6", "TXFJ6", delivery_date="2026-10-21"
    ))
    monkeypatch.setattr(client, "_query_tickers", lambda _: [
        {"symbol": "TXFJ6", "endDate": "2026-10-21"},
        {"symbol": "TXFK6", "endDate": "2026-11-18"},
    ])
    assert client.resolve_contract("TXFR2").target_code == "TXFK6"
