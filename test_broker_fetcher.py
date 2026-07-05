import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import broker_analysis
import broker_fetcher
import main


class BrokerFetcherApiTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix='.db')
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE stock_names (code TEXT, name TEXT, category TEXT)')
        self.conn.execute('CREATE TABLE daily_kbars (code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER)')
        self.conn.execute('INSERT INTO stock_names VALUES (?, ?, ?)', ('6693', 'TestStock', '上市'))
        for i in range(10):
            day = 17 + i
            self.conn.execute(
                'INSERT INTO daily_kbars VALUES (?, ?, ?, ?, ?, ?, ?)',
                ('6693', f'2026-06-{day:02d}', 10, 11, 9, 10 + i, 1000),
            )
        broker_analysis.ensure_broker_tables(self.conn)
        self.conn.close()
        self.old_db_path = main._STOCK_DB_PATH
        main._STOCK_DB_PATH = self.db_path

    def tearDown(self):
        main._STOCK_DB_PATH = self.old_db_path
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def _run_api(self, query='6693'):
        return asyncio.run(main.api_broker_key_points(query=query))

    def _seed_all_recent_broker_dates(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        broker_analysis.ensure_broker_tables(conn)
        for i in range(10):
            day = 17 + i
            conn.execute(
                '''
                INSERT INTO broker_trading_daily
                    (code, date, broker_name, branch_name, buy_qty, sell_qty, net_qty, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                ('6693', f'2026-06-{day:02d}', 'BrokerA', 'Main', 100, 20, 80, 'test'),
            )
        conn.commit()
        conn.close()

    def test_existing_data_does_not_fetch(self):
        self._seed_all_recent_broker_dates()
        with patch('broker_fetcher.fetch_broker_data_for_stock') as mocked_fetch:
            data = self._run_api()
        self.assertFalse(mocked_fetch.called)
        self.assertEqual(data['fetch_status'], 'not_needed')
        self.assertTrue(data['available'])

    def test_missing_data_fetch_success_writes_and_analyzes(self):
        def fake_fetch(conn, code, days=10):
            rows = []
            for d, net in [('2026-06-22', 400), ('2026-06-23', 450), ('2026-06-24', 300), ('2026-06-25', 250), ('2026-06-26', 270)]:
                rows.append({
                    'code': code,
                    'date': d,
                    'broker_name': 'BrokerA',
                    'branch_name': 'Main',
                    'buy_qty': max(net, 0),
                    'sell_qty': max(-net, 0),
                    'net_qty': net,
                    'source': 'test',
                })
            broker_fetcher.upsert_fetched_broker_rows(conn, rows)
            return {'status': 'success', 'message': '已自動補齊 5 個交易日分點資料', 'rows': len(rows)}

        with patch('broker_fetcher.fetch_broker_data_for_stock', side_effect=fake_fetch):
            data = self._run_api('TestStock')
        self.assertEqual(data['fetch_status'], 'success')
        self.assertTrue(data['available'])
        self.assertGreaterEqual(len(data['key_brokers']), 1)

    def test_fetch_failure_does_not_raise_500(self):
        with patch('broker_fetcher.fetch_broker_data_for_stock', side_effect=RuntimeError('network down')):
            data = self._run_api()
        self.assertEqual(data['status'], 'success')
        self.assertEqual(data['fetch_status'], 'failed')
        self.assertFalse(data['available'])

    def test_one_day_broker_data_reports_incomplete_days_and_normalized_volume_ratio(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        broker_analysis.ensure_broker_tables(conn)
        conn.execute(
            '''
            INSERT INTO broker_trading_daily
                (code, date, broker_name, branch_name, buy_qty, sell_qty, net_qty, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            ('6693', '2026-06-26', 'BrokerA', 'Main', 600000, 500000, 100000, 'test_csv'),
        )
        conn.commit()
        result = broker_analysis.analyze_key_brokers(conn, '6693')
        conn.close()

        summary = result['summary']
        self.assertTrue(result['available'])
        self.assertEqual(summary['available_days_5d'], 1)
        self.assertEqual(summary['available_days_10d'], 1)
        self.assertIn('少於 3 個交易日', summary['data_completeness_warning'])
        self.assertEqual(summary['volume_unit'], 'lots')
        self.assertEqual(result['top_buy_brokers_5d'][0]['volume_ratio_5d'], 10.0)
        self.assertLess(result['top_buy_brokers_5d'][0]['volume_ratio_5d'], 100)

    def test_frontend_has_partial_period_labels_for_incomplete_broker_data(self):
        static_path = os.path.join(os.path.dirname(__file__), 'static', 'app_pro.js')
        with open(static_path, encoding='utf-8') as fh:
            app_js = fh.read()
        self.assertIn('updateBrokerPeriodLabels', app_js)
        self.assertIn('區間淨買賣', app_js)
        self.assertIn('${safeDays}/${target}D', app_js)

    def test_unknown_stock_keeps_error_behavior(self):
        data = self._run_api('NO_SUCH_STOCK')
        self.assertEqual(data['status'], 'error')

    def test_existing_stock_routes_are_still_registered(self):
        paths = {route.path for route in main.app.routes}
        self.assertIn('/api/institutional_rankings', paths)
        self.assertIn('/api/industry_rankings', paths)
        self.assertIn('/api/integrated-strategy', paths)
        self.assertIn('/api/broker/key-points', paths)


if __name__ == '__main__':
    unittest.main()
