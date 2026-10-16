"""R011 regime-gated account: per-coin daily ADX(14) trend gate + direction slots.

Gate ORDER (critical): regime filter BEFORE slot reservation.
Otherwise choppy coins occupy slots, trending coins get rejected by cap,
then choppy ones are filtered -> zero entries despite valid signals.

- Each coin has its own DailyADX(14) on daily bars (UTC midnight).
- Trending (ADX > 25): entries allowed for that coin.
- Choppy: NEW entries blocked; exits unchanged. Fail-closed per coin.
- No lookahead: only closed daily bars.
"""
from .engine import MINUTE, DAY_US
from .phase1 import CappedAccount
from .signal_quality import ExtensionAccount
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

    def _apply_slot_filter(self, due):
        """Slot reservation (same logic as CappedAccount, applied after regime)."""
        allowed = []
        for action in self.queue[due]:
            if action['kind'] != 'entry':
                allowed.append(action)
                continue
            key = f"{due}:{action['symbol']}:{action['side']}"
            positions = self.ledger._state['positions']
            count = self.direction_slots.count(positions, action['side'])
            accepted = self.direction_slots.reserve(
                key, action['symbol'], action['side'], due, positions)
            # Record slot gate (reuse CappedAccount's recorder).
            self.record_slot(action, 'signal', due - MINUTE, accepted, count)
            if accepted:
                allowed.append({**action, '_direction_slot': key})
        self.queue[due] = allowed

    def decisions(self, available, features):
        # Step 1: SIG-001 signal generation WITHOUT slot reservation.
        # Call grandparent directly to skip CappedAccount's slot-first ordering.
        ExtensionAccount.decisions(self, available, features)
        due = available + MINUTE
        if due not in self.queue:
            return
        # Step 2: regime filter FIRST.
        regime_passed = []
        for action in self.queue[due]:
            if action['kind'] != 'entry':
                regime_passed.append(action)
                continue
            symbol = action['symbol']
            if self.coin_trending(symbol):
                self.regime_allows += 1
                regime_passed.append(action)
            else:
                self.regime_blocks += 1
                reason = 'R011:choppy_regime_block_new_entry'
                self.gate_counts[reason] += 1
                self.gate_record({'gate': 'per_coin_regime_adx', 'time_us': available,
                                 'symbol': symbol, 'side': action['side'],
                                 'adx': self.regimes[symbol].adx,
                                 'threshold': self.regime_threshold, 'accepted': False})
                self.rejects[reason] += 1
        self.queue[due] = regime_passed
        # Step 3: slot reservation on regime-passed entries only.
        self._apply_slot_filter(due)
