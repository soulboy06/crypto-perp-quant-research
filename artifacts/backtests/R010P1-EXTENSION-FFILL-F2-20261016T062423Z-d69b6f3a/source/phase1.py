"""R010 Phase 1: a slot gate around unchanged SIG-001 decisions and execution."""
from .config import require
from .engine import MINUTE
from .signal_quality import ExtensionAccount


class DirectionSlots:
    """Reservations are admission slots, never cash/margin reservations."""

    def __init__(self, cap=3):
        require(type(cap) is int and cap > 0, 'Invalid direction cap')
        self.cap = cap
        self.pending = {}

    def count(self, positions, side, excluding=None):
        require(side in ('long', 'short'), 'Invalid side')
        held = sum((p['quantity'] > 0) == (side == 'long')
                   for p in positions.values() if p['quantity'] != 0)
        waiting = sum(x['side'] == side for key, x in self.pending.items()
                      if key != excluding)
        return held + waiting

    def reserve(self, key, symbol, side, due, positions):
        require(key not in self.pending, 'Duplicate direction reservation')
        if self.count(positions, side) >= self.cap:
            return False
        self.pending[key] = {'symbol': symbol, 'side': side, 'due': due}
        return True

    def can_fill(self, key, positions):
        require(key in self.pending, 'Missing direction reservation')
        side = self.pending[key]['side']
        return self.count(positions, side, excluding=key) + 1 <= self.cap

    def release(self, key):
        self.pending.pop(key, None)

    def clear_due(self, due):
        for key, x in list(self.pending.items()):
            if x['due'] <= due:
                self.release(key)


class CappedAccount(ExtensionAccount):
    def __init__(self, *args, direction_cap=3, **kwargs):
        self.direction_slots = DirectionSlots(direction_cap)
        self.maximum_observed_same_direction = 0
        super().__init__(*args, **kwargs)
        self.plan['design_id'] = 'R010-P1-v1'
        # The inherited SIG-001 distance gate remains active and unchanged.
        self.plan['efficiency24'].update(
            changed_field='additional_same_direction_admission_cap',
            value=str(direction_cap))

    def record_slot(self, action, phase, time_us, accepted, count):
        reason = 'R010P1:' + phase + '_same_direction_cap'
        self.gate_counts[reason + ('_pass' if accepted else '_reject')] += 1
        self.gate_record({'gate': 'same_direction_slot_cap', 'phase': phase,
                         'time_us': time_us, 'symbol': action['symbol'],
                         'side': action['side'], 'other_same_direction_slots': count,
                         'cap': self.direction_slots.cap, 'accepted': accepted})
        if not accepted:
            self.rejects[reason] += 1

    def decisions(self, available, features):
        due = available + MINUTE
        # Decisions normally run once per hour. Replacing a batch must not leak slots.
        for key, x in list(self.direction_slots.pending.items()):
            if x['due'] == due:
                self.direction_slots.release(key)
        super().decisions(available, features)
        if due not in self.queue:
            return
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
            self.record_slot(action, 'signal', available, accepted, count)
            if accepted:
                allowed.append({**action, '_direction_slot': key})
        self.queue[due] = allowed

    def entry(self, symbol, t, action, data):
        key = action['_direction_slot']
        slots = self.direction_slots
        try:
            count = slots.count(self.ledger._state['positions'], action['side'], key)
            accepted = slots.can_fill(key, self.ledger._state['positions'])
            self.record_slot(action, 'fill', t, accepted, count)
            if accepted:
                # Inherit sizing, fees, funding, liquidation checks and all fills.
                super().entry(symbol, t, action, data)
                positions = self.ledger._state['positions']
                observed = max(sum(p['quantity'] > 0 for p in positions.values()),
                               sum(p['quantity'] < 0 for p in positions.values()))
                self.maximum_observed_same_direction = max(
                    self.maximum_observed_same_direction, observed)
                require(observed <= slots.cap, 'Direction cap exceeded after fill')
        finally:
            # Success converts reservation to a position; every refusal frees it.
            slots.release(key)

    def minute(self, t, data, funds, last):
        try:
            return super().minute(t, data, funds, last)
        finally:
            # Also release skipped terminal batches or batches aborted by protection.
            self.direction_slots.clear_due(t)

    def finish(self, folder):
        require(not self.direction_slots.pending, 'Leaked direction reservations')
        return super().finish(folder)
