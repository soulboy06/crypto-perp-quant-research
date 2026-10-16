"""R011 regime-gated account: CappedAccount + per-coin daily ADX(14) trend gate.

Gate semantics (pre-registered, per-coin):
- Each coin has its own DailyADX(14) on its daily bars (UTC midnight).
- Trending (coin ADX > 25): entries allowed for that coin.
- Choppy (ADX <= 25 or not ready): NEW entries for that coin blocked;
  exits and position management unchanged. Blocked entries release slots.
- Fail-closed per coin.
- No lookahead: only closed daily bars feed each calculator.
"""
from .engine import MINUTE, DAY_US
from .phase1 import CappedAccount
from .regime import DailyADX, ADX_TREND_THRESHOLD
from .universe_data import SYMBOLS


class RegimeAccount(CappedAccount):
    def __init__(self, *args, regime_threshold=ADX_TREND_THRESHOLD, **kwargs):
        self.regimes = {s: DailyADX(threshold=regime_threshold) for s in SYMBOLS}
        self.regime_threshold = float(regime_threshold)
        self.regime_blocks = 0
        self.regime_allows = 0
        super().__init__(*args, **kwargs)
        self.plan['design_id'] = 'R011-v1-percoin'
        self.plan['efficiency24'].update(
            changed_field='additional_per_coin_regime_gate',
            value=f'each_coin_daily_ADX14_gt_{regime_threshold}_blocks_new_entries')

    def warmup_minute(self, t, data):
        """Feed each coin's minute OHLC into its own regime tracker."""
        day_id = t // DAY_US
        for symbol in SYMBOLS:
            row = data[symbol]['trade']
            self.regimes[symbol].push_minute(float(row['high']), float(row['low']),
                                            float(row['close']), day_id)

    def minute(self, t, data, funds, last):
        self.warmup_minute(t, data)
        return super().minute(t, data, funds, last)

    def coin_trending(self, symbol):
        return self.regimes[symbol].is_trending()

    def decisions(self, available, features):
        super().decisions(available, features)
        due = available + MINUTE
        if due not in self.queue:
            return
        allowed = []
        for action in self.queue[due]:
            if action['kind'] != 'entry':
                allowed.append(action)
                continue
            symbol = action['symbol']
            if self.coin_trending(symbol):
                self.regime_allows += 1
                allowed.append(action)
                continue
            # Choppy for this coin: block entry, release its slot.
            key = action.get('_direction_slot')
            if key:
                self.direction_slots.release(key)
            self.regime_blocks += 1
            reason = 'R011:choppy_regime_block_new_entry'
            self.gate_counts[reason] += 1
            self.gate_record({'gate': 'per_coin_regime_adx', 'time_us': available,
                             'symbol': symbol, 'side': action['side'],
                             'adx': self.regimes[symbol].adx,
                             'threshold': self.regime_threshold, 'accepted': False})
            self.rejects[reason] += 1
        self.queue[due] = allowed
