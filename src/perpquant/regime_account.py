"""R011 regime-gated account: CappedAccount + BTC daily ADX(14) trend gate.

Gate semantics (pre-registered):
- Trending (BTC daily ADX > 25): entries allowed, all existing logic unchanged.
- Choppy (ADX <= 25 or ADX not ready): NEW entries blocked; exits and position
  management unchanged. Blocked entries release any direction slots they reserved.
- Warmup: the runner feeds warmup minutes via warmup_minute() so ADX is ready
  by window start. Fail-closed if ADX is None.
"""
from .config import require
from .engine import MINUTE, DAY_US
from .phase1 import CappedAccount
from .regime import DailyADX, REGIME_SYMBOL, ADX_TREND_THRESHOLD


class RegimeAccount(CappedAccount):
    def __init__(self, *args, regime_threshold=ADX_TREND_THRESHOLD, **kwargs):
        self.regime = DailyADX(threshold=regime_threshold)
        self.regime_threshold = float(regime_threshold)
        self.regime_blocks = 0
        self.regime_allows = 0
        super().__init__(*args, **kwargs)
        self.plan['design_id'] = 'R011-v1'
        self.plan['efficiency24'].update(
            changed_field='additional_market_regime_gate',
            value=f'BTC_daily_ADX14_gt_{regime_threshold}_blocks_new_entries')

    def warmup_minute(self, t, data):
        """Feed BTC minute OHLC into the regime tracker. No trading logic."""
        row = data[REGIME_SYMBOL]['trade']
        self.regime.push_minute(float(row['high']), float(row['low']),
                                float(row['close']), t // DAY_US)

    def minute(self, t, data, funds, last):
        self.warmup_minute(t, data)
        return super().minute(t, data, funds, last)

    def decisions(self, available, features):
        super().decisions(available, features)
        due = available + MINUTE
        if due not in self.queue:
            return
        trending = self.regime.is_trending()
        if trending:
            self.regime_allows += 1
            return
        # Choppy regime: block new entries, keep exits. Release reserved slots.
        allowed = []
        for action in self.queue[due]:
            if action['kind'] != 'entry':
                allowed.append(action)
                continue
            key = action.get('_direction_slot')
            if key:
                self.direction_slots.release(key)
            self.regime_blocks += 1
            reason = 'R011:choppy_regime_block_new_entry'
            self.gate_counts[reason] += 1
            self.gate_record({'gate': 'market_regime_adx', 'time_us': available,
                             'symbol': action['symbol'], 'side': action['side'],
                             'adx': self.regime.adx, 'threshold': self.regime_threshold,
                             'accepted': False})
            self.rejects[reason] += 1
        self.queue[due] = allowed
