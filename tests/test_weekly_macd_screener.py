import pandas as pd

import weekly_macd_screener as weekly_macd


def test_constant_weekly_close_has_zero_macd():
    weekly = pd.DataFrame(
        {"close": [50.0] * 20},
        index=pd.date_range("2026-01-02", periods=20, freq="W-FRI"),
    )

    result = weekly_macd.calculate_weekly_macd(weekly)

    assert result["dif"].tolist() == [0.0] * 20
    assert result["signal"].tolist() == [0.0] * 20
    assert result["osc"].tolist() == [0.0] * 20


def test_bullish_macd_divergence_requires_price_lower_low_and_osc_higher_low():
    index = pd.date_range("2026-06-26", periods=8, freq="W-FRI")
    bars = pd.DataFrame({
        "low": [100, 95, 90, 96, 93, 87, 92, 94],
        "osc": [-0.2, -0.8, -2.0, -1.0, -0.7, -1.0, -0.4, -0.1],
    }, index=index)

    divergence = weekly_macd.find_bullish_macd_divergence(bars)

    assert divergence is not None
    assert divergence["previous_low_date"] == index[2].strftime("%Y-%m-%d")
    assert divergence["recent_low_date"] == index[5].strftime("%Y-%m-%d")
    assert divergence["price_lower_pct"] < -1
    assert divergence["recent_osc"] > divergence["previous_osc"]


def test_macd_divergence_rejects_lower_price_with_lower_osc():
    index = pd.date_range("2026-06-26", periods=8, freq="W-FRI")
    bars = pd.DataFrame({
        "low": [100, 95, 90, 96, 93, 87, 92, 94],
        "osc": [-0.2, -0.8, -1.0, -0.5, -0.7, -1.5, -0.4, -0.1],
    }, index=index)

    assert weekly_macd.find_bullish_macd_divergence(bars) is None


def test_macd_divergence_rejects_unconfirmed_latest_low():
    index = pd.date_range("2026-06-26", periods=7, freq="W-FRI")
    bars = pd.DataFrame({
        "low": [100, 95, 90, 96, 93, 92, 87],
        "osc": [-0.2, -0.8, -2.0, -1.0, -0.7, -0.4, -1.0],
    }, index=index)

    assert weekly_macd.find_bullish_macd_divergence(bars) is None

