from decimal import Decimal as D
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.margin import (Tier, funding_wallet_delta, initial_margin, isolated_state,
                             liquidation_fee, liquidation_price, tier_for, validate_tiers)


class MarginBehaviorTests(unittest.TestCase):
    def tiers(self):
        # Deliberately synthetic, not BTC/ETH exchange rules.
        return [Tier("0", "1000", "0.005", "0", "50"),
                Tier("1000", "10000", "0.01", "5", "20")]

    def test_long_short_pnl_and_mark_price_trigger(self):
        self.assertEqual(isolated_state(D(1), D(100), D(10), D(105), self.tiers())["unrealized_pnl"], 5)
        self.assertEqual(isolated_state(D(-1), D(100), D(10), D(105), self.tiers())["unrealized_pnl"], -5)
        for q, mark in [(D(1), D(90)), (D(-1), D(110))]:
            self.assertTrue(isolated_state(q, D(100), D(10), mark, self.tiers())["liquidation_triggered"])
        self.assertIsNone(isolated_state(D(1), D(100), D(-1), D(100), self.tiers())["margin_ratio"])

    def test_liquidation_root_rechecks_tier_at_root_not_entry(self):
        for q, entry, wallet in [(D(1), D(100), D(10)), (D(-1), D(100), D(10)),
                                  (D(-1), D(990), D(100))]:
            price = liquidation_price(q, entry, wallet, self.tiers())
            state = isolated_state(q, entry, wallet, price, self.tiers())
            self.assertLess(abs(state["margin_surplus"]), D("1e-20"))
        price = liquidation_price(D(-1), D(990), D(100), self.tiers())
        self.assertGreater(price, 1000)
        self.assertEqual(tier_for(price, self.tiers()).maintenance_rate, D("0.01"))

    def test_funding_sign_uses_notional_and_changes_threshold(self):
        for rate in (D("0.001"), D("-0.001")):
            long = funding_wallet_delta(D(2), D(100), rate)
            short = funding_wallet_delta(D(-2), D(100), rate)
            self.assertEqual(long + short, 0)
        delta = funding_wallet_delta(D(1), D(100), D("0.001"))
        before = liquidation_price(D(1), D(100), D(10), self.tiers())
        after = liquidation_price(D(1), D(100), D(10) + delta, self.tiers())
        self.assertGreater(after, before)

    def test_initial_margin_is_occupied_collateral_and_fee_is_separate(self):
        self.assertEqual(initial_margin(D(2), D(100), D(10), self.tiers()), 20)
        self.assertEqual(liquidation_fee(D(-2), D(90), D("0.01")), D("1.80"))
        with self.assertRaises(ValueError):
            initial_margin(D(1), D(100), D(11), self.tiers())

    def test_missing_discontinuous_and_out_of_coverage_rules_fail(self):
        for tiers in ([], [Tier(0, 1000, "0.005", 0, 50), Tier(1001, 10000, "0.01", 5, 20)],
                      [Tier(0, 1000, "0.005", 0, 50), Tier(1000, 10000, "0.01", 0, 20)]):
            with self.assertRaises(ValueError):
                validate_tiers(tiers)
        with self.assertRaises(ValueError):
            isolated_state(1000, 100, 10, 100, self.tiers())
        with self.assertRaises(ValueError):
            Tier(0, 1000, "NaN", 0, 50)
