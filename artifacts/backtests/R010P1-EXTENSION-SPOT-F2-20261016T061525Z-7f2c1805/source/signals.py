"""Fixed R001 indicators, updated only on closed hourly candles."""
from collections import deque
import math


class Indicators:
    def __init__(self):
        self.history = deque(maxlen=200)
        self.highs = deque(maxlen=48)
        self.lows = deque(maxlen=48)
        self.closes = deque(maxlen=20)
        self.tr_seed = []
        self.ema200 = self.ema50 = self.atr = self.previous = None

    def push(self, op, high, low, close):
        prior_high = max(self.highs) if len(self.highs) == 48 else None
        prior_low = min(self.lows) if len(self.lows) == 48 else None
        tr = high-low if self.previous is None else max(high-low,abs(high-self.previous),abs(low-self.previous))
        self.history.append(close)
        self.closes.append(close)
        if self.ema200 is None and len(self.history) >= 200:
            self.ema200 = sum(self.history)/200
        elif self.ema200 is not None:
            self.ema200 += 2/201*(close-self.ema200)
        if self.ema50 is None and len(self.history) >= 50:
            self.ema50 = sum(list(self.history)[-50:])/50
        elif self.ema50 is not None:
            self.ema50 += 2/51*(close-self.ema50)
        if self.atr is None:
            self.tr_seed.append(tr)
            if len(self.tr_seed) == 14:
                self.atr = sum(self.tr_seed)/14
        else:
            self.atr += (tr-self.atr)/14
        self.highs.append(high)
        self.lows.append(low)
        self.previous = close
        if self.ema200 is None or self.atr is None or self.atr <= 0 or prior_high is None or len(self.closes) < 20:
            return None
        mean = sum(self.closes)/20
        std = math.sqrt(sum((c-mean)**2 for c in self.closes)/20)
        trend = "long" if close>prior_high and close>self.ema200 else "short" if close<prior_low and close<self.ema200 else None
        z = (close-mean)/std if std > 0 else None
        mr = None
        if z is not None and abs(close-self.ema200) <= 2*self.atr:
            mr = "long" if z <= -2 else "short" if z >= 2 else None
        return {"close":close,"atr":self.atr,"ema50":self.ema50,"ema200":self.ema200,
                "mean":mean,"z":z,"prior_high":prior_high,"prior_low":prior_low,
                "trend":trend,"mean_reversion":mr}


class Hour:
    def __init__(self):
        self.count = 0
        self.indicators = Indicators()

    def push(self, row):
        op,high,low,close = [float(row[k]) for k in ("open","high","low","close")]
        if self.count == 0:
            self.op,self.high,self.low = op,high,low
        self.high,self.low = max(self.high,high),min(self.low,low)
        self.count += 1
        if self.count < 60:
            return False,None
        self.count = 0
        return True,self.indicators.push(self.op,self.high,self.low,close)
