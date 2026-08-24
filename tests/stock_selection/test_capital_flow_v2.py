from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import pandas as pd

from capital_flow_v2 import (
    _actor_metrics,
    classify_flow_momentum,
    classify_flow_price_quadrant,
    compute_capital_flow_v2_shadow,
    score_capital_flow_shadow,
)
from stock_selection_schema import ensure_stock_selection_schema


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((10, 20, 30, 15, 0.03, 0.02), "accelerating"),
        ((5, 20, 30, 10, 0.01, 0.02), "decelerating"),
        ((-2, 5, 30, -4, -0.01, 0.01), "reversing_negative"),
        ((2, -5, -30, 4, 0.01, -0.01), "reversing_positive"),
        ((-5, -20, -30, -10, -0.01, -0.02), "stable"),
        ((0, 0, 0, 0, 0.0, 0.0), "inactive"),
    ],
)
def test_flow_momentum_states(args, expected):
    assert classify_flow_momentum(*args) == expected


def test_active_offsetting_flow_is_neutral_not_inactive():
    assert classify_flow_momentum(0, 0, 0, 0, 0.0, 0.0, 2) == "neutral"


def test_activity_uses_daily_flows_not_only_five_day_sum():
    dates = [f"2026-08-{day:02d}" for day in range(3, 8)]
    flows = pd.DataFrame({
        "date": dates,
        "trust_net": [1000, -1000, 0, 0, 0],
    })
    bars = pd.DataFrame({
        "date": dates,
        "close": [100.0] * 5,
        "volume": [1000.0] * 5,
    })
    metric = _actor_metrics("trust", flows, "trust_net", bars, dates)
    assert metric["trust_flow_5d_shares"] == 0
    assert metric["trust_active_days_5"] == 2
    assert metric["trust_flow_activity"] == "active"
    assert metric["trust_flow_direction"] == "zero"
    assert metric["trust_flow_momentum"] == "neutral"


@pytest.mark.parametrize(
    ("flow", "return_5d", "rs20", "quadrant", "state"),
    [
        (100, 2.0, 1.0, "Q1", "confirmed_accumulation"),
        (100, -1.0, 1.0, "Q2", "unconfirmed_accumulation"),
        (-100, 2.0, 1.0, "Q3", "absorption_divergence"),
        (-100, -1.0, -1.0, "Q4", "confirmed_distribution"),
    ],
)
def test_flow_price_quadrants(flow, return_5d, rs20, quadrant, state):
    assert classify_flow_price_quadrant(flow, return_5d, rs20) == (
        quadrant,
        state,
    )


def test_capital_flow_score_uses_capped_factor_buckets():
    metrics = {
        "foreign_flow_5d_shares": 100_000,
        "trust_flow_5d_shares": 50_000,
        "dealer_prop_flow_5d_shares": 10_000,
        "dealer_flow_detail_level": "split",
        "foreign_flow_ratio_5d": 0.20,
        "trust_flow_ratio_5d": 0.10,
        "foreign_positive_days_10": 10,
        "trust_positive_days_10": 10,
        "foreign_flow_momentum": "accelerating",
        "trust_flow_momentum": "accelerating",
        "foreign_flow_direction": "positive",
        "trust_flow_direction": "positive",
        "flow_price_quadrant": "Q1",
        "foreign_flow_intensity_percentile": 100,
        "trust_flow_intensity_percentile": 100,
        "foreign_flow_active_percentile_v21": 100,
        "trust_flow_active_percentile_v21": 100,
    }
    scores = score_capital_flow_shadow(metrics)
    assert scores == {
        "flow_identity_score": 10.0,
        "flow_intensity_score": 25.0,
        "flow_persistence_score": 10.0,
        "flow_momentum_score": 10.0,
        "flow_price_confirmation_score": 25.0,
        "flow_relative_score": 20.0,
    }
    assert sum(scores.values()) == 100.0


def test_negative_stable_flow_does_not_receive_positive_momentum_component():
    metrics = {
        "foreign_flow_5d_shares": -100_000,
        "trust_flow_5d_shares": 0,
        "dealer_prop_flow_5d_shares": 0,
        "dealer_flow_detail_level": "split",
        "foreign_flow_ratio_5d": -0.02,
        "trust_flow_ratio_5d": 0.0,
        "foreign_positive_days_10": 0,
        "trust_positive_days_10": 0,
        "foreign_flow_momentum": "stable",
        "trust_flow_momentum": "inactive",
        "foreign_flow_direction": "negative",
        "trust_flow_direction": "zero",
        "flow_price_quadrant": "Q4",
        "foreign_flow_active_percentile_v21": 80,
        "trust_flow_active_percentile_v21": None,
    }
    scores = score_capital_flow_shadow(metrics)
    assert scores["flow_momentum_score"] == -3.5
    assert scores["flow_momentum_score"] <= 0


