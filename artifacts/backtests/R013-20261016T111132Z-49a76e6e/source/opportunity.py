"""R013 independent exit/stop/confirmation hypotheses; diagnostics never feed orders."""
from collections import defaultdict
from copy import deepcopy
import csv
import json
import statistics
from .archive import preserve
from .config import decimal_value as D, require
from .engine import HOUR, MINUTE
from .phase1 import CappedAccount
from .signal_quality import ExtensionAccount

CASES = ('control', 'hold168', 'stop3atr', 'confirm2')


class OpportunityAccount(CappedAccount):
    def __init__(self, *args, opportunity_case='control', **kwargs):
        require(opportunity_case in CASES, 'Unknown R013 case')
        config = deepcopy(args[0])
        if opportunity_case == 'hold168':
            config['strategies']['trend']['max_holding_hours'] = 168
        elif opportunity_case == 'stop3atr':
            config['strategies']['trend']['stop_atr_multiple'] = '3'
        super().__init__(config, *args[1:], **kwargs)
        self.opportunity_case = opportunity_case
        if opportunity_case != 'control':
            self.plan['design_id'] = 'R013-v1'
            self.plan['efficiency24'].update(changed_field=opportunity_case, value=opportunity_case)
        self.confirmation_history = {}
        self.initial_price_risks = {}
        self.pending_labels = defaultdict(list)
        self.direction_labels = []

    def decisions(self, available, features):
        if self.opportunity_case in ('control', 'hold168'):
            return super().decisions(available, features)
        due = available + MINUTE
        for key, item in list(self.direction_slots.pending.items()):
            if item['due'] == due:
                self.direction_slots.release(key)
        ExtensionAccount.decisions(self, available, features)
        if due not in self.queue:
            self.confirmation_history.clear()
            return
        actions = self.queue[due]
        current = {(a['symbol'], a['side']) for a in actions if a['kind'] == 'entry'}
        previous = self.confirmation_history.get(available - HOUR, set())
        self.confirmation_history = {t: batch for t, batch in self.confirmation_history.items()
                                     if t >= available - HOUR}
        self.confirmation_history[available] = current
        allowed = []
        for action in actions:
            if action['kind'] != 'entry':
                allowed.append(action)
                continue
            symbol, side = action['symbol'], action['side']
            if self.opportunity_case == 'confirm2':
                confirmed = (symbol, side) in previous
                self.gate_record({'gate': 'R013_two_qualified_hours', 'available_us': available,
                                 'symbol': symbol, 'side': side, 'accepted': confirmed})
                if not confirmed:
                    self.rejects['R013:second_qualified_hour_missing'] += 1
                    continue
            else:
                feature = features[symbol]
                stop = feature['close'] + (-3 if side == 'long' else 3) * feature['atr']
                if stop <= 0:
                    self.rejects['R013:nonpositive_wide_stop'] += 1
                    continue
                action = {**action, 'stop': format(stop, '.10f')}
            key = f'{due}:{symbol}:{side}'
            positions = self.ledger._state['positions']
            count = self.direction_slots.count(positions, side)
            accepted = self.direction_slots.reserve(key, symbol, side, due, positions)
            self.record_slot(action, 'signal', available, accepted, count)
            if accepted:
                allowed.append({**action, '_direction_slot': key})
        self.queue[due] = allowed

    def entry(self, symbol, t, action, data):
        previous = self.meta.get(symbol)
        super().entry(symbol, t, action, data)
        if symbol not in self.meta or self.meta[symbol] is previous:
            return
        position = self.ledger._state['positions'][symbol]
        entry_time = self.meta[symbol]['time']
        risk = abs(position['quantity']) * abs(position['entry'] - position['stop'])
        require(risk > 0, 'Missing initial R013 price risk')
        self.initial_price_risks[symbol, entry_time] = risk
        minute = (t // MINUTE) * MINUTE
        target = minute + 24 * HOUR
        label = {'symbol': symbol, 'side': action['side'], 'entry_time_us': entry_time,
                 'entry_minute_us': minute, 'label_time_us': target,
                 'raw_entry_open': data[symbol]['trade']['open']}
        if target < self.ledger.end:
            self.pending_labels[target].append(label)
        else:
            self.direction_labels.append({**label, 'known': False,
                                          'raw_future_open': '', 'signed_return': '', 'correct': ''})

    def minute(self, t, data, funds, last):
        # Label a fixed 24h horizon using this minute's observed open, even if closed earlier.
        # Labels are only reporting state: decisions/size/stop never read them.
        for label in self.pending_labels.pop(t, []):
            future = D(data[label['symbol']]['trade']['open'])
            signed = (future / D(label['raw_entry_open']) - 1) * (1 if label['side'] == 'long' else -1)
            self.direction_labels.append({**label, 'known': True, 'raw_future_open': str(future),
                                          'signed_return': str(signed), 'correct': signed > 0})
        return super().minute(t, data, funds, last)

    def finish(self, folder):
        require(not self.pending_labels, 'Unresolved within-window direction labels')
        metrics = super().finish(folder)
        rows = []
        for trade in self.trades:
            risk = self.initial_price_risks[trade['symbol'], int(trade['entry_time_us'])]
            rows.append({'symbol': trade['symbol'], 'entry_time_us': trade['entry_time_us'],
                         'initial_price_risk_usdt': str(risk), 'net_pnl': trade['net_pnl'],
                         'net_R': float(D(trade['net_pnl']) / risk)})
        values = [x['net_R'] for x in rows]
        wins, losses = [x for x in values if x > 0], [-x for x in values if x <= 0]
        known = [x for x in self.direction_labels if x['known']]
        extra = {'case': self.opportunity_case,
                 'mean_net_trade_R': statistics.mean(values) if values else None,
                 'average_winner_R': statistics.mean(wins) if wins else None,
                 'average_loser_abs_R': statistics.mean(losses) if losses else None,
                 'mean_net_trade_usdt': sum(float(t['net_pnl']) for t in self.trades) / len(rows) if rows else None,
                 'direction_24h_labeled_entries': len(known),
                 'direction_24h_missing_entries': len(self.direction_labels) - len(known),
                 'direction_24h_hit_rate': sum(x['correct'] for x in known) / len(known) if known else None,
                 'direction_24h_labels_used_for_trading': False}
        for suffix, records in [('trade_expectancy', rows), ('direction_labels', self.direction_labels)]:
            with (folder / (self.name + '_' + suffix + '.csv')).open('x', encoding='utf-8-sig', newline='') as stream:
                if records:
                    writer = csv.DictWriter(stream, fieldnames=list(records[0]))
                    writer.writeheader()
                    writer.writerows(records)
        metrics = {**metrics, **extra}
        preserve(folder / (self.name + '_expectancy_metrics.json'), json.dumps(metrics, indent=2).encode())
        return metrics
