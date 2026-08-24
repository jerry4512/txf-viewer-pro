import integrated_strategy as integrated


def _stock(tomorrow_category="高優先觀察", **overrides):
    value = {
        "tomorrow_category": tomorrow_category,
        "chip_bonus": 0, "industry_bonus": 0,
        "dist_cost20_pct": 0, "grade": "A",
        "risk_reward": 2, "is_near_60d_high": False,
        "close": 100, "stop_price": 97,
    }
    value.update(overrides)
    return value


def test_tomorrow_buy_and_exclude_keep_veto_priority():
    assert integrated._classify_final_category(_stock("明日可買"), {}) == "buy_candidates"
    assert integrated._classify_final_category(_stock("排除"), {}) == "excluded"


def test_extended_strong_chip_stock_becomes_wait_pullback():
    result = integrated._classify_final_category(
        _stock(dist_cost20_pct=9, chip_bonus=6), {}
    )
    assert result == "wait_pullback"


def test_invalid_tomorrow_result_fails_closed(monkeypatch):
    monkeypatch.setattr(
        integrated._ts,
        "run_tomorrow_strategy",
        lambda as_of_date=None: {
            "strategy_valid": False,
            "as_of_date": as_of_date,
            "stock_kbar_date": "2026-04-09",
            "market_regime": {"status": "data_invalid", "strategy_valid": False},
            "data_errors": ["date mismatch"],
        },
    )
    result = integrated.run_integrated_strategy(as_of_date="2026-04-10")
    assert result["strategy_valid"] is False
    assert result["buy_candidates"] == []
    assert result["high_priority_watch"] == []

