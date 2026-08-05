"""Offline contract/candle tests for the read-only Fubon futures adapter."""

import unittest
from datetime import datetime
from types import SimpleNamespace

from fubon_market_data import FubonContract, FubonMarketDataClient, TAIPEI


class FakeIntraday:
    def __init__(self):
        self.candle_calls = []

    def tickers(self, **params):
        self.last_ticker_params = params
        year = datetime.now(TAIPEI).year
        return {
            "data": [
                {
                    "symbol": "TXFA0",
                    "name": "expired",
                    "endDate": "2020-01-01",
                },
                {
                    "symbol": "TXFH1",
                    "name": "TXF near",
                    "endDate": f"{year + 1}-03-17",
                    "referencePrice": 20100,
                },
                {
                    "symbol": "TXFJ1",
                    "name": "TXF next",
                    "endDate": f"{year + 1}-04-21",
                    "referencePrice": 20200,
                },
            ]
        }

    def candles(self, **params):
        self.candle_calls.append(params)
        if params.get("session") == "afterhours":
            return {
                "data": [{
                    "date": "2026-08-03T15:00:00+08:00",
                    "open": 21000,
                    "high": 21020,
                    "low": 20990,
                    "close": 21010,
                    "volume": 25,
                }]
            }
        return {
            "data": [{
                "date": "2026-08-03T08:45:00+08:00",
                "open": 20800,
                "high": 20820,
                "low": 20790,
                "close": 20810,
                "volume": 10,
            }]
        }


class FakeStockIntraday:
    def ticker(self, **params):
        return {
            "date": "2026-08-04",
            "type": "EQUITY",
            "exchange": "TWSE",
            "market": "TSE",
            "symbol": params["symbol"],
            "name": "台積電",
            "referencePrice": 1000,
        }

    def quote(self, **params):
        return {
            "symbol": params["symbol"],
            "name": "台積電",
            "referencePrice": 1000,
            "openPrice": 1010,
            "highPrice": 1020,
            "lowPrice": 1005,
            "closePrice": 1015,
            "avgPrice": 1012.5,
            "lastSize": 8,
            "lastUpdated": 1785778200000000,
            "total": {"tradeVolume": 12345},
        }

    def candles(self, **params):
        self.last_candle_params = params
        return {
            "date": "2026-08-04",
            "symbol": params["symbol"],
            "data": [{
                "date": "2026-08-04T09:00:00+08:00",
                "open": 1010,
                "high": 1015,
                "low": 1008,
                "close": 1012,
                "volume": 321,
                "average": 1011.5,
            }],
        }


class FakeWebSocket:
    def __init__(self):
        self.subscriptions = []
        self.unsubscriptions = []

    def subscribe(self, payload):
        self.subscriptions.append(dict(payload))

    def unsubscribe(self, payload):
        self.unsubscriptions.append(dict(payload))


