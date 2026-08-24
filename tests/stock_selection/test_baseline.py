import json
from pathlib import Path


FIXTURES = Path(__file__).parent / "fixtures"


def test_pre_v2_regression_baseline_is_versioned():
    baseline = json.loads(
        (FIXTURES / "pre_v2_integrated_output.json").read_text(encoding="utf-8")
    )
    assert baseline["baseline_commit"] == "55036b328d84fb61ea62196764956d5f541f41cc"
    assert baseline["summary"]["buy_count"] == 10
    assert len(baseline["buy_candidates"]) == 10
    assert sum(row["risk_reward"] is None for row in baseline["buy_candidates"]) == 7


def test_fixed_database_fixture_spec_is_versioned():
    spec = json.loads(
        (FIXTURES / "fixed_db_fixture.json").read_text(encoding="utf-8")
    )
    assert spec["fixture_version"] == 1
    assert spec["bars"] >= 62
    assert any(s["code"] == "2945" for s in spec["securities"])


def test_post_milestone_snapshot_records_fail_closed_result():
    after = json.loads(
        (FIXTURES / "post_v2_m1_regression_output.json").read_text(encoding="utf-8")
    )
    production = after["production_as_of_2026_08_10"]
    assert production["strategy_valid"] is False
    assert production["summary"]["buy_count"] == 0