def test_institution_schema_preserves_unknown_dealer_identity(fixed_db):
    conn = sqlite3.connect(fixed_db)
    ensure_stock_selection_schema(conn)
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(institutional_trading)")
    }
    assert {
        "foreign_net",
        "trust_net",
        "dealer_prop_net",
        "dealer_hedge_net",
        "dealer_unknown_net",
        "flow_detail_level",
        "flow_data_source",
    }.issubset(columns)
    row = conn.execute(
        "SELECT foreign_net,trust_net,dealer_prop_net,dealer_hedge_net,"
        "dealer_unknown_net,flow_detail_level FROM institutional_trading "
        "WHERE code='2945' AND date='2026-04-03'"
    ).fetchone()
    conn.close()
    assert row == (3300, 4200, None, None, 1100, "legacy_combined")


def test_shadow_metrics_are_point_in_time_and_use_full_common_stock_universe(
    fixed_db, fixture_spec
):
    conn = sqlite3.connect(fixed_db)
    result = compute_capital_flow_v2_shadow(conn, fixture_spec["as_of_date"])
    conn.close()

    assert result["errors"] == []
    assert result["universe_size"] == 1
    assert set(result["metrics_by_code"]) == {"2945"}
    metric = result["metrics_by_code"]["2945"]

    # Future 2026-04-13 rows (999,000 shares) must never leak into 2026-04-10.
    assert metric["foreign_flow_1d_shares"] == 6600
    assert metric["foreign_flow_3d_shares"] == 16_500
    assert metric["foreign_flow_5d_shares"] == 22_000
    assert metric["foreign_flow_10d_shares"] == 23_500
    assert metric["trust_flow_5d_shares"] == 26_500
    assert metric["foreign_positive_days_5"] == 5
    assert metric["foreign_consecutive_buy"] == 6
    assert metric["dealer_flow_detail_level"] == "legacy_combined"
    assert metric["dealer_prop_flow_5d_shares"] == 0
    assert metric["dealer_unknown_flow_5d_shares"] == 5700
    assert metric["capital_flow_v2_available"] is True
    assert 0 <= metric["capital_flow_score_v2_shadow"] <= 100
    assert 0 <= metric["capital_flow_score_v21_shadow"] <= 100
    assert metric["foreign_flow_activity"] == "active"
    assert metric["foreign_active_days_5"] == 5
    assert metric["foreign_flow_direction"] == "positive"
    assert metric["foreign_flow_active_percentile_v21"] == pytest.approx(100)
    assert metric["foreign_signed_flow_strength"] == pytest.approx(100)
    assert metric["rs_5d"] == pytest.approx(metric["rs5"])
    assert metric["rs_20d"] == pytest.approx(metric["rs20"])


def test_flow_ratio_and_amount_ratio_use_exact_shares_and_same_period_volume(
    fixed_db, fixture_spec
):
    conn = sqlite3.connect(fixed_db)
    result = compute_capital_flow_v2_shadow(conn, fixture_spec["as_of_date"])
    metric = result["metrics_by_code"]["2945"]
    dates = result["institutional_dates"][-5:]
    bars = {
        row[0]: (float(row[1]), float(row[2]))
        for row in conn.execute(
            "SELECT date,close,volume FROM daily_kbars WHERE code='2945' "
            "AND date<=?",
            (fixture_spec["as_of_date"],),
        )
    }
    flows = {
        row[0]: float(row[1])
        for row in conn.execute(
            "SELECT date,foreign_net FROM institutional_trading "
            "WHERE code='2945' AND date<=?",
            (fixture_spec["as_of_date"],),
        )
    }
    conn.close()

    volume_shares = sum(bars[date][1] * 1000 for date in dates)
    expected_ratio = sum(flows[date] for date in dates) / volume_shares
    expected_amount_ratio = sum(
        flows[date] * bars[date][0] for date in dates
    ) / sum(bars[date][1] * 1000 * bars[date][0] for date in dates)
    assert metric["foreign_flow_ratio_5d"] == pytest.approx(expected_ratio)
    assert metric["foreign_amount_ratio_5d"] == pytest.approx(
        expected_amount_ratio
    )


