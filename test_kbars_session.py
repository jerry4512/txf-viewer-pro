"""Regression tests for day/all futures K-bar session selection."""

import unittest
from datetime import date

import pandas as pd

from main import (
    _drop_incomplete_recent_futures_dates,
    _filter_kbars_session,
)


class KbarSessionTests(unittest.TestCase):
    def _frame(self):
        night = pd.date_range(
            "2026-07-31 00:00:00", periods=304, freq="min", tz="UTC"
        )
        day = pd.date_range(
            "2026-07-31 08:46:00", periods=300, freq="min", tz="UTC"
        )
        index = night.append(day)
        return pd.DataFrame({"Close": range(len(index))}, index=index)

    def test_day_mode_selects_regular_session_only(self):
        result = _filter_kbars_session(self._frame(), "day")
        self.assertEqual(len(result), 300)
        self.assertEqual(result.index[0].strftime("%H:%M"), "08:46")
        self.assertEqual(result.index[-1].strftime("%H:%M"), "13:45")

    def test_day_quality_guard_does_not_require_complete_after_hours(self):
        day_only = _filter_kbars_session(self._frame(), "day").iloc[2:]
        kept, incomplete_dates, removed_count = (
            _drop_incomplete_recent_futures_dates(
                day_only, date(2026, 8, 5), session_mode="day"
            )
        )
        self.assertEqual(len(kept), 298)
        self.assertEqual(incomplete_dates, [])
        self.assertEqual(removed_count, 0)

    def test_all_session_guard_still_rejects_fragmented_date(self):
        fragmented = self._frame()
        kept, incomplete_dates, removed_count = (
            _drop_incomplete_recent_futures_dates(
                fragmented, date(2026, 8, 5), session_mode="all"
            )
        )
        self.assertTrue(kept.empty)
        self.assertEqual(incomplete_dates, ["2026-07-31"])
        self.assertEqual(removed_count, len(fragmented))


if __name__ == "__main__":
    unittest.main(verbosity=2)
