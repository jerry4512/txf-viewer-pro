import pandas as pd
import pytest

import weekly_kd_screener as weekly_kd


@pytest.mark.parametrize("name", ["鮮活果汁-KY", "泰金寶-DR", "測試－KY", "測試－DR"])
def test_ky_and_dr_names_are_detected(name):
    assert weekly_kd.is_ky_or_dr_stock_name(name) is True


@pytest.mark.parametrize("name", ["台積電", "KY食品", "DRAM概念"])
def test_regular_names_are_not_misclassified_as_ky_or_dr(name):
    assert weekly_kd.is_ky_or_dr_stock_name(name) is False


def test_screening_universe_excludes_ky_and_dr_codes():
    bars = pd.DataFrame([
        {"code": "2330", "name": "台積電", "category": "半導體業", "date": "2026-08-14", "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000},
        {"code": "1256", "name": "鮮活果汁-KY", "category": "食品工業", "date": "2026-08-14", "open": 100, "high": 110, "low": 95, "close": 105, "volume": 1000},
        {"code": "9103", "name": "美德醫療-DR", "category": "其他", "date": "2026-08-14", "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000},
    ])

    result = weekly_kd.screen_daily_bars(bars)

    assert result["universe_count"] == 1
    assert result["excluded_ky_dr_count"] == 2
    assert result["insufficient_count"] == 1


def test_calculate_weekly_kd_keeps_neutral_value_for_flat_midpoint_prices():
    index = pd.date_range("2026-01-02", periods=12, freq="W-FRI")
    weekly = pd.DataFrame(
        {"high": [100.0] * 12, "low": [0.0] * 12, "close": [50.0] * 12},
        index=index,
    )

    result = weekly_kd.calculate_weekly_kd(weekly)
    valid = result.dropna(subset=["k", "d"])

    assert len(valid) == 4
    assert valid["rsv"].tolist() == [50.0] * 4
    assert valid["k"].tolist() == [50.0] * 4
    assert valid["d"].tolist() == [50.0] * 4


def test_low_passivation_requires_latest_three_low_weeks_and_upward_cross():
    kd_bars = pd.DataFrame({
        "k": [17.0, 15.0, 13.0, 16.0],
        "d": [18.0, 16.0, 14.0, 15.0],
    })

    assert weekly_kd.matches_low_passivation_golden_cross(kd_bars) is True
    assert weekly_kd.consecutive_low_weeks(kd_bars) == 4


def test_low_passivation_rejects_cross_without_three_low_weeks():
    kd_bars = pd.DataFrame({
        "k": [31.0, 24.0, 13.0, 16.0],
        "d": [32.0, 25.0, 14.0, 15.0],
    })

    assert weekly_kd.matches_low_passivation_golden_cross(kd_bars) is False


def test_low_passivation_rejects_low_zone_without_new_cross():
    kd_bars = pd.DataFrame({
        "k": [17.0, 15.0, 14.5, 14.0],
        "d": [18.0, 16.0, 15.0, 14.5],
    })

    assert weekly_kd.matches_low_passivation_golden_cross(kd_bars) is False


def test_average_volume_filter_is_strictly_greater_than_500_lots():
    dates = pd.date_range("2026-07-20", periods=20, freq="B")
    at_threshold = pd.DataFrame({"date": dates, "volume": [500] * 20})
    above_threshold = pd.DataFrame({"date": dates, "volume": [501] * 20})

    exact = weekly_kd.calculate_average_volume_lots(
        at_threshold,
        session_dates=dates,
    )
    above = weekly_kd.calculate_average_volume_lots(
        above_threshold,
        session_dates=dates,
    )

    assert exact["avg_volume_20d"] == 500
    assert weekly_kd.passes_average_volume_filter(exact, 500) is False
    assert above["avg_volume_20d"] == 501
    assert weekly_kd.passes_average_volume_filter(above, 500) is True


def test_average_volume_counts_missing_market_sessions_as_zero_lots():
    dates = pd.date_range("2026-07-20", periods=20, freq="B")
    bars = pd.DataFrame({"date": dates[1:], "volume": [520] * 19})

    result = weekly_kd.calculate_average_volume_lots(bars, session_dates=dates)

    assert result["avg_volume_20d"] == 494
    assert result["volume_window_complete"] is True
    assert weekly_kd.passes_average_volume_filter(result, 500) is False