def test_shadow_fields_do_not_change_formal_score_or_classification():
    import integrated_strategy as strategy

    formal_stock = {
        "base_score_raw": 70,
        "institution_5d_total": 100,
        "foreign_5d": 50,
        "trust_5d": 50,
        "foreign_consecutive": 3,
        "trust_consecutive": 3,
        "chip_tier": "",
        "industry_score": 0,
        "liquidity_level": "正常",
        "broker_bonus": 0,
        "high_vol_upper_shadow": False,
        "down_vol": False,
        "macd_neg_expanding": False,
        "dist_cost20_pct": 0,
    }
    before = strategy._calculate_final_score(dict(formal_stock))
    with_shadow = dict(formal_stock)
    with_shadow.update({
        "capital_flow_score_v2_shadow": 100,
        "capital_flow_score_v21_shadow": 100,
        "rs20_percentile": 100,
        "flow_price_quadrant": "Q1",
    })
    after = strategy._calculate_final_score(with_shadow)
    assert after == before


def test_versioned_formal_before_after_regression_is_exact():
    fixtures = Path(__file__).parent / "fixtures"
    before = json.loads(
        (fixtures / "capital_flow_v2_m1_before.json").read_text(encoding="utf-8")
    )
    after = json.loads(
        (fixtures / "capital_flow_v2_m1_after.json").read_text(encoding="utf-8")
    )
    assert after["as_of_date"] == before["as_of_date"]
    assert after["formal_projection_fields"] == before["formal_projection_fields"]
    assert after["buckets"] == before["buckets"]


def test_official_institution_sync_preserves_identity_split(monkeypatch, tmp_path):
    import screener

    fields = [
        "證券代號",
        "證券名稱",
        "外陸資買進股數(不含外資自營商)",
        "外陸資賣出股數(不含外資自營商)",
        "外陸資買賣超股數(不含外資自營商)",
        "外資自營商買進股數",
        "外資自營商賣出股數",
        "外資自營商買賣超股數",
        "投信買進股數",
        "投信賣出股數",
        "投信買賣超股數",
        "自營商買賣超股數",
        "自營商買進股數(自行買賣)",
        "自營商賣出股數(自行買賣)",
        "自營商買賣超股數(自行買賣)",
        "自營商買進股數(避險)",
        "自營商賣出股數(避險)",
        "自營商買賣超股數(避險)",
    ]
    twse_row = ["2330", "台積電"] + [0] * 16
    twse_row[4] = "12,345"
    twse_row[7] = "111"
    twse_row[10] = "2,345"
    twse_row[11] = "-1,000"
    twse_row[14] = "500"
    twse_row[17] = "-1,500"

    tpex_row = ["6488", "環球晶"] + [0] * 21
    tpex_row[10] = "8,765"
    tpex_row[13] = "1,234"
    tpex_row[16] = "300"
    tpex_row[19] = "-200"
    tpex_row[22] = "100"

    payloads = {
        "T86": {"fields": fields, "data": [twse_row]},
        "3itrade": {"tables": [{"data": [tpex_row]}]},
    }

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, **_kwargs):
        url = request.full_url
        return FakeResponse(payloads["T86" if "T86" in url else "3itrade"])

    db_path = tmp_path / "official_sync.db"
    monkeypatch.setattr(screener, "DB_PATH", str(db_path))
    monkeypatch.setattr(screener.urllib.request, "urlopen", fake_urlopen)
    result = screener.sync_twse_institutional_data(
        screener.datetime(2026, 8, 10)
    )
    assert result == {
        "success": True,
        "date": "2026-08-10",
        "count": 2,
        "split_count": 2,
    }

    conn = sqlite3.connect(db_path)
    twse = conn.execute(
        "SELECT foreign_buy,investment_buy,dealer_buy,foreign_net,trust_net,"
        "dealer_prop_net,dealer_hedge_net,dealer_unknown_net,flow_detail_level "
        "FROM institutional_trading WHERE code='2330'"
    ).fetchone()
    tpex = conn.execute(
        "SELECT foreign_net,trust_net,dealer_prop_net,dealer_hedge_net,"
        "dealer_unknown_net,flow_detail_level FROM institutional_trading "
        "WHERE code='6488'"
    ).fetchone()
    conn.close()
    assert twse == (
        12, 2, -1, 12_456, 2_345, 500, -1_500, None, "split"
    )
    assert tpex == (8_765, 1_234, 300, -200, None, "split")
