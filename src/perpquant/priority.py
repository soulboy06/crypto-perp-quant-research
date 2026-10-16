"""R012: one prespecified, symmetric allocation priority; no new entry filter."""
from collections import deque
import math
import statistics
from .config import require
from .engine import HOUR, MINUTE
from .phase1 import CappedAccount
from .research4 import FourthHour
from .signal_quality import ExtensionAccount

LOOKBACK = 168


class WeeklyMomentum:
    def __init__(self):
        self.returns = deque(maxlen=LOOKBACK)
        self.previous = self.last_available = None

    def push(self, available, close):
        close = float(close)
        require(math.isfinite(close) and close > 0, 'Invalid momentum close')
        require(available % HOUR == 0 and
                (self.last_available is None or available == self.last_available + HOUR),
                'Momentum requires consecutive closed hours')
        if self.previous is not None:
            self.returns.append(math.log(close / self.previous))
        self.previous, self.last_available = close, available
        score = None
        if len(self.returns) == LOOKBACK:
            volatility = statistics.stdev(self.returns)
            if volatility > 0:
                score = math.fsum(self.returns) / (volatility * math.sqrt(LOOKBACK))
        return {'available_us': available, 'lookback_hours': LOOKBACK, 'long_score': score}


class PriorityHour:
    def __init__(self):
        self.parent, self.momentum = FourthHour(), WeeklyMomentum()

    def push(self, row):
        closed, feature = self.parent.push(row)
        if closed:
            context = self.momentum.push(int(row['available_time_us']), row['close'])
            if feature is not None:
                feature = {**feature, 'momentum_priority': context}
        return closed, feature


def directional_score(feature, side, available):
    require(side in ('long', 'short'), 'Invalid priority side')
    context = feature.get('momentum_priority')
    if context is None:
        return None
    require(context['available_us'] <= available and context['lookback_hours'] == LOOKBACK,
            'Future or unsupported momentum context')
    score = context['long_score']
    if score is None:
        return None
    require(math.isfinite(score), 'Nonfinite priority score')
    return score if side == 'long' else -score


class PriorityAccount(CappedAccount):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.plan['design_id'] = 'R012-v1'
        self.plan['efficiency24'].update(changed_field='same_hour_signal_priority_only',
                                        value='symmetric_weekly_volatility_normalized_momentum')
        self.priority_batches = self.reordered_batches = 0

    def decisions(self, available, features):
        due = available + MINUTE
        for key, item in list(self.direction_slots.pending.items()):
            if item['due'] == due:
                self.direction_slots.release(key)
        # SIG-001 produces the exact same eligible signal set. Sort before slots.
        ExtensionAccount.decisions(self, available, features)
        if due not in self.queue:
            return
        actions = self.queue[due]
        exits = [a for a in actions if a['kind'] != 'entry']
        entries = [a for a in actions if a['kind'] == 'entry']
        scored = [(a, directional_score(features[a['symbol']], a['side'], available)) for a in entries]
        # Stable sort preserves original symbol priority for ties/unavailable data.
        ranked = sorted(scored, key=lambda item: float('inf') if item[1] is None else -item[1])
        if len(entries) > 1:
            self.priority_batches += 1
            self.reordered_batches += [a for a, _ in ranked] != entries
        allowed = list(exits)
        for rank, (action, score) in enumerate(ranked, 1):
            key = f"{due}:{action['symbol']}:{action['side']}"
            positions = self.ledger._state['positions']
            count = self.direction_slots.count(positions, action['side'])
            accepted = self.direction_slots.reserve(key, action['symbol'], action['side'], due, positions)
            self.gate_record({'gate': 'R012_priority', 'available_us': available,
                             'scheduled_entry_us': due, 'symbol': action['symbol'], 'side': action['side'],
                             'score': score, 'rank': rank, 'slot_accepted': accepted})
            self.record_slot(action, 'signal', available, accepted, count)
            if accepted:
                allowed.append({**action, '_direction_slot': key})
        self.queue[due] = allowed
