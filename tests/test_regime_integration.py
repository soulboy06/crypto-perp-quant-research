"""R011 runner, financial-metric schema and real account admission regressions."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import csv
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.engine import HOUR, MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.regime_account import RegimeAccount
from perpquant.regime_run import regime_window_config, corpus, evaluate_gates
from perpquant.signal_quality import load_signal_plan
from perpquant.universe_data import SYMBOLS


class RunnerTests(unittest.TestCase):
    def test_ten_coin_config_and_both_windows(self):
        for scope, start in [('extension', '2024-07-01'), ('reproduction', '2023-01-01')]:
            plan, universe, config = regime_window_config(ROOT, scope)
            self.assertEqual(config['market']['symbols'], SYMBOLS)
            self.assertEqual(universe['symbols'], SYMBOLS)
            self.assertTrue(config['data']['development_start'].startswith(start))
            self.assertLessEqual(plan[scope]['end_exclusive'], '2026-01-01T00:00:00Z')

    def test_actual_corpora_select_old_window_and_both_proxy_manifests(self):
        for scope, scenario in [('reproduction', None), ('extension', 'spot'), ('extension', 'ffill')]:
            with self.subTest(scope=scope, scenario=scenario):
                _, _, config = regime_window_config(ROOT, scope)
                path, manifest, index, reference = corpus(ROOT, scope, config, scenario)
                self.assertEqual(len(index), 600)
                self.assertTrue(reference.is_dir())
                self.assertFalse(manifest['holdout_history_requested'])
                if scenario:
                    self.assertEqual(manifest['scenario'], scenario)
                    self.assertTrue(manifest['complete_with_explicit_proxy'])
                    self.assertTrue(manifest['funding_rest_verified_all_partitions'])
                    self.assertEqual(manifest['proxy_bar_count'], 14)
                else:
                    self.assertIn('universe_runs', str(path))
                    self.assertTrue(all(period < '2024-07' for _, _, period in index))

    def test_changed_threshold_and_2026_window_are_refused(self):
        for old, new in [('threshold = "25.0"', 'threshold = "20.0"'),
                         ('end_exclusive = "2026-01-01T00:00:00Z"',
                          'end_exclusive = "2026-02-01T00:00:00Z"')]:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                shutil.copytree(ROOT / 'configs', root / 'configs')
                path = root / 'configs/eleventh_round.toml'
                path.write_text(path.read_text(encoding='utf-8').replace(old, new), encoding='utf-8')
                with self.assertRaises(ValueError):
                    regime_window_config(root, 'extension')


class GateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.cfg = regime_window_config(ROOT, 'extension')[0]['gates']
        self.metrics = []
        for cost in ('base', 'stress'):
            for label, daily in [('control_cap3', .0001), ('candidate_regime', .0005)]:
                name = label + '_' + cost
                cagr = (1 + daily) ** 365 - 1
                self.metrics.append({'account': name, 'round_trips': 100, 'net_return': .1,
                                     'cagr': cagr, 'max_adverse_bound_drawdown': .05,
                                     'calmar': cagr / .05, 'halt_reasons': [],
                                     'liquidation_proxy_events': 0, 'execution_ambiguities': 0})
                with (self.folder / (name + '_daily_equity.csv')).open('w', newline='') as stream:
                    writer = csv.DictWriter(stream, fieldnames=['date', 'equity'])
                    writer.writeheader()
                    writer.writerows({'date': f'day-{i:03}', 'equity': 100 * (1 + daily) ** (i + 1)}
                                     for i in range(56))

    def evaluate(self, metrics=None, scope='extension'):
        return evaluate_gates(self.metrics if metrics is None else metrics, self.cfg, self.folder, scope)

    def test_real_schema_and_actual_paired_bootstrap_pass(self):
        gates = self.evaluate()
        self.assertTrue(gates['overall_passed'])
        for cost in ('base', 'stress'):
            ci = gates[cost]['bootstrap']
            self.assertGreater(ci['cagr_improvement_lower_95'], 0)
            self.assertEqual((ci['block_days'], ci['resamples'], ci['seed']), (14, 2000, 20261016))
        self.assertFalse(gates['formal_selection_eligible'])

    def test_missing_pairs_and_missing_fields_fail_closed(self):
        self.assertFalse(self.evaluate([])['overall_passed'])
        self.assertFalse(self.evaluate(self.metrics[:2])['overall_passed'])
        broken = deepcopy(self.metrics)
        del broken[1]['round_trips']
        self.assertFalse(self.evaluate(broken)['overall_passed'])

    def test_real_trade_risk_calmar_and_halt_fields_are_enforced(self):
        for field, value, check in [('round_trips', 29, 'min_trades'),
                                    ('max_adverse_bound_drawdown', .21, 'drawdown_limit'),
                                    ('calmar', 0, 'calmar_non_decreasing'),
                                    ('halt_reasons', ['drawdown_limit'], 'no_halts'),
                                    ('liquidation_proxy_events', 1, 'no_halts'),
                                    ('execution_ambiguities', 1, 'no_halts')]:
            with self.subTest(field=field):
                broken = deepcopy(self.metrics)
                broken[1][field] = value
                gates = self.evaluate(broken)
                self.assertFalse(gates['base']['checks'][check])
                self.assertFalse(gates['overall_passed'])

    def test_bootstrap_not_bypassed_by_other_passing_metrics(self):
        for cost in ('base', 'stress'):
            shutil.copyfile(self.folder / f'control_cap3_{cost}_daily_equity.csv',
                            self.folder / f'candidate_regime_{cost}_daily_equity.csv')
        gates = self.evaluate()
        self.assertFalse(gates['overall_passed'])
        self.assertFalse(gates['base']['checks']['bootstrap_lower_positive'])

    def test_missing_daily_series_fails_closed_and_reproduction_cannot_accept(self):
        self.assertTrue(self.evaluate(scope='reproduction')['numerical_checks_passed'])
        self.assertFalse(self.evaluate(scope='reproduction')['overall_passed'])
        (self.folder / 'candidate_regime_base_daily_equity.csv').unlink()
        gates = self.evaluate()
        self.assertFalse(gates['overall_passed'])
        self.assertEqual(gates['base']['reason'], 'bootstrap_unavailable')


class AccountTests(unittest.TestCase):
    def fixture(self, folder):
        _, universe, config = regime_window_config(ROOT, 'reproduction')
        signal, _, _ = load_signal_plan(ROOT)
        rule = InstrumentRules('.01', '.01', '100', '5',
                               (Tier('0', '1000000', '.025', '0', '10'),),
                               0, 2_000_000_000_000_000, 'SYNTHETIC_ONLY')
        account = RegimeAccount(config, {s: rule for s in SYMBOLS}, {s: D('.01') for s in SYMBOLS},
                                {s: D('.0125') for s in SYMBOLS}, 'base', Path(folder),
                                plan=universe, signal_plan=signal, name='regime_test')
        return account

    def features(self, t):
        feature = {'close': 102, 'mean': 100, 'atr': 1, 'ema50': 100, 'trend': 'long',
                   'efficiency24': .4, 'higher_4h': {'available_us': t, 'ema20': 101, 'ema50': 100}}
        return {s: deepcopy(feature) if s in SYMBOLS[:4] else None for s in SYMBOLS}

    def test_choppy_coins_do_not_take_slots_and_replaced_batch_releases_reservations(self):
        with tempfile.TemporaryDirectory() as folder:
            account = self.fixture(folder)
            try:
                t = account.ledger.start + HOUR
                for symbol in SYMBOLS[:3]:
                    account.regimes[symbol].adx = 10
                account.regimes[SYMBOLS[3]].adx = 30
                for _ in range(2):
                    account.decisions(t, self.features(t))
                    entries = account.queue[t + MINUTE]
                    self.assertEqual([x['symbol'] for x in entries], [SYMBOLS[3]])
                    self.assertEqual(len(account.direction_slots.pending), 1)
                # A newly choppy regime replaces the batch and frees its old slot.
                account.regimes[SYMBOLS[3]].adx = 10
                account.decisions(t, self.features(t))
                self.assertFalse(account.queue[t + MINUTE])
                self.assertFalse(account.direction_slots.pending)
            finally:
                for field in ('event_stream', 'gate_log', 'sizing_log'):
                    getattr(account, field).close()

    def test_choppy_regime_does_not_block_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            account = self.fixture(folder)
            try:
                t = account.ledger.start + HOUR
                symbol = SYMBOLS[3]
                account.regimes[symbol].adx = 30
                account.decisions(t, self.features(t))
                row = {'open': '102', 'close': '102', 'high': '102.1', 'low': '101.9'}
                data = {s: {'trade': dict(row), 'mark': dict(row)} for s in SYMBOLS}
                account.minute(t + MINUTE, data, [], False)
                self.assertIn(symbol, account.ledger._state['positions'])
                account.regimes[symbol].adx = 10
                features = self.features(t + HOUR)
                features[symbol]['ema50'] = 103
                account.decisions(t + HOUR, features)
                self.assertEqual(account.queue[t + HOUR + MINUTE][0]['kind'], 'exit')
                account.minute(t + HOUR + MINUTE, data, [], False)
                self.assertFalse(account.ledger._state['positions'])
            finally:
                for field in ('event_stream', 'gate_log', 'sizing_log'):
                    getattr(account, field).close()


if __name__ == '__main__':
    unittest.main()
