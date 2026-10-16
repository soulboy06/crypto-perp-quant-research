"""R011 regime filter tests: ADX math, gate behavior, no-lookahead, fail-closed."""
import math
import random
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from perpquant.regime import DailyADX, ADX_PERIOD, ADX_TREND_THRESHOLD


def ref_adx(bars, n=14):
    trs, pdms, mdms = [], [], []
    for i in range(1, len(bars)):
        h, l, c = bars[i]
        ph, pl, pc = bars[i - 1]
        up, dn = h - ph, pl - l
        pdms.append(up if up > dn and up > 0 else 0.0)
        mdms.append(dn if dn > up and dn > 0 else 0.0)
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    str_ = sum(trs[:n]) / n
    spdm = sum(pdms[:n]) / n
    smdm = sum(mdms[:n]) / n
    dxs = []
    for i in range(n, len(trs)):
        str_ += (trs[i] - str_) / n
        spdm += (pdms[i] - spdm) / n
        smdm += (mdms[i] - smdm) / n
        pdi, mdi = 100 * spdm / str_, 100 * smdm / str_
        s = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / s if s > 0 else 0.0)
    adx = sum(dxs[:n]) / n
    for d in dxs[n:]:
        adx += (d - adx) / n
    return adx


def trending_bars(n=60, seed=7):
    rnd = random.Random(seed)
    bars, p = [], 100.0
    for _ in range(n):
        o = p
        c = o + 2.0 + rnd.uniform(-1, 1)
        bars.append((max(o, c) + 0.5, min(o, c) - 0.5, c))
        p = c
    return bars


def choppy_bars(n=60, seed=7):
    # Mean-reverting around 100: no sustained trend -> low ADX.
    rnd = random.Random(seed)
    bars, p = [], 100.0
    for _ in range(n):
        o = p
        c = 100 + (o - 100) * 0.3 + rnd.uniform(-1.2, 1.2)
        bars.append((max(o, c) + 0.5, min(o, c) - 0.5, c))
        p = c
    return bars


def feed_daily(adx, bars):
    for day, (h, l, c) in enumerate(bars):
        adx.push_minute(h, l, c, day)
    # close the final day
    h, l, c = bars[-1]
    adx.push_minute(h, l, c, len(bars))
    return adx


class TestDailyADX(unittest.TestCase):
    def test_matches_batch_reference(self):
        bars = trending_bars()
        got = feed_daily(DailyADX(), bars).adx
        self.assertAlmostEqual(got, ref_adx(bars), places=9)

    def test_matches_batch_reference_choppy(self):
        bars = choppy_bars()
        got = feed_daily(DailyADX(), bars).adx
        self.assertAlmostEqual(got, ref_adx(bars), places=9)

    def test_trending_market_above_threshold(self):
        adx = feed_daily(DailyADX(), trending_bars(80))
        self.assertIsNotNone(adx.adx)
        self.assertTrue(adx.is_trending())

    def test_choppy_market_below_threshold(self):
        adx = feed_daily(DailyADX(), choppy_bars(80))
        self.assertIsNotNone(adx.adx)
        self.assertFalse(adx.is_trending())

    def test_fail_closed_before_ready(self):
        adx = DailyADX()
        self.assertIsNone(adx.adx)
        self.assertFalse(adx.is_trending())

    def test_no_lookahead_single_day_does_not_move_adx(self):
        bars = trending_bars(40)
        a = feed_daily(DailyADX(), bars)
        before = a.adx
        # Push an extreme next-day bar but do NOT close it: ADX must not move.
        h, l, c = bars[-1]
        a.push_minute(h * 2.0, l * 0.5, c * 1.5, len(bars))
        self.assertEqual(a.adx, before)

    def test_flat_market_adx_low(self):
        bars = [(100.0, 100.0, 100.0)] * 60
        adx = feed_daily(DailyADX(), bars)
        # TR=0 -> ADX stays None (no division); gate stays closed.
        self.assertFalse(adx.is_trending())

    def test_threshold_boundary(self):
        adx = DailyADX(threshold=25.0)
        adx.adx = 25.0
        self.assertFalse(adx.is_trending())
        adx.adx = 25.0001
        self.assertTrue(adx.is_trending())


if __name__ == '__main__':
    unittest.main()
