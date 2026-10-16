"""Independent hypothesis, fixed risk and future-label separation regressions."""
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.engine import HOUR, MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.opportunity import OpportunityAccount
from perpquant.opportunity_run import load_plan
from perpquant.phase1 import CappedAccount
from perpquant.phase1_data import window_config
from perpquant.priority_run import prepare_cases
from perpquant.signal_quality import load_signal_plan
from perpquant.universe_data import SYMBOLS


class OpportunityTests(unittest.TestCase):
    def account(self, folder, case='control', cost='base', cls=OpportunityAccount, short_window=False):
        _, universe, config = window_config(ROOT, 'reproduction')
        if short_window:
            config['data']['development_end'] = '2023-01-01T12:00:00Z'
        signal, _, _ = load_signal_plan(ROOT)
        rule = InstrumentRules('.01', '.01', '100', '5',
                               (Tier('0', '1000000', '.025', '0', '10'),),
                               0, 2_000_000_000_000_000, 'SYNTHETIC_ONLY')
        kwargs = {'opportunity_case': case} if cls is OpportunityAccount else {}
        return cls(config, {s:rule for s in SYMBOLS}, {s:D('.01') for s in SYMBOLS},
                   {s:D('.0125') for s in SYMBOLS}, cost, Path(folder),
                   plan=universe, signal_plan=signal, name=case, **kwargs)

    def close(self, *accounts):
        for account in accounts:
            for name in ('event_stream', 'gate_log', 'sizing_log'):
                getattr(account, name).close()

    def features(self, t, side='long'):
        f = {'close':102 if side == 'long' else 98, 'mean':100, 'atr':1, 'ema50':100,
             'trend':side, 'efficiency24':.4,
             'higher_4h':{'available_us':t, 'ema20':101 if side == 'long' else 99, 'ema50':100}}
        return {s:deepcopy(f) if s == SYMBOLS[3] else None for s in SYMBOLS}

    def data(self, price='102'):
        value = D(price)
        row = {'open':price, 'close':price, 'high':str(value+D('.1')), 'low':str(value-D('.1'))}
        return {s:{'trade':dict(row), 'mark':dict(row)} for s in SYMBOLS}

    def test_control_diagnostics_do_not_change_financial_execution_both_costs(self):
        for cost in ('base', 'stress'):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as folder:
                control = self.account(folder, cost=cost)
                original = self.account(folder, 'original', cost, CappedAccount)
                try:
                    t = control.ledger.start + HOUR
                    for a in (control, original):
                        a.decisions(t, self.features(t))
                        a.minute(t+MINUTE, self.data(), [], False)
                        f = self.features(t+HOUR)
                        f[SYMBOLS[3]]['ema50'] = 103
                        a.decisions(t+HOUR, f)
                        a.minute(t+HOUR+MINUTE, self.data(), [], False)
                    self.assertEqual(control.trades, original.trades)
                    self.assertEqual(control.ledger.equity, original.ledger.equity)
                finally:
                    self.close(control, original)

    def test_wider_stop_reduces_quantity_and_keeps_risk_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            base, wide = self.account(folder), self.account(folder, 'stop3atr')
            try:
                t = base.ledger.start + HOUR
                for a in (base, wide):
                    a.decisions(t, self.features(t))
                    a.minute(t+MINUTE, self.data(), [], False)
                b, w = [a.ledger._state['positions'][SYMBOLS[3]] for a in (base, wide)]
                self.assertEqual((b['stop'], w['stop']), (D(100), D(99)))
                self.assertLess(w['quantity'], b['quantity'])
                self.assertLessEqual(next(iter(wide.initial_price_risks.values())), D('.5'))
                self.assertEqual(wide.config['risk'], base.config['risk'])
                self.assertEqual(wide.holding_limit_hours, 72)
            finally:
                self.close(base, wide)

    def test_longer_deadline_keeps_ema_exit_and_changes_no_risk(self):
        with tempfile.TemporaryDirectory() as folder:
            base, longer = self.account(folder), self.account(folder, 'hold168')
            try:
                t = base.ledger.start + HOUR
                for a in (base, longer):
                    a.decisions(t, self.features(t))
                    a.minute(t+MINUTE, self.data(), [], False)
                    a.decisions(t+74*HOUR, self.features(t+74*HOUR))
                self.assertEqual(base.queue[t+74*HOUR+MINUTE][0]['kind'], 'exit')
                self.assertFalse(longer.queue[t+74*HOUR+MINUTE])
                f = self.features(t+75*HOUR)
                f[SYMBOLS[3]]['ema50'] = 103
                longer.decisions(t+75*HOUR, f)
                self.assertEqual(longer.queue[t+75*HOUR+MINUTE][0]['kind'], 'exit')
                self.assertEqual(longer.config['risk'], base.config['risk'])
            finally:
                self.close(base, longer)

    def test_confirmation_is_causal_symmetric_and_idempotent_before_slots(self):
        for side in ('long', 'short'):
            with self.subTest(side=side), tempfile.TemporaryDirectory() as folder:
                a = self.account(folder, 'confirm2')
                try:
                    t = a.ledger.start + HOUR
                    a.decisions(t, self.features(t, side))
                    self.assertFalse(a.queue[t+MINUTE])
                    self.assertFalse(a.direction_slots.pending)
                    for _ in range(2):
                        a.decisions(t+HOUR, self.features(t+HOUR, side))
                        self.assertEqual(len(a.queue[t+HOUR+MINUTE]), 1)
                        self.assertEqual(len(a.direction_slots.pending), 1)
                    a.minute(t+HOUR+MINUTE, self.data(), [], True)
                    self.assertFalse(a.direction_slots.pending)
                finally:
                    self.close(a)

    def test_missing_confirmation_hour_resets_sequence(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.account(folder, 'confirm2')
            try:
                t = a.ledger.start + HOUR
                a.decisions(t, self.features(t))
                a.decisions(t+HOUR, {s:None for s in SYMBOLS})
                a.decisions(t+2*HOUR, self.features(t+2*HOUR))
                self.assertFalse(a.queue[t+2*HOUR+MINUTE])
                a.decisions(t+3*HOUR, self.features(t+3*HOUR))
                self.assertEqual(len(a.queue[t+3*HOUR+MINUTE]), 1)
            finally:
                self.close(a)

    def test_fixed_horizon_label_survives_early_stop_and_never_changes_equity(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.account(folder)
            try:
                t = a.ledger.start + HOUR
                a.decisions(t, self.features(t))
                a.minute(t+MINUTE, self.data(), [], False)
                a.minute(t+2*MINUTE, self.data('99'), [], False)
                self.assertFalse(a.ledger._state['positions'])
                equity = a.ledger.equity
                self.assertFalse(a.direction_labels)
                a.minute(t+MINUTE+24*HOUR, self.data('106'), [], False)
                self.assertEqual(a.ledger.equity, equity)
                self.assertTrue(a.direction_labels[0]['correct'])
                result = a.finish(Path(folder))
                self.assertEqual(result['direction_24h_hit_rate'], 1)
                self.assertLess(result['mean_net_trade_R'], 0)
                self.assertEqual(result['direction_24h_missing_entries'], 0)
            finally:
                self.close(a)

    def test_window_end_labels_are_missing_without_reading_future(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.account(folder, short_window=True)
            try:
                t = a.ledger.start + HOUR
                a.decisions(t, self.features(t))
                a.minute(t+MINUTE, self.data(), [], False)
                a.minute(t+2*MINUTE, self.data('99'), [], False)
                self.assertFalse(a.pending_labels)
                result = a.finish(Path(folder))
                self.assertEqual(result['direction_24h_missing_entries'], 1)
                self.assertIsNone(result['direction_24h_hit_rate'])
            finally:
                self.close(a)

    def test_registered_cases_use_the_original_windows_and_72_account_budget(self):
        plan = load_plan(ROOT)
        cases, aliases = prepare_cases(ROOT, plan)
        self.assertEqual(plan['candidate_cases'], ['hold168', 'stop3atr', 'confirm2'])
        self.assertEqual(len(cases)*plan['accounts_per_run'], 72)
        self.assertEqual(len(aliases), 2)
        self.assertFalse(plan['combine_changes'])


if __name__ == '__main__':
    unittest.main()
