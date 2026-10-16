from copy import deepcopy
from pathlib import Path
import sys
import unittest
import tempfile
import csv
import gzip
import io
from unittest.mock import patch
from decimal import Decimal as D

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.engine import MINUTE
from perpquant.mark_proxy import fill_missing, deviation, load_policy, archive_funding, valued_funding
from perpquant.full_data import MonthlySpec
from perpquant.engine import stamp_us


class MarkProxyTests(unittest.TestCase):
    def fixture(self):
        start = 1723456800000000
        def row(t, close):
            return [str(t // 1000), str(close), str(close + 1), str(close - 1), str(close),
                    '0', str((t + MINUTE) // 1000 - 1), '0', '0', '0', '0', '0']
        spot = [row(start + i * MINUTE, 100 + i) for i in range(6)]
        missing = [start + 2 * MINUTE, start + 3 * MINUTE]
        mark = [row(start + i * MINUTE, 110 + i) for i in (0, 1, 4, 5)]
        return start, start + 6 * MINUTE, missing, mark, spot

    def test_spot_replaces_only_absent_rows_preserves_all_observed_marks(self):
        start, end, missing, mark, spot = self.fixture()
        original = deepcopy(mark)
        parsed, evidence = fill_missing(mark, spot, start, end, missing, 'spot')
        self.assertEqual(mark, original)
        self.assertEqual([float(x['close']) for x in parsed], [110, 111, 102, 103, 114, 115])
        self.assertEqual([x['open_time_us'] for x in evidence], missing)
        self.assertTrue(all(x['proxy_source'] == 'spot' and not x['exact_mark'] for x in evidence))
        self.assertEqual(evidence[0]['extrema_available_us'], missing[0] + MINUTE)

    def test_ffill_uses_same_actual_past_close_never_future_or_recursive_proxy(self):
        start, end, missing, mark, spot = self.fixture()
        parsed, evidence = fill_missing(mark, spot, start, end, missing, 'ffill')
        for i in (2, 3):
            self.assertEqual([float(parsed[i][k]) for k in ('open', 'high', 'low', 'close')], [111] * 4)
        self.assertEqual([x['source_mark_open_us'] for x in evidence], [start + MINUTE] * 2)
        self.assertEqual([x['age_at_open_us'] for x in evidence], [0, MINUTE])
        self.assertNotEqual(float(parsed[3]['close']), float(parsed[4]['close']))

    def test_extra_gap_rejected_instead_of_extending_whitelist(self):
        start, end, missing, mark, spot = self.fixture()
        with self.assertRaisesRegex(ValueError, 'Unapproved mark gap'):
            fill_missing(mark[1:], spot, start, end, missing, 'spot')

    def test_incomplete_or_duplicate_spot_rejected(self):
        start, end, missing, mark, spot = self.fixture()
        for bad in (spot[:-1], spot[:2] + [spot[1]] + spot[3:]):
            with self.assertRaises(ValueError):
                fill_missing(mark, bad, start, end, missing, 'spot')

    def test_invalid_spot_extrema_rejected(self):
        start, end, missing, mark, spot = self.fixture()
        spot[2][2] = '1'
        with self.assertRaisesRegex(ValueError, 'Invalid OHLC'):
            fill_missing(mark, spot, start, end, missing, 'spot')

    def test_policy_fixed_two_scenarios_four_accounts_no_2026_search(self):
        policy = load_policy(ROOT)
        self.assertEqual(policy['maximum_strategy_accounts'], 24)
        self.assertEqual(policy['scenarios'], ['spot', 'ffill'])
        self.assertEqual(len(policy['gap_open_times_utc']), 2)
        self.assertTrue(policy['holdout_locked'])
        self.assertFalse(policy['select_proxy_by_returns'])
        self.assertEqual(policy['funding_sensitivity']['multipliers'], [0, 1, 2])
        self.assertEqual(len(policy['funding_sensitivity']['archive_only_whitelist']), 9)

    def test_deviation_uses_only_observed_overlap_never_claims_missing_error(self):
        start, end, missing, mark, spot = self.fixture()
        result = deviation(mark, spot, start, end, missing, neighbor_minutes=1)
        self.assertEqual(result['overlap_minutes'], 2)
        self.assertFalse(result['true_gap_error_measured'])
        self.assertTrue(result['not_extreme_price_or_liquidation_error_bound'])
        # Neighbors have an intentionally large basis; never silently cap or discard it.
        self.assertGreater(float(result['maximum']), .08)

    def test_archive_only_exception_is_limited_before_network_access(self):
        config = {'market': {'symbols': ['BTCUSDT']}, 'data': {
            'warmup_start': '2024-05-01T00:00:00Z', 'development_end': '2026-01-01T00:00:00Z'}}
        with patch('perpquant.mark_proxy.fetch', side_effect=AssertionError('Network not allowed')):
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                archive_funding(ROOT, config, MonthlySpec('BTCUSDT', 'fundingRate', '2025-12'), load_policy(ROOT))

    def test_unverified_archive_funding_uses_closed_mark_and_retains_unverified_flag(self):
        begin = stamp_us('2025-12-01T00:00:00Z')
        records = [{'symbol': 'SOLUSDT', 'kind': 'markPriceKlines', 'period': '2025-12'},
                   {'symbol': 'SOLUSDT', 'kind': 'fundingRate', 'period': '2025-12',
                    'funding_rest_verified': False, 'provisional_archive_only': True}]
        config = {'market': {'symbols': ['SOLUSDT']}, 'data': {'development_start': '2025-12-01T00:00:00Z'}}
        marks = [{'open_time_us': begin, 'available_time_us': begin + MINUTE, 'close': '123'}]
        funds = [{'event_time_us': begin + MINUTE, 'interval_hours': 8, 'rate': D('.001')}]
        with patch('perpquant.mark_proxy.normalized', side_effect=[marks, funds]):
            body, count = valued_funding(ROOT, records, config)
        row = list(csv.DictReader(io.StringIO(gzip.decompress(body).decode())))[0]
        self.assertEqual(count, 1)
        self.assertEqual(row['settlement_mark'], '123')
        self.assertEqual(row['funding_rest_verified'], 'False')
        self.assertEqual(row['exact_mark'], 'False')
        self.assertEqual(int(row['source_available_us']), begin + MINUTE)
        stale = [{'event_time_us': begin + 3 * MINUTE, 'interval_hours': 8, 'rate': D('.001')}]
        with patch('perpquant.mark_proxy.normalized', side_effect=[marks, stale]):
            with self.assertRaisesRegex(ValueError, 'stale'):
                valued_funding(ROOT, records, config)


if __name__ == '__main__':
    unittest.main()
