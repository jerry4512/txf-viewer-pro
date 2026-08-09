"""Offline tests for TWSE TAIEX quote normalization."""

import unittest
from datetime import datetime, timedelta, timezone

from main import (
    _twse_snapshot_to_intraday_payload,
    _twse_taiex_row_to_quote,
    _weighted_stock_reference,
)


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

    def test_twse_stock_reference_overrides_stale_broker_value(self):
        reference = _weighted_stock_reference(
            "2330",
            {"2330": {"y": "2405.0000"}},
            2320,
        )
        self.assertEqual(reference, 2405.0)

    def test_weighted_stock_payload_keeps_official_reference(self):
        payload = _twse_snapshot_to_intraday_payload(
            "2330",
            "台積電",
            {
                "d": "20260806",
                "t": "09:27:55",
                "o": "2395.0000",
                "h": "2395.0000",
                "l": "2370.0000",
                "z": "2375.0000",
                "y": "2405.0000",
                "v": "6223",
            },
        )
        self.assertEqual(payload["reference"], 2405.0)
        self.assertAlmostEqual(payload["change_pct"], -1.2474012474)


if __name__ == "__main__":
    unittest.main(verbosity=2)
