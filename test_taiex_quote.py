"""Offline tests for TWSE TAIEX quote normalization."""

import unittest
from datetime import datetime, timedelta, timezone

from main import _twse_taiex_row_to_quote


class TaiexQuoteTests(unittest.TestCase):
    def test_live_taiex_quote_is_normalized(self):
        quote = _twse_taiex_row_to_quote({
            "d": "20260805",
            "t": "09:20:30",
            "tlong": "1785892830000",
            "n": "發行量加權股價指數",
            "z": "44421.73",
            "y": "43360.66",
            "o": "43809.83",
            "h": "44773.87",
            "l": "43809.83",
        })
        self.assertEqual(quote["price"], 44421.73)
        self.assertEqual(quote["reference"], 43360.66)
        self.assertAlmostEqual(quote["change"], 1061.07)
        self.assertEqual(quote["date"], "2026-08-05")
        self.assertEqual(quote["quote_time"], "09:20:30")
        self.assertTrue(quote["is_live_price"])
        taipei = timezone(timedelta(hours=8))
        self.assertEqual(
            datetime.fromtimestamp(quote["time"], taipei).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "2026-08-05 09:20:30",
        )

    def test_reference_is_used_before_first_trade(self):
        quote = _twse_taiex_row_to_quote({
            "d": "20260805",
            "t": "08:30:00",
            "z": "-",
            "y": "43360.66",
        })
        self.assertEqual(quote["price"], 43360.66)
        self.assertFalse(quote["is_live_price"])

    def test_missing_price_is_rejected(self):
        with self.assertRaises(RuntimeError):
            _twse_taiex_row_to_quote({"d": "20260805", "z": "-"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
