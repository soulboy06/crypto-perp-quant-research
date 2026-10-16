"""Offline guards for monthly partition boundaries and before-request isolation."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.config import load
from perpquant.full_data import MonthlySpec, specs_for, prepare_one


class MonthlyGuardTests(unittest.TestCase):
    def setUp(self):
        self.config=load(ROOT/"configs/first_round.toml")

    def test_exact_monthly_inventory_and_leap_year(self):
        specs=specs_for(self.config)
        self.assertEqual(len(specs),228)
        self.assertEqual(min(s.period for s in specs),"2022-11")
        self.assertEqual(max(s.period for s in specs),"2025-12")
        start,end=MonthlySpec("BTCUSDT","klines","2024-02").interval()
        self.assertEqual((end-start).days,29)
        self.assertEqual(MonthlySpec("BTCUSDT","markPriceKlines","2023-01").url(),
                         "https://data.binance.vision/data/futures/um/monthly/markPriceKlines/BTCUSDT/1m/BTCUSDT-1m-2023-01.zip")

    def test_holdout_and_wrong_universe_rejected_before_network(self):
        for spec in (MonthlySpec("BTCUSDT","klines","2026-01"), MonthlySpec("DOGEUSDT","fundingRate","2023-01"),
                     MonthlySpec("BTCUSDT","klines","2022-10")):
            with self.subTest(spec=spec), patch("perpquant.full_data.fetch") as fetch:
                with self.assertRaises(ValueError):
                    prepare_one(ROOT,self.config,spec)
                fetch.assert_not_called()


if __name__=="__main__":
    unittest.main()
