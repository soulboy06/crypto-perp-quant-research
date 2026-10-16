"""Frozen descriptive follow-through study. No account simulation or optimization."""
import sys
sys.dont_write_bytecode = True
import csv, gzip, hashlib, itertools, json, statistics, time
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from uuid import uuid4

ROOT = Path(r'D:\量化')
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.signals import Hour

BASE = ROOT / 'artifacts/backtests/R004-20261015T081913Z-2a03f944'
MINUTE = 60_000_000
HOUR = 60 * MINUTE
START = 1672531200000000
END = 1767225600000000
MANIFEST = ROOT / 'artifacts/data/full_runs/RECONCILED-20261015T065056Z-d83c78dc/manifest.json'

def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def price(value, tick, buy, adverse):
    value = Decimal(str(value)) * (1 + adverse if buy else 1 - adverse)
    return float((value / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick)

def stop_price(feature, side, tick):
    value = Decimal(format(feature['close'] + (-2 if side == 'long' else 2) * feature['atr'], '.10f'))
    return float((value / tick).to_integral_value(rounding=ROUND_CEILING if side == 'long' else ROUND_FLOOR) * tick)

class Shadow:
    def __init__(self, t, symbol, side, entry, stop, tick, adverse, accepted=None, selected=False):
        self.t, self.symbol, self.side, self.entry, self.stop = t, symbol, side, entry, stop
        self.sign = 1 if side == 'long' else -1
        self.distance = abs(entry - stop)
        assert self.distance > 0 and self.sign * (entry - stop) > 0
        self.tick, self.adverse = tick, adverse
        self.accepted, self.selected = accepted, selected
        self.funding = 0.0
        self.fund_minutes = set()
        self.due = None
        self.mfe_net_r = 0.0
        self.mae_price_r = 0.0
        self.completed_minutes = 0

    def settle(self, funds):
        for f in funds:
            assert int(f['event_time_us']) >= self.t
            self.funding -= self.sign * float(f['settlement_mark']) * float(f['rate'])
            self.fund_minutes.add(int(f['event_time_us']) // MINUTE)

    def net(self, exit_price):
        return self.sign * (exit_price-self.entry) + self.funding - .0006*(self.entry+exit_price)

    def finish(self, t, reference, reason):
        fill = price(reference, self.tick, self.sign < 0, self.adverse)
        net_r = self.net(fill)/self.distance
        record = dict(entry_time_us=self.t, exit_time_us=t, symbol=self.symbol, side=self.side,
                      entry_price=self.entry, stop_price=self.stop, exit_price=fill,
                      exit_reason=reason, price_risk_per_unit=self.distance, net_r=net_r,
                      funding_per_unit=self.funding, net_per_unit=self.net(fill),
                      mfe_net_r_completed_minutes=self.mfe_net_r,
                      mae_price_r_completed_minutes=self.mae_price_r,
                      completed_minutes=self.completed_minutes,
                      accepted=self.accepted is not None, rejected_nonoverlap_selected=self.selected,
                      funding_stop_same_minute=(reason in ('stop','gap_stop') and t//MINUTE in self.fund_minutes))
        if self.accepted is not None:
            actual = self.accepted
            assert int(actual['exit_time_us']) == t, ('exit time', actual, record)
            assert actual['exit_reason'] == reason, ('reason', actual, record)
            assert abs(float(actual['entry_price'])-self.entry) < 1e-8
            assert abs(float(actual['stop_price'])-self.stop) < 1e-8
            assert abs(float(actual['exit_price'])-fill) < 1e-8
            assert abs(float(actual['net_pnl'])-self.net(fill)*float(actual['quantity'])) < 1e-7, ('net', actual, record)
            record['actual_net_pnl'] = float(actual['net_pnl'])
        return record

    def open_exit(self, t, op, last):
        crossed = op <= self.stop if self.sign > 0 else op >= self.stop
        reason = ('end_of_partition' if last else 'gap_stop' if crossed else
                  'time_limit' if t-self.t >= 72*HOUR else
                  'hourly_exit' if self.due == t else None)
        return self.finish(t, op, reason) if reason else None

    def intrabar(self, t, hi, lo):
        crossed = lo <= self.stop if self.sign > 0 else hi >= self.stop
        if crossed:
            return self.finish(t+MINUTE-1, self.stop, 'stop')
        # Exit minute deliberately excluded: OHLC cannot order favorable highs and stop touches.
        favorable, adverse = (hi, lo) if self.sign > 0 else (lo, hi)
        estimate = favorable*(1-float(self.adverse) if self.sign > 0 else 1+float(self.adverse))
        self.mfe_net_r = max(self.mfe_net_r, self.net(estimate)/self.distance)
        self.mae_price_r = min(self.mae_price_r, self.sign*(adverse-self.entry)/self.distance)
        self.completed_minutes += 1
        return None

def summarize(rows):
    values = [x['net_r'] for x in rows]
    positive = sum(max(0,v) for v in values)
    negative = -sum(min(0,v) for v in values)
    return {'n': len(rows), 'mean_net_r': statistics.mean(values) if values else None,
            'median_net_r': statistics.median(values) if values else None,
            'win_rate': sum(v>0 for v in values)/len(values) if values else None,
            'profit_factor_r': positive/negative if negative else None,
            'exit_reasons': dict(Counter(x['exit_reason'] for x in rows)),
            'funding_stop_same_minute': sum(x['funding_stop_same_minute'] for x in rows),
            'stop_previously_positive_n': sum(x['mfe_net_r_completed_minutes'] > 0 for x in rows if x['exit_reason']=='stop'),
            'stop_previously_half_r_n': sum(x['mfe_net_r_completed_minutes'] >= .5 for x in rows if x['exit_reason']=='stop'),
            'stop_previously_one_r_n': sum(x['mfe_net_r_completed_minutes'] >= 1 for x in rows if x['exit_reason']=='stop')}

def self_test():
    tick, adverse = Decimal('.01'), Decimal('.0002')
    p = Shadow(0, 'T', 'long', 100, 98, tick, adverse)
    assert p.intrabar(0, 110, 97)['mfe_net_r_completed_minutes'] == 0, 'Stop minute high must be excluded'
    p = Shadow(0, 'T', 'long', 100, 98, tick, adverse)
    p.intrabar(0, 103, 99)
    r = p.intrabar(MINUTE, 104, 97)
    assert r['mfe_net_r_completed_minutes'] > 1 and r['exit_time_us'] == 2*MINUTE-1
    p = Shadow(0, 'T', 'short', 100, 102, tick, adverse)
    assert p.open_exit(MINUTE, 104, False)['exit_reason'] == 'gap_stop'
    p = Shadow(0, 'T', 'long', 100, 98, tick, adverse)
    p.settle([{'event_time_us': MINUTE, 'settlement_mark': '100', 'rate': '.001'}])
    r = p.finish(MINUTE, 100, 'hourly_exit')
    assert abs(r['net_per_unit']-(-.02-.1-.0006*199.98)) < 1e-10
    print('4 diagnostic edge checks passed', flush=True)

def main():
    self_test()
    start_clock = time.monotonic()
    baseline = json.loads((BASE/'summary.json').read_text(encoding='utf-8'))
    assert digest(ROOT/'src/perpquant/signals.py') == baseline['source_sha256']['signals.py']
    assert digest(MANIFEST) == baseline['data_manifest_sha256']
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    periods = sorted({x['period'] for x in manifest['results']})
    assert len(periods) == 38 and periods[0] == '2022-11' and periods[-1] == '2025-12'
    index = {(x['symbol'],x['kind'],x['period']): x for x in manifest['results']}
    snapshot = json.loads((ROOT/'artifacts/rules/exchange_rules.json').read_text(encoding='utf-8'))
    ticks = {s: Decimal(next(f['tickSize'] for f in x['filters'] if f['filterType']=='PRICE_FILTER')) for s,x in snapshot['symbols'].items()}
    folder = Path(__file__).parent / ('DIAG-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8])
    folder.mkdir()
    plan = {'design_id': 'FOLLOWTHROUGH-001-v1', 'strategy_accounts_added': 0,
            'baseline': str(BASE), 'baseline_summary_sha256': digest(BASE/'summary.json'),
            'source_sha256': digest(Path(__file__)), 'data_manifest_sha256': digest(MANIFEST),
            'holdout_read': False, 'parameter_search': False,
            'shadow_exit_rules': 'original 2ATR initial stop, EMA50 hourly exit plus60s, hard72h deadline',
            'shadow_units': 'one dollar initial price-to-stop risk, fractional quantity without order minimum/step',
            'sample_rules': 'all actual reserve rejects; also first rejected per symbol while no selected rejected shadow remains open; selection uses only information available at entry',
            'costs': 'original base/stress adverse fills with tick rounding, .0006 fee each side, actual settled funding',
            'mfe_rules': 'maximum favorable trade candle price with adverse exit rate and fee/funding estimate in fully held non-exit minutes only; threshold >0, >=.5R and >=1R fixed here',
            'limitations': ['shadow ignores shared funds, account protections, liquidation and sizing; not an account return',
                            'repeated rejection attempts overlap and are correlated; nonoverlap only within each symbol/cost sample',
                            'R excludes fee/gap/funding buffers and differs from configured total risk budget',
                            'MFE excludes exit minute, is a lower-coverage excursion estimate, not a guaranteed fill or executable rule',
                            'development data repeatedly observed; no independent out-of-sample inference']}
    (folder/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    (folder/'source.py').write_bytes(Path(__file__).read_bytes())
    print(json.dumps({'folder':str(folder),'design_id':plan['design_id']}), flush=True)
    candidates = {c: defaultdict(list) for c in ['base','stress']}
    actual = {}
    inputs = {}
    for cost in candidates:
        trades_path = BASE/f'efficiency24_{cost}_trades.csv'
        sizing_path = BASE/f'efficiency24_{cost}_sizing.jsonl'
        inputs[cost] = {'trades_sha256':digest(trades_path),'sizing_sha256':digest(sizing_path)}
        actual[cost] = {(int(x['entry_time_us']),x['symbol'],x['side']):x for x in csv.DictReader(trades_path.open(encoding='utf-8-sig'))}
        for line in sizing_path.read_text(encoding='utf-8').splitlines():
            x = json.loads(line)
            assert START <= x['time_us'] < END
            if x['reserve_error'] is None:
                assert (x['time_us'],x['symbol'],x['side']) in actual[cost]
            candidates[cost][x['time_us']].append(x)
    with gzip.open(ROOT/manifest['funding_valuation_path'],'rt',encoding='utf-8') as f:
        funds = list(csv.DictReader(f))
    assert all(START <= int(x['event_time_us']) < END for x in funds)
    funding_index = 0
    hours = {s: Hour() for s in ticks}
    features = {s:None for s in ticks}
    active = {c:[] for c in candidates}
    finished = {c:[] for c in candidates}
    previous = None
    for period in periods:
        with ExitStack() as stack:
            symbols = ['BTCUSDT','ETHUSDT']
            readers = [csv.DictReader(stack.enter_context(gzip.open(ROOT/index[s,'klines',period]['normalized_path'],'rt',encoding='utf-8'))) for s in symbols]
            for bundle in itertools.zip_longest(*readers):
                assert all(x is not None for x in bundle)
                t = int(bundle[0]['open_time_us'])
                assert int(bundle[1]['open_time_us']) == t and (previous is None or t == previous+MINUTE) and t < END
                previous = t
                rows = dict(zip(symbols,bundle))
                numeric = {s: tuple(float(rows[s][k]) for k in ['open','high','low']) for s in symbols}
                if t >= START:
                    current_funds = defaultdict(list)
                    while funding_index < len(funds) and int(funds[funding_index]['event_time_us']) < t+MINUTE:
                        f = funds[funding_index]
                        assert int(f['event_time_us']) >= t
                        current_funds[f['symbol']].append(f)
                        funding_index += 1
                    for cost in candidates:
                        retained = []
                        for p in active[cost]:
                            p.settle(current_funds[p.symbol])
                            r = p.open_exit(t,numeric[p.symbol][0],t==END-MINUTE)
                            if r: finished[cost].append(r)
                            else: retained.append(p)
                        active[cost] = retained
                        for x in candidates[cost].pop(t,[]):
                            s, side = x['symbol'], x['side']
                            f = features[s]
                            assert (t-MINUTE)%HOUR == 0 and f is not None
                            adverse = Decimal('.0002' if cost=='base' else '.0006')
                            entry = price(rows[s]['open'], ticks[s], side=='long', adverse)
                            stop = stop_price(f,side,ticks[s])
                            accepted = actual[cost].get((t,s,side)) if x['reserve_error'] is None else None
                            selected = accepted is None and not any(p.symbol==s and p.selected for p in active[cost])
                            active[cost].append(Shadow(t,s,side,entry,stop,ticks[s],adverse,accepted,selected))
                        retained = []
                        for p in active[cost]:
                            _,hi,lo = numeric[p.symbol]
                            r = p.intrabar(t,hi,lo)
                            if r: finished[cost].append(r)
                            else: retained.append(p)
                        active[cost] = retained
                for s in symbols:
                    closed,feature = hours[s].push(rows[s])
                    if closed: features[s] = feature
                if closed and START <= t+MINUTE < END-MINUTE:
                    for cost in active:
                        for p in active[cost]:
                            f = features[p.symbol]
                            if f is not None and (f['close']<f['ema50'] if p.sign>0 else f['close']>f['ema50']):
                                p.due = t+2*MINUTE
        print(json.dumps({'through':period,'completed':{c:len(v) for c,v in finished.items()}}),flush=True)
    assert funding_index == len(funds)
    assert not any(active.values()) and not any(candidates[c] for c in candidates)
    result = {'plan': plan, 'inputs':inputs, 'self_tests_passed':4, 'results':{}, 'runtime_seconds':time.monotonic()-start_clock}
    for cost,records in finished.items():
        assert sum(x['accepted'] for x in records) == len(actual[cost])
        subsets = {'accepted_control':[x for x in records if x['accepted']],
                   'rejected_all':[x for x in records if not x['accepted']],
                   'rejected_nonoverlap':[x for x in records if x['rejected_nonoverlap_selected']]}
        result['results'][cost] = {}
        for name,group in subsets.items():
            result['results'][cost][name] = {'overall':summarize(group),
                'by_symbol':{s:summarize([x for x in group if x['symbol']==s]) for s in symbols},
                'by_entry_year':{str(y):summarize([x for x in group if datetime.fromtimestamp(x['entry_time_us']/1e6,timezone.utc).year==y]) for y in [2023,2024,2025]}}
        with (folder/f'{cost}_paths.csv').open('x',newline='',encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f,fieldnames=list(records[0]))
            # Accepted rows have an additional validation field.
            if 'actual_net_pnl' not in writer.fieldnames: writer.fieldnames.append('actual_net_pnl')
            writer.writeheader()
            writer.writerows(records)
    (folder/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (Path(__file__).parent/'latest_diagnostic.json').write_text(json.dumps({'folder':str(folder)},ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'completed':str(folder),'seconds':result['runtime_seconds'],'accepted_controls_validated':True}),flush=True)

if __name__ == '__main__':
    if '--self-test' in sys.argv: self_test()
    else: main()
