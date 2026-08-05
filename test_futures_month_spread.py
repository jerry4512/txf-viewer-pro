"""Offline tests for TXF next-month-minus-near spread state."""

import unittest

import main
from fubon_market_data import FubonContract


class FuturesMonthSpreadTests(unittest.TestCase):
    def setUp(self):
        self.previous_contract = main.contract
        self.near = FubonContract(
            code="TXFR1",
            target_code="TXFH6",
            symbol="TXFH6",
            delivery_date="2026-08-19",
        )
        self.far = FubonContract(
            code="TXFR2",
            target_code="TXFJ6",
            symbol="TXFJ6",
            delivery_date="2026-09-16",
        )
        main._reset_futures_month_spread_state(self.near, self.far)

    def tearDown(self):
        main.contract = self.previous_contract
        main._reset_futures_month_spread_state()

    def test_spread_is_next_month_minus_near(self):
        main._update_futures_month_spread_quote({
            "code": "TXFR1",
            "target_code": "TXFH6",
            "close": 44320.50,
            "ts": 1785893250,
        }, "test")
        payload = main._update_futures_month_spread_quote({
            "code": "TXFR2",
            "target_code": "TXFJ6",
            "close": 44420.75,
            "ts": 1785893251,
        }, "test")

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["formula"], "TXFR2-TXFR1")
        self.assertAlmostEqual(payload["spread"], 100.25)
        self.assertEqual(payload["near"]["target_code"], "TXFH6")
        self.assertEqual(payload["far"]["target_code"], "TXFJ6")

    def test_next_month_tick_does_not_match_selected_near_month(self):
        main.contract = self.near
        self.assertTrue(main._quote_matches_selected_contract({"code": "TXFR1"}))
        self.assertTrue(main._quote_matches_selected_contract({"code": "TXFH6"}))
        self.assertFalse(main._quote_matches_selected_contract({"code": "TXFR2"}))
        self.assertFalse(main._quote_matches_selected_contract({"code": "TXFJ6"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
