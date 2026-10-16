"""Accounting identities against hand-computed synthetic examples, NOT historical rules."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.config import load
from perpquant.ledger import Ledger, InstrumentRules, DAY_US
from perpquant.margin import Tier


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "configs/first_round.toml")
        tier = Tier("0", "1000000", "0.005", "0", "10")
        rule = InstrumentRules("0.01", "0.01", "100", "5", (tier,), 0, 2_000_000_000_000_000, "SYNTHETIC_TEST_ONLY")
        self.rules = {s: rule for s in self.config["market"]["symbols"]}
        self.ledger = self.new()
        self.t = self.ledger.start
        self.n = 0

    def new(self, **kwargs):
        return Ledger(self.config, self.rules, funding_policy="available_first_credit_cash_research_v1", **kwargs)

    def event(self, kind, **kwargs):
        self.n += 1
        return {"id": f"e{self.n}", "type": kind, "time_us": self.t, **kwargs}

    def apply(self, kind, **kwargs):
        return self.ledger.apply(self.event(kind, **kwargs))

    def reserve(self, symbol="BTCUSDT", side="long", order_id="o1", quantity="0.20"):
        return self.apply("reserve", symbol=symbol, side=side, order_id=order_id,
                          reference_price="100", stop_price="98" if side == "long" else "102", quantity=quantity)

    def fill(self, order_id="o1", quantity="0.20", price="100", mark="100"):
        return self.apply("fill", order_id=order_id, quantity=quantity, price=price, mark_price=mark)

    def test_two_symbols_share_cash_and_wallet_identity(self):
        self.reserve()
        self.assertEqual(self.ledger.equity, 100)
        self.assertEqual(D(self.ledger.snapshot()["cash"]), D("97.948"))
        self.fill()
        self.reserve("ETHUSDT", "short", "o2")
        self.fill("o2")
        state = self.ledger.snapshot()
        self.assertEqual(D(state["cash"]), D("95.976"))
        self.assertEqual(self.ledger.equity, D("99.976"))
        self.apply("mark", prices={"BTCUSDT": "102", "ETHUSDT": "99"})
        self.assertEqual(self.ledger.equity, D("100.576"))  # +0.4 BTC, +0.2 ETH, 0.024 fees
        self.assertEqual(D(self.ledger.snapshot()["realized_pnl"]), 0)

    def test_partial_fill_cancel_partial_exit_and_full_exit(self):
        self.reserve()
        self.fill(quantity="0.10")
        state = self.ledger.snapshot()
        self.assertEqual(D(state["orders"]["o1"]["remaining"]), D("0.10"))
        self.assertEqual(D(state["orders"]["o1"]["reserved"]), D("1.026"))
        self.apply("cancel", order_id="o1")
        self.apply("exit", symbol="BTCUSDT", quantity="0.04", price="110", mark_price="110")
        self.assertEqual(D(self.ledger.snapshot()["positions"]["BTCUSDT"]["wallet"]), D("0.6"))
        self.apply("exit", symbol="BTCUSDT", quantity="0.06", price="105", mark_price="105")
        self.assertEqual(self.ledger.equity, D("100.68758"))  # +.7 pnl - .006 -.00264 -.00378 fees
        self.assertFalse(self.ledger.snapshot()["positions"])

    def test_idempotency_conflicts_and_atomic_rejection(self):
        event = self.event("reserve", symbol="BTCUSDT", side="long", order_id="o1",
                           reference_price="100", stop_price="98", quantity="0.20")
        first = self.ledger.apply(event)
        self.assertEqual(first, self.ledger.apply(event))
        with self.assertRaises(ValueError):
            self.ledger.apply({**event, "quantity": "0.19"})
        with self.assertRaises(ValueError):
            self.fill(price="200", mark="200")
        self.assertEqual(first, self.ledger.snapshot())
        self.fill()
        before = self.ledger.snapshot()
        with self.assertRaises(ValueError):
            self.reserve(side="short", order_id="o2")
        self.assertEqual(before, self.ledger.snapshot())

    def test_funding_sign_proxy_validation_and_economic_duplicates(self):
        self.reserve()
        self.fill()
        self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.001", exact_mark=False, source_available_us=self.t)
        self.assertEqual(self.ledger.equity, D("99.968"))
        before = self.ledger.snapshot()
        with self.assertRaises(ValueError):
            self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.001", exact_mark=True)
        self.assertEqual(before, self.ledger.snapshot())
        self.t += 1
        self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="-0.002", exact_mark=True)
        self.assertEqual(self.ledger.equity, D("100.008"))
        self.t += 1
        before = self.ledger.snapshot()
        with self.assertRaises(ValueError):
            self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.001", exact_mark=False, source_available_us=self.t+1)
        self.assertEqual(before, self.ledger.snapshot())

    def test_funding_draws_cash_before_isolated_and_does_not_hide_deficit(self):
        self.reserve()
        self.fill()
        for _ in range(6):
            self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.99", exact_mark=True)
            self.t += 1
        state = self.ledger.snapshot()
        self.assertEqual(D(state["cash"]), 0)
        self.assertEqual(D(state["positions"]["BTCUSDT"]["wallet"]), D("-18.812"))
        self.assertIn("liquidation_required:BTCUSDT", state["halt_reasons"])

    def test_day_pause_and_permanent_drawdown_survive_recovery(self):
        self.reserve()
        self.fill()
        self.apply("mark", prices={"BTCUSDT": "80"})
        self.assertTrue(self.ledger.snapshot()["daily_paused"])
        self.apply("mark", prices={"BTCUSDT": "100"})
        self.assertTrue(self.ledger.snapshot()["daily_paused"])
        self.t += DAY_US
        self.apply("mark", prices={"BTCUSDT": "100"})
        self.assertFalse(self.ledger.snapshot()["daily_paused"])
        # A large gap can overshoot both stops, and is recorded without loss clamping.
        self.apply("exit", symbol="BTCUSDT", quantity="0.20", price="1", mark_price="1")
        self.assertIn("liquidation_required:BTCUSDT", self.ledger.snapshot()["halt_reasons"])
        self.assertLess(self.ledger.equity, 81)

    def test_capital_stop_cancels_orders_and_cannot_be_reset_by_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.sqlite"
            self.ledger = self.new(journal=path)
            self.reserve()
            self.fill()
            self.reserve("ETHUSDT", "short", "o2")
            self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="-0.99", exact_mark=True)
            self.t += 1
            self.apply("mark", prices={"BTCUSDT": "100"})
            self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.99", exact_mark=True)
            self.t += 1
            # Repeated severe funding loss to prove account stop is independent of notional clamping.
            for _ in range(3):
                self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.99", exact_mark=True)
                self.t += 1
            state = self.ledger.snapshot()
            self.assertLess(self.ledger.equity, 50)
            self.assertIn("capital_stop", state["halt_reasons"])
            self.assertFalse(state["orders"])
            self.ledger.close()
            restored = self.new(journal=path)
            self.assertEqual(state, restored.snapshot())
            self.assertEqual(state, restored.apply({"id":"e1", "type":"reserve", "time_us":restored.start,
                "symbol":"BTCUSDT", "side":"long", "order_id":"o1", "reference_price":"100", "stop_price":"98", "quantity":"0.20"}))
            restored.close()

    def test_journal_tampering_rules_binding_and_missing_rules_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "ledger.sqlite"
            ledger = self.new(journal=path)
            ledger.apply(self.event("mark", prices={}))
            ledger.close()
            with self.assertRaises(ValueError):
                self.new(journal=path, leverage_cap="2")
            with sqlite3.connect(path) as db:
                db.execute("UPDATE events SET state_hash='tampered'")
            db.close()
            with self.assertRaises(ValueError):
                self.new(journal=path)
        with self.assertRaises(ValueError):
            Ledger(self.config, {}, funding_policy="available_first_credit_cash_research_v1")

    def test_rule_expiry_missing_midnight_and_stale_marks_fail_atomically(self):
        self.reserve()
        self.fill()
        before = self.ledger.snapshot()
        self.t += 60_000_000
        with self.assertRaises(ValueError):
            self.reserve("ETHUSDT", "short", "o2")
        self.assertEqual(before, self.ledger.snapshot())
        self.t = self.ledger.start + DAY_US + 1
        with self.assertRaises(ValueError):
            self.apply("mark", prices={"BTCUSDT": "100"})
        self.assertEqual(before, self.ledger.snapshot())
        self.rules["BTCUSDT"] = replace(self.rules["BTCUSDT"], valid_to_us=self.ledger.start)
        expired = self.new()
        with self.assertRaisesRegex(ValueError, "No rule schedule"):
            expired.apply({"id":"expired", "type":"reserve", "time_us":expired.start, "symbol":"BTCUSDT",
                           "side":"long", "order_id":"expired", "reference_price":"100", "stop_price":"98", "quantity":"0.20"})

    def test_short_close_and_persistent_peak_drawdown(self):
        self.reserve(side="short")
        self.fill()
        self.apply("funding", symbol="BTCUSDT", mark_price="100", rate="0.001", exact_mark=True)
        self.apply("exit", symbol="BTCUSDT", quantity="0.20", price="90", mark_price="90")
        self.assertEqual(self.ledger.equity, D("101.9972"))
        self.ledger = self.new()
        self.reserve(order_id="fresh")
        self.fill("fresh")
        self.apply("mark", prices={"BTCUSDT":"500"})
        self.apply("mark", prices={"BTCUSDT":"100"})
        self.assertIn("drawdown_halt", self.ledger.snapshot()["halt_reasons"])
        self.apply("mark", prices={"BTCUSDT":"500"})
        self.assertIn("drawdown_halt", self.ledger.snapshot()["halt_reasons"])


if __name__ == "__main__":
    unittest.main()
