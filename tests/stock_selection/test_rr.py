import pytest

import tomorrow_strategy as ts


def test_rr_uses_previous_60d_high_and_calculates_max_entry():
    result = ts._calculate_rr_metrics(100, 90, 120, 125)
    assert result["target_price"] == 120
    assert result["current_60d_high"] == 125
    assert result["risk_reward"] == 2.0
    assert result["rr_buyable"] is True
    assert result["max_entry_rr15"] == 102.0


def test_breakout_has_no_fake_rr_and_is_not_buyable():
    result = ts._calculate_rr_metrics(120, 110, 120, 123)
    assert result["target_status"] == "breakout_no_defined_target"
    assert result["risk_reward"] is None
    assert result["rr_valid"] is False
    assert result["rr_buyable"] is False
    assert result["max_entry_rr15"] is None


def test_actual_entry_above_max_is_skipped():
    assert ts.evaluate_actual_entry(102.01, 102.0) is True
    assert ts.evaluate_actual_entry(102.0, 102.0) is False
    assert ts.evaluate_actual_entry(100, None) is True
    assert ts.evaluate_actual_entry(None, 102.0) is None


def _candidate(rr, valid, buyable):
    return {
        "grade": "A", "close": 105, "cost_20": 103, "cost_60": 100,
        "high_vol_upper_shadow": False, "dist_cost20_pct": 1.94,
        "macd_neg_expanding": False, "down_vol": False,
        "rr_valid": valid, "rr_buyable": buyable, "risk_reward": rr,
        "macd_neg_converging": True, "macd_pos_expanding": False,
        "vol_shrinking": True, "volume_status": "量縮",
        "is_near_60d_high": False, "stop_price": 100,
        "target_status": "defined" if valid else "target_unavailable",
    }


def test_rr_none_cannot_enter_buy_candidates():
    category, *_ = ts._classify_candidate(
        _candidate(None, False, False),
        {"status": ts.REGIME_HEALTHY_PB},
    )
    assert category != "明日可買"


def test_rr_at_threshold_can_enter_buy_candidates():
    category, *_ = ts._classify_candidate(
        _candidate(1.5, True, True),
        {"status": ts.REGIME_HEALTHY_PB},
    )
    assert category == "明日可買"

