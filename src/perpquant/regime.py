"""R011 market regime filter: Wilder ADX(14) on BTC daily bars.

Pre-registered design (no tuning):
- Indicator: Wilder ADX(14), textbook formulation.
- Timeframe: daily bars built from minute trade data (UTC midnight close).
- Symbol: BTCUSDT as the global regime proxy (altcoins correlate ~1 in risk events).
- Threshold: ADX > 25 = trending (Wilder's textbook level). Not tuned.
- Gate: block NEW entries when not trending; exits and position management unchanged.
- Fail-closed: if ADX not ready (warmup), block entries.
- No lookahead: only closed daily bars feed the calculator; decisions() sees
  the ADX computed from the last fully closed daily bar.
"""
from .config import require

ADX_PERIOD = 14
ADX_TREND_THRESHOLD = 25.0
REGIME_SYMBOL = "BTCUSDT"


class DailyADX:
    """Incremental Wilder ADX(14). Feed minute (high, low, close, day_id);
    a day closes when day_id changes (UTC midnight)."""

    def __init__(self, period=ADX_PERIOD, threshold=ADX_TREND_THRESHOLD):
        require(type(period) is int and period > 0, "Invalid ADX period")
        self.period = period
        self.threshold = float(threshold)
        self._day_high = self._day_low = self._day_close = self._day_id = None
        self._pdh = self._pdl = self._pdc = None
        self._tr_seed, self._pdm_seed, self._mdm_seed, self._dx_seed = [], [], [], []
        self._str = self._spdm = self._smdm = self._adx = None
        self.adx = None
        self.closed_days = 0

    def push_minute(self, high, low, close, day_id):
        """Returns True when a daily bar just closed."""
        if self._day_id is None:
            self._day_high, self._day_low = high, low
            self._day_close, self._day_id = close, day_id
            return False
        if day_id != self._day_id:
            self._close_day()
            self._day_high, self._day_low = high, low
            self._day_close, self._day_id = close, day_id
            return True
        if high > self._day_high:
            self._day_high = high
        if low < self._day_low:
            self._day_low = low
        self._day_close = close
        return False

    def _close_day(self):
        high, low, close = self._day_high, self._day_low, self._day_close
        if self._pdh is None:
            # First observed day: store OHLC; DM/TR need a previous day.
            self._pdh, self._pdl, self._pdc = high, low, close
            return
        up_move = high - self._pdh
        down_move = self._pdl - low
        pdm = up_move if up_move > down_move and up_move > 0 else 0.0
        mdm = down_move if down_move > up_move and down_move > 0 else 0.0
        tr = max(high - low, abs(high - self._pdc), abs(low - self._pdc))
        self._pdh, self._pdl, self._pdc = high, low, close
        self.closed_days += 1
        if self._str is None:
            self._tr_seed.append(tr)
            self._pdm_seed.append(pdm)
            self._mdm_seed.append(mdm)
            if len(self._tr_seed) == self.period:
                self._str = sum(self._tr_seed) / self.period
                self._spdm = sum(self._pdm_seed) / self.period
                self._smdm = sum(self._mdm_seed) / self.period
            return
        self._str += (tr - self._str) / self.period
        self._spdm += (pdm - self._spdm) / self.period
        self._smdm += (mdm - self._smdm) / self.period
        if self._str <= 0:
            return
        pdi = 100.0 * self._spdm / self._str
        mdi = 100.0 * self._smdm / self._str
        di_sum = pdi + mdi
        dx = 100.0 * abs(pdi - mdi) / di_sum if di_sum > 0 else 0.0
        if self._adx is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) == self.period:
                self._adx = sum(self._dx_seed) / self.period
                self.adx = self._adx
            return
        self._adx += (dx - self._adx) / self.period
        self.adx = self._adx

    def is_trending(self):
        """Fail-closed: None ADX blocks entries."""
        return self.adx is not None and self.adx > self.threshold