class FubonMarketDataTests(unittest.TestCase):
    def setUp(self):
        self.intraday = FakeIntraday()
        self.client = FubonMarketDataClient()
        self.client.rest = SimpleNamespace(intraday=self.intraday)
        self.stock_intraday = FakeStockIntraday()
        self.client.stock_rest = SimpleNamespace(intraday=self.stock_intraday)

    def test_rolling_contract_resolves_nearest_nonexpired_month(self):
        contract = self.client.resolve_contract("TXFR1")
        self.assertIsNotNone(contract)
        self.assertEqual(contract.code, "TXFR1")
        self.assertEqual(contract.target_code, "TXFH1")
        next_contract = self.client.resolve_contract("TXFR2")
        self.assertIsNotNone(next_contract)
        self.assertEqual(next_contract.code, "TXFR2")
        self.assertEqual(next_contract.target_code, "TXFJ1")
        self.assertEqual(self.intraday.last_ticker_params["exchange"], "TAIFEX")

    def test_day_and_night_candles_are_merged_and_close_stamped(self):
        contract = self.client.resolve_contract("TXFR1")
        bars = self.client.kbars(
            contract=contract,
            start="2026-08-03",
            end="2026-08-03",
        )
        self.assertEqual(len(bars.ts), 2)
        self.assertEqual(bars.Close, [20810.0, 21010.0])
        self.assertEqual(bars.Volume, [10, 25])
        self.assertEqual(
            [call.get("session") for call in self.intraday.candle_calls],
            [None, "afterhours"],
        )
        # 舊快取格式以 K 棒結束時間標記，所以 08:45 的棒會存成 08:46。
        first_wallclock = datetime.utcfromtimestamp(bars.ts[0] / 1_000_000_000)
        self.assertEqual(first_wallclock.strftime("%H:%M"), "08:46")

    def test_websocket_subscribes_day_and_night_without_auth_duplicate(self):
        ws = FakeWebSocket()
        self.client.ws = ws
        contract = FubonContract(
            code="TXFR1",
            target_code="TXFH1",
            symbol="TXFH1",
        )
        self.client.subscribe_contract(contract)

        expected = {
            (channel, after_hours)
            for channel in ("trades", "aggregates", "candles")
            for after_hours in (False, True)
        }
        actual = {
            (item["channel"], item["afterHours"])
            for item in ws.subscriptions
        }
        self.assertEqual(actual, expected)
        self.assertEqual(len(ws.subscriptions), 6)

        self.client._handle_message({"event": "authenticated", "data": {}})
        self.assertEqual(len(ws.subscriptions), 6)

        for index, item in enumerate(ws.subscriptions):
            self.client._handle_message({
                "event": "subscribed",
                "data": {
                    "id": f"subscription-{index}",
                    "channel": item["channel"],
                    "symbol": item["symbol"],
                },
            })
        self.client.resubscribe_all()
        self.assertEqual(len(ws.unsubscriptions), 1)
        self.assertEqual(len(ws.unsubscriptions[0]["ids"]), 6)
        self.assertEqual(len(ws.subscriptions), 12)

    def test_stock_contract_quote_and_candles_use_official_stock_client(self):
        contract = self.client.resolve_stock_contract("2330")
        self.assertIsNotNone(contract)
        self.assertEqual(contract.security_type, "STK")
        self.assertEqual(contract.reference, 1000.0)

        bars = self.client.kbars(
            contract=contract,
            start="2026-08-04",
            end="2026-08-04",
        )
        self.assertEqual(bars.Close, [1012.0])
        self.assertEqual(bars.Volume, [321])
        self.assertEqual(bars.Average, [1011.5])
        self.assertEqual(self.stock_intraday.last_candle_params["sort"], "asc")

        snapshots = self.client.snapshots([contract])
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].close, 1015.0)
        self.assertEqual(snapshots[0].avg_price, 1012.5)
        self.assertEqual(snapshots[0].total_volume, 12345)

    def test_stock_websocket_normalizes_trades_and_candles(self):
        ws = FakeWebSocket()
        self.client.stock_ws = ws
        contract = self.client.resolve_stock_contract("2330")
        ticks = []
        candles = []
        self.client.set_callbacks(
            stock_tick=ticks.append,
            stock_candle=candles.append,
        )
        self.client.subscribe_stock(contract)
        self.assertEqual(
            {item["channel"] for item in ws.subscriptions},
            {"trades", "candles"},
        )

        self.client._handle_stock_message({
            "event": "data",
            "channel": "trades",
            "data": {
                "symbol": "2330",
                "price": 1015,
                "size": 8,
                "volume": 12345,
                "time": 1785778200000000,
                "serial": 99,
            },
        })
        self.client._handle_stock_message({
            "event": "data",
            "channel": "candles",
            "data": {
                "symbol": "2330",
                "date": "2026-08-04T09:01:00+08:00",
                "open": 1012,
                "high": 1016,
                "low": 1011,
                "close": 1015,
                "volume": 8,
                "average": 1012.5,
            },
        })
        self.assertEqual(ticks[0]["code"], "2330")
        self.assertEqual(ticks[0]["volume"], 8)
        self.assertEqual(ticks[0]["total_volume"], 12345)
        self.assertEqual(candles[0]["code"], "2330")
        self.assertGreater(candles[0]["ts"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
