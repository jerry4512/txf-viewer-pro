"""Regression tests for historical-average TXF amplitude levels."""

import unittest

import pandas as pd

from main import _calculate_amplitude_levels, _txf_amplitude_time_mask


class TxfAmplitudeLevelTests(unittest.TestCase):
    def test_afternoon_window_uses_close_stamped_one_minute_bars(self):
        timestamps = pd.Series(pd.to_datetime([
            "2026-08-05 15:00:00+00:00",
            "2026-08-05 15:01:00+00:00",
            "2026-08-05 21:30:00+00:00",
            "2026-08-05 21:31:00+00:00",
        ]))

        mask = _txf_amplitude_time_mask(timestamps, "all", "afternoon")

        self.assertEqual(mask.tolist(), [False, True, True, False])

    def test_afternoon_max_includes_2130_close_bar(self):
        timestamps = pd.Series(pd.to_datetime([
            "2026-07-30 15:01:00+00:00",
            "2026-07-30 21:29:00+00:00",
            "2026-07-30 21:30:00+00:00",
        ]))
        highs = pd.Series([40033, 41077, 41090])
        lows = pd.Series([39866, 40950, 40980])

        mask = _txf_amplitude_time_mask(timestamps, "all", "afternoon")
        amplitude = highs[mask].max() - lows[mask].min()

        self.assertEqual(amplitude, 1224)

    def test_night_window_is_2130_to_next_day_0500(self):
        timestamps = pd.Series(pd.to_datetime([
            "2026-08-05 21:30:00+00:00",
            "2026-08-05 21:31:00+00:00",
            "2026-08-05 23:59:00+00:00",
            "2026-08-06 00:00:00+00:00",
            "2026-08-06 05:00:00+00:00",
            "2026-08-06 05:01:00+00:00",
        ]))

        mask = _txf_amplitude_time_mask(timestamps, "all", "night")

        self.assertEqual(
            mask.tolist(),
            [False, True, True, True, True, False],
        )

    def test_day_session_minimum_comes_from_completed_history(self):
        completed_days = [
            1237, 847, 1646, 1565, 2628, 1111, 907, 859, 894, 751,
            1207, 1043, 1985, 1057, 1067, 1612, 1127, 827, 720, 1588,
        ]

        result = _calculate_amplitude_levels(completed_days)

        self.assertEqual(result["amp_min"], 720)
        self.assertEqual(result["amp_avg"], 1234)

    def test_all_levels_use_completed_history_only(self):
        historical = [1000 + index * 10 for index in range(20)]
        result = _calculate_amplitude_levels(historical)

        self.assertEqual(result["amp_avg"], 1095)
        self.assertEqual(result["amp_min"], 1000)
        self.assertEqual(result["amp_max"], 1190)
        self.assertEqual(result["days"], 20)

    def test_large_and_small_keep_existing_midpoint_formula(self):
        result = _calculate_amplitude_levels([800, 1200, 2000])

        self.assertEqual(result["amp_avg"], 1333)
        self.assertEqual(result["amp_large"], 1667)
        self.assertEqual(result["amp_small"], 1067)


if __name__ == "__main__":
    unittest.main(verbosity=2)
