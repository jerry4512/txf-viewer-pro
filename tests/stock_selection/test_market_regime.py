import tomorrow_strategy as ts


def test_market_regime_is_valid_on_exact_as_of_date(market_frame, fixture_spec):
    result = ts.calculate_market_regime(market_frame, as_of_date=fixture_spec["as_of_date"])
    assert result["strategy_valid"] is True
    assert result["status"] != ts.REGIME_DATA_INVALID
    assert result["metrics"]["data_available"] is True


def test_market_regime_fails_closed_on_date_mismatch(market_frame):
    result = ts.calculate_market_regime(market_frame, as_of_date="2026-04-13")
    assert result["strategy_valid"] is False
    assert result["status"] == ts.REGIME_DATA_INVALID
    assert result["metrics"]["regime_error"] is True


def test_market_regime_fails_closed_when_bars_are_insufficient(market_frame, fixture_spec):
    result = ts.calculate_market_regime(market_frame.tail(20), as_of_date=fixture_spec["as_of_date"])
    assert result["strategy_valid"] is False
    assert result["status"] == ts.REGIME_DATA_INVALID

