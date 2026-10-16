"""Causal score, fixed budget and unchanged execution/accounting regressions."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import math
import random
import statistics
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.engine import HOUR, MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.phase1 import CappedAccount
from perpquant.phase1_data import window_config
from perpquant.priority import WeeklyMomentum, PriorityAccount, directional_score
from perpquant.priority_run import load_plan, prepare_cases, compare_primary
from perpquant.signal_quality import load_signal_plan
from perpquant.universe_data import SYMBOLS


class ScoreTests(unittest.TestCase):
    def test_matches_independent_batch_and_has_exact_week_warmup(self):
        rng = random.Random(19)
        closes, value = [], 100.0
        model = WeeklyMomentum()
        for i in range(200):
            value *= math.exp(rng.uniform(-.01, .012))
            closes.append(value)
            context = model.push((i + 1) * HOUR, value)
            if i < 168:
                self.assertIsNone(context['long_score'])
        returns = [math.log(b / a) for a, b in zip(closes[-169:-1], closes[-168:])]
        expected = sum(returns) / (statistics.stdev(returns) * math.sqrt(168))
        self.assertAlmostEqual(context['long_score'], expected, places=12)

    def test_direction_mirror_and_future_features_refused(self):
        context = {'available_us': HOUR, 'lookback_hours': 168, 'long_score': 3}
        feature = {'momentum_priority': context}
        self.assertEqual(directional_score(feature, 'long', HOUR), 3)
        self.assertEqual(directional_score(feature, 'short', HOUR), -3)
        with self.assertRaisesRegex(ValueError, 'Future'):
            directional_score(feature, 'long', 0)

    def test_flat_and_missing_are_unavailable_and_clock_gaps_refused(self):
        model = WeeklyMomentum()
        for i in range(180):
            self.assertIsNone(model.push((i + 1) * HOUR, 100)['long_score'])
        self.assertIsNone(directional_score({}, 'long', HOUR))
        with self.assertRaisesRegex(ValueError, 'consecutive'):
            model.push(182 * HOUR, 100)


class AccountTests(unittest.TestCase):
    def account(self, folder, cls=PriorityAccount, name='priority', cost='base'):
        _, universe, config = window_config(ROOT, 'reproduction')
        signal, _, _ = load_signal_plan(ROOT)
        rule = InstrumentRules('.01', '.01', '100', '5',
                               (Tier('0', '1000000', '.025', '0', '10'),),
                               0, 2_000_000_000_000_000, 'SYNTHETIC_ONLY')
        return cls(config, {s: rule for s in SYMBOLS}, {s: D('.01') for s in SYMBOLS},
                   {s: D('.0125') for s in SYMBOLS}, cost, Path(folder),
                   plan=universe, signal_plan=signal, name=name)

    def close(self, account):
        for field in ('event_stream', 'gate_log', 'sizing_log'):
            getattr(account, field).close()

    def features(self, time, scores, side='long'):
        feature = {'close': 102 if side == 'long' else 98, 'mean': 100, 'atr': 1,
                   'ema50': 100, 'trend': side, 'efficiency24': .4,
                   'higher_4h': {'available_us': time, 'ema20': 101 if side == 'long' else 99, 'ema50': 100}}
        result = {s: None for s in SYMBOLS}
        for s, score in zip(SYMBOLS, scores):
            result[s] = {**deepcopy(feature), 'momentum_priority': {
                'available_us': time, 'lookback_hours': 168, 'long_score': score}}
        return result

    def data(self):
        row = {'open': '102', 'close': '102', 'high': '102.1', 'low': '101.9'}
        return {s: {'trade': dict(row), 'mark': dict(row)} for s in SYMBOLS}

    def test_priority_precedes_slot_cap_and_replacement_releases_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            account = self.account(folder)
            try:
                t = account.ledger.start + HOUR
                for _ in range(2):
                    account.decisions(t, self.features(t, [1, 2, 3, 4]))
                    self.assertEqual([a['symbol'] for a in account.queue[t + MINUTE]], list(reversed(SYMBOLS[1:4])))
                    self.assertEqual(len(account.direction_slots.pending), 3)
                account.minute(t + MINUTE, self.data(), [], True)
                self.assertFalse(account.direction_slots.pending)
            finally:
                self.close(account)

    def test_short_ranks_negative_momentum_first_and_negative_scores_are_not_filters(self):
        for side, scores, expected in [('short', [3, -4, -2], [1, 2, 0]),
                                       ('long', [-1, -2], [0, 1])]:
            with self.subTest(side=side), tempfile.TemporaryDirectory() as folder:
                account = self.account(folder)
                try:
                    t = account.ledger.start + HOUR
                    account.decisions(t, self.features(t, scores, side))
                    self.assertEqual([a['symbol'] for a in account.queue[t + MINUTE]], [SYMBOLS[i] for i in expected])
                finally:
                    self.close(account)

    def test_ties_and_unavailable_scores_preserve_legacy_order(self):
        with tempfile.TemporaryDirectory() as folder:
            account = self.account(folder)
            try:
                t = account.ledger.start + HOUR
                account.decisions(t, self.features(t, [None, 1, 1]))
                self.assertEqual([a['symbol'] for a in account.queue[t + MINUTE]], [SYMBOLS[1], SYMBOLS[2], SYMBOLS[0]])
            finally:
                self.close(account)

    def test_single_signal_preserves_original_fills_exit_and_equity_both_costs(self):
        for cost in ('base', 'stress'):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as folder:
                candidate = self.account(folder, cost=cost)
                control = self.account(folder, CappedAccount, 'control', cost)
                try:
                    t = candidate.ledger.start + HOUR
                    features = self.features(t, [1])
                    for account in (candidate, control):
                        account.decisions(t, features)
                        account.minute(t + MINUTE, self.data(), [], False)
                    self.assertEqual(candidate.ledger._state['positions'], control.ledger._state['positions'])
                    features[SYMBOLS[0]].update(close=99, ema50=100)
                    for account in (candidate, control):
                        account.decisions(t + HOUR, features)
                        self.assertEqual(account.queue[t + HOUR + MINUTE][0]['kind'], 'exit')
                        account.minute(t + HOUR + MINUTE, self.data(), [], False)
                    self.assertEqual(candidate.trades, control.trades)
                    self.assertEqual(candidate.ledger.equity, control.ledger.equity)
                finally:
                    self.close(candidate)
                    self.close(control)


class ProtocolTests(unittest.TestCase):
    def test_registered_inputs_deduplicate_before_any_returns_and_respect_budget(self):
        plan = load_plan(ROOT)
        cases, aliases = prepare_cases(ROOT, plan)
        self.assertEqual(len(cases), 9)
        self.assertEqual(len(cases) * 4, plan['maximum_account_executions'])
        self.assertEqual({a['case'] for a in aliases}, {'2025H1-ffill', '2025H2-ffill'})
        for case in cases:
            self.assertEqual(case['config']['market']['symbols'], SYMBOLS)
            self.assertEqual(case['config']['risk']['per_trade_risk_fraction'], '0.005')
            self.assertLessEqual(case['window']['end_exclusive'], '2026-01-01T00:00:00Z')
            self.assertTrue(all(int(row['event_time_us']) < 1767225600000000 for row in case['funding']))

    def test_missing_account_results_cannot_pass_primary(self):
        plan = load_plan(ROOT)
        with self.assertRaises(KeyError):
            compare_primary(Path('.'), [], plan['gates'])


if __name__ == '__main__':
    unittest.main()
