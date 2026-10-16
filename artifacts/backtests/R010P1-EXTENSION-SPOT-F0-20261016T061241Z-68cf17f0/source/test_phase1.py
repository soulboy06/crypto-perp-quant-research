from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.engine import HOUR, MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.phase1 import DirectionSlots, CappedAccount
from perpquant.phase1_data import load_phase_plan
from perpquant.signal_quality import ExtensionAccount, load_signal_plan
from perpquant.universe_data import SYMBOLS, load_plan


class Phase1Tests(unittest.TestCase):
    def test_simultaneous_batch_reserves_three_slots_for_each_direction(self):
        slots = DirectionSlots(3)
        for side in ('long', 'short'):
            accepted = [slots.reserve(side + str(i), SYMBOLS[i], side, 60, {})
                        for i in range(5)]
            self.assertEqual(accepted, [True, True, True, False, False])
        self.assertEqual(len(slots.pending), 6)

    def test_three_reserved_fills_convert_to_positions_without_self_rejection(self):
        slots, positions = DirectionSlots(3), {}
        for i in range(3):
            self.assertTrue(slots.reserve(str(i), SYMBOLS[i], 'long', 60, positions))
        for i in range(3):
            self.assertTrue(slots.can_fill(str(i), positions))
            positions[SYMBOLS[i]] = {'quantity': D('1')}
            slots.release(str(i))
            self.assertEqual(slots.count(positions, 'long'), 3)
        self.assertFalse(slots.pending)

    def test_fill_recheck_rejects_new_excess_and_failed_reservation_is_reusable(self):
        slots = DirectionSlots(3)
        positions = {'a': {'quantity': D(1)}, 'b': {'quantity': D(1)}}
        self.assertTrue(slots.reserve('c', 'c', 'long', 60, positions))
        positions['external'] = {'quantity': D(1)}
        self.assertFalse(slots.can_fill('c', positions))
        slots.release('c')
        del positions['external']
        self.assertTrue(slots.reserve('d', 'd', 'long', 120, positions))
        slots.clear_due(120)
        self.assertFalse(slots.pending)

    def fixture(self, folder, capped, cost='base', name='test'):
        universe, _, config = load_plan(ROOT)
        signal, _, _ = load_signal_plan(ROOT)
        rule = InstrumentRules('.01', '.01', '100', '5',
                               (Tier('0', '1000000', '.025', '0', '10'),),
                               0, 2_000_000_000_000_000, 'SYNTHETIC_ONLY')
        rules = {s: rule for s in SYMBOLS}
        cls = CappedAccount if capped else ExtensionAccount
        return cls(config, rules, {s: D('.01') for s in SYMBOLS},
                   {s: D('.0125') for s in SYMBOLS}, cost, Path(folder),
                   plan=universe, signal_plan=signal, name=name)

    def close(self, a):
        for field in ('event_stream', 'gate_log', 'sizing_log'):
            getattr(a, field).close()

    def features(self, t, symbols):
        f = {'close': 102, 'mean': 100, 'atr': 1, 'ema50': 100,
             'trend': 'long', 'efficiency24': .4,
             'higher_4h': {'available_us': t, 'ema20': 101, 'ema50': 100}}
        return {s: dict(f) if s in symbols else None for s in SYMBOLS}

    def data(self, opening='102'):
        row = {'open': opening, 'close': opening,
               'high': str(D(opening) + D('.1')), 'low': str(D(opening) - D('.1'))}
        return {s: {'trade': dict(row), 'mark': dict(row)} for s in SYMBOLS}

    def test_real_decisions_limit_batch_and_failed_fills_release_all_slots(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.fixture(folder, True)
            try:
                t = a.ledger.start + HOUR
                fs = self.features(t, SYMBOLS[:5])
                a.decisions(t, fs)
                self.assertEqual([x['symbol'] for x in a.queue[t + MINUTE]], SYMBOLS[:3])
                self.assertEqual(a.rejects['R010P1:signal_same_direction_cap'], 2)
                # Every stop has already crossed by the delayed execution open.
                a.minute(t + MINUTE, self.data('99'), [], False)
                self.assertFalse(a.direction_slots.pending)
                self.assertFalse(a.ledger._state['positions'])
                self.assertEqual(a.rejects['stop_crossed_before_entry'], 3)
                a.decisions(t + HOUR, self.features(t + HOUR, SYMBOLS[:5]))
                self.assertEqual(len(a.direction_slots.pending), 3)
            finally:
                self.close(a)

    def test_terminal_batch_is_cancelled_without_slot_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            a = self.fixture(folder, True)
            try:
                t = a.ledger.start + HOUR
                a.decisions(t, self.features(t, ['XRPUSDT']))
                a.minute(t + MINUTE, self.data(), [], True)
                self.assertFalse(a.direction_slots.pending)
                self.assertFalse(a.ledger._state['positions'])
            finally:
                self.close(a)

    def test_nonbinding_gate_keeps_original_fills_exits_and_equity_both_costs(self):
        for cost in ('base', 'stress'):
            with self.subTest(cost=cost), tempfile.TemporaryDirectory() as folder:
                a = self.fixture(folder, True, cost, 'cap')
                b = self.fixture(folder, False, cost, 'control')
                try:
                    t = a.ledger.start + HOUR
                    fs = self.features(t, ['XRPUSDT'])
                    for x in (a, b):
                        x.decisions(t, fs)
                        self.assertNotIn(t, x.queue)
                        x.minute(t + MINUTE, self.data(), [], False)
                    self.assertTrue(a.ledger._state['positions'])
                    self.assertEqual(a.ledger._state['positions'], b.ledger._state['positions'])
                    self.assertFalse(a.direction_slots.pending)
                    fs['XRPUSDT'].update(close=99, mean=90, ema50=100)
                    for x in (a, b):
                        x.decisions(t + HOUR, fs)
                        self.assertEqual(x.queue[t + HOUR + MINUTE][0]['kind'], 'exit')
                        x.minute(t + HOUR + MINUTE, self.data(), [], False)
                    self.assertEqual(a.trades, b.trades)
                    self.assertEqual(a.ledger.equity, b.ledger.equity)
                finally:
                    self.close(a)
                    self.close(b)

    def test_phase1_locks_cap_baseline_and_does_not_allow_2026_extension(self):
        plan = load_phase_plan(ROOT)
        self.assertEqual(plan['maximum_same_direction_positions'], 3)
        self.assertFalse(plan['phase_2_enabled'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'configs').mkdir()
            text = (ROOT / 'configs/phase1.toml').read_text(encoding='utf-8')
            text = text.replace('end_exclusive = "2026-01-01T00:00:00Z"',
                                'end_exclusive = "2026-02-01T00:00:00Z"')
            (root / 'configs/phase1.toml').write_text(text, encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'Extension dates changed'):
                load_phase_plan(root)


if __name__ == '__main__':
    unittest.main()
