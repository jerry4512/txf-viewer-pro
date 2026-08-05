"""Regression tests for current TXF near-month lifecycle statistics."""

import unittest
from datetime import datetime, timezone

from main import (
    _calculate_txf_contract_stats,
    _txf_contract_cycle_start,
)


def _wallclock_ns(text: str) -> int:
    value = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(
        tzinfo=timezone.utc
    )
    return int(value.timestamp() * 1_000_000_000)


class TxfContractStatsTests(unittest.TestCase):
    def test_cycle_starts_at_prior_month_third_wednesday_night_open(self):
        self.assertEqual(
            _txf_contract_cycle_start("2026-08-19"),
            datetime(2026, 7, 15, 15, 0),
        )
        self.assertEqual(
            _txf_contract_cycle_start("2027-01-20"),
            datetime(2026, 12, 16, 15, 0),
        )

    def test_all_session_uses_entire_current_contract_cycle(self):
        rows = [
            (_wallclock_ns("2026-07-15 15:01"), 40000, 40100, 39900, 40050),
            (_wallclock_ns("2026-07-16 08:46"), 40200, 40400, 40150, 40300),
            (_wallclock_ns("2026-07-16 13:45"), 40300, 40500, 40000, 40450),
            (_wallclock_ns("2026-07-16 15:01"), 40350, 40600, 40200, 40500),
        ]
        result = _calculate_txf_contract_stats(rows, "all")
        self.assertEqual(result["contract_open"], 40000)
        self.assertEqual(result["contract_high"], 40600)
        self.assertEqual(result["contract_low"], 39900)
        self.assertEqual(result["contract_close"], 40500)
        self.assertEqual(result["contract_change"], 500)
        self.assertEqual(result["contract_change_pct"], 1.25)
        self.assertEqual(result["month_cost"], 40250)

    def test_day_session_excludes_night_bars(self):
        rows = [
            (_wallclock_ns("2026-07-15 15:01"), 40000, 40100, 39900, 40050),
            (_wallclock_ns("2026-07-16 08:46"), 40200, 40400, 40150, 40300),
            (_wallclock_ns("2026-07-16 13:45"), 40300, 40500, 40000, 40100),
            (_wallclock_ns("2026-07-16 15:01"), 40350, 40600, 39800, 39900),
        ]
        result = _calculate_txf_contract_stats(rows, "day")
        self.assertEqual(result["contract_open"], 40200)
        self.assertEqual(result["contract_high"], 40500)
        self.assertEqual(result["contract_low"], 40000)
        self.assertEqual(result["contract_close"], 40100)
        self.assertEqual(result["contract_change"], -100)
        self.assertEqual(result["month_cost"], 40250)
        self.assertEqual(result["bar_count"], 2)


if __name__ == "__main__":
    unittest.main()
