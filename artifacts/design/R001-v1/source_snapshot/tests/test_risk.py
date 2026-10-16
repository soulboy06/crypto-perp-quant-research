from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.config import load, validate, readiness
from perpquant.risk import EntryRequest, preview


class RiskBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load(ROOT / "configs/first_round.toml")

    def request(self, **changes):
        initial = EntryRequest(D(100), D(100), D(100), D(98), D("0.01"), D("0.01"), D(5))
        return replace(initial, **changes)

    def test_long_short_risk_and_rounding(self):
        for side, stop in [("long", D(98)), ("short", D(102))]:
            result = preview(self.config, self.request(side=side, stop_price=stop))
            self.assertEqual(result["quantity"], D("0.21"))
            self.assertLessEqual(result["projected_risk"], D("0.5"))
            self.assertEqual(result["quantity"] % D("0.01"), 0)

    def test_cap_uses_equity_after_costs_and_includes_existing_exposure(self):
        result = preview(self.config, self.request(existing_gross=D(950)), D(10))
        self.assertEqual(result["quantity"], D("0.21"))
        self.assertLessEqual(result["projected_gross_leverage"], 10)
        tight = self.request(stop_price=D("99.99"), existing_gross=D(950))
        result = preview(self.config, tight, D(10))
        self.assertEqual(result["quantity"], D("0.49"))
        self.assertLessEqual(result["projected_gross_leverage"], 10)

    def test_pending_orders_and_shared_risk_are_reserved(self):
        result = preview(self.config, self.request(pending_gross=D(90)))
        self.assertEqual(result["quantity"], D("0.09"))
        result = preview(self.config, self.request(existing_stop_risk=D("0.8"),
                                                  pending_stop_risk=D("0.2")))
        self.assertEqual(result["status"], "rejected")

    def test_capital_stop_and_persistent_halt(self):
        for equity in (D(50), D(49), D(0)):
            result = preview(self.config, self.request(equity=equity, free_collateral=equity))
            self.assertEqual(result["reason"], "account_halted_or_capital_stop")
        self.assertEqual(preview(self.config, self.request(halted=True))["status"], "rejected")

    def test_minimums_do_not_force_oversizing_and_cash_reserve_is_kept(self):
        for request in (self.request(min_notional=D(25)), self.request(free_collateral=D(20))):
            self.assertEqual(preview(self.config, request)["status"], "rejected")

    def test_invalid_numbers_direction_and_leverage_fail_closed(self):
        for request in (self.request(stop_price=D(101)), self.request(equity=D("NaN")),
                        self.request(step_size=D(0)), self.request(pending_gross=D(-1))):
            with self.assertRaises(ValueError):
                preview(self.config, request)
        for cap in (D(11), D("NaN"), D(3)):
            with self.assertRaises(ValueError):
                preview(self.config, self.request(), cap)

    def test_config_blocks_live_leverage_breach_and_time_overlap(self):
        for section, key, value in ((None, "mode", "live"),
                                     ("risk", "max_effective_leverage", "11"),
                                     ("risk", "per_trade_risk_fraction", "NaN"),
                                     ("data", "holdout_start", "2025-01-01T00:00:00Z"),
                                     ("data", "holdout_locked", False)):
            changed = deepcopy(self.config)
            (changed if section is None else changed[section])[key] = value
            with self.assertRaises(ValueError):
                validate(changed)

    def test_readiness_remains_incomplete_without_engine_and_evidence(self):
        missing = readiness(self.config, ROOT)
        self.assertIn("engine_implemented", missing)
        self.assertIn("margin_rules_verified", missing)
        self.assertIn("missing:data_manifest", missing)


if __name__ == "__main__":
    unittest.main()
