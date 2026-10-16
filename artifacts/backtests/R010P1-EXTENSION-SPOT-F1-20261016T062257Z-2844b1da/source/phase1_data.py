"""Separate R010 extension corpus; no replacement of frozen universe manifests."""
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import gzip
import json
import tomllib
from .archive import preserve
from .config import require, decimal_value as D
from .engine import stamp_us
from .full_data import specs_for, sha, relative
from .reconcile import repair, normalized, compressed_rows
from .universe_data import SYMBOLS, load_plan, prepare_new_one


def load_phase_plan(root):
    p = tomllib.loads((root / 'configs/phase1.toml').read_text(encoding='utf-8'))
    require(p['design_id'] == 'R010-P1-v1' and p['phase'] == 1
            and p['maximum_same_direction_positions'] == 3
            and p['maximum_accounts_per_run'] == 4
            and p['cost_scenarios'] == ['base', 'stress']
            and not p['parameter_search'] and not p['phase_2_enabled']
            and p['holdout_locked'] and not p['formal_selection_eligible'],
            'Phase 1 scope changed')
    require(p['extension']['warmup_start'] == '2024-05-01T00:00:00Z'
            and p['extension']['start'] == '2024-07-01T00:00:00Z'
            and p['extension']['end_exclusive'] == p['true_holdout_start']
            == '2026-01-01T00:00:00Z', 'Extension dates changed')
    require(p['gates']['baseline_control'] == 'same_window_SIG001_rerun',
            'Phase 1 baseline changed')
    return p


def window_config(root, scope):
    require(scope in ('extension', 'reproduction'), 'Unknown Phase 1 scope')
    p = load_phase_plan(root)
    universe, _, config = load_plan(root)
    window = p[scope]
    config['data'].update(warmup_start=window['warmup_start'],
                          development_start=window['start'],
                          development_end=window['end_exclusive'])
    require(stamp_us(window['end_exclusive']) <= stamp_us(p['true_holdout_start']),
            'Frozen 2026 access forbidden')
    return p, universe, config


def cached_records(root):
    cache = {}
    folders = ['full_runs', 'universe_runs', 'phase1_runs']
    for name in folders:
        for path in sorted((root / 'artifacts/data' / name).glob('*/manifest.json')):
            payload = json.loads(path.read_text(encoding='utf-8'))
            for x in payload.get('results', []):
                if x.get('audit_passed') and (root / x['normalized_path']).is_file():
                    cache[x['symbol'], x['kind'], x['period']] = x
        for path in sorted((root / 'artifacts/data' / name).glob('*/progress.jsonl')):
            for line in path.read_text(encoding='utf-8').splitlines():
                x = json.loads(line)
                if x.get('audit_passed') and (root / x['normalized_path']).is_file():
                    cache[x['symbol'], x['kind'], x['period']] = x
    return cache


def prepare_partition(root, config, spec):
    x = prepare_new_one(root, config, spec)
    if (not x['audit_passed'] and spec.kind in ('klines', 'markPriceKlines')
            and 'Missing or extra minute bars' in x.get('error', '')):
        try:
            return repair(root, config, x)
        except Exception as exc:
            x['repair_error'] = f'{type(exc).__name__}: {exc}'
    return x


def value_funding(root, records, config):
    index = {(x['symbol'], x['kind'], x['period']): x for x in records}
    periods = sorted({x['period'] for x in records})
    start = stamp_us(config['data']['development_start'])
    values = []
    for symbol in SYMBOLS:
        previous_mark = previous_event = previous_interval = None
        for period in periods:
            mark_item, fund_item = [index[symbol, k, period]
                                    for k in ('markPriceKlines', 'fundingRate')]
            marks, funds = [normalized(root, x) for x in (mark_item, fund_item)]
            available = [int(x['available_time_us']) for x in marks]
            if previous_mark is not None:
                require(int(marks[0]['open_time_us']) ==
                        int(previous_mark['available_time_us']), 'Mark month gap')
            body = (root / fund_item['funding_rest_path']).read_bytes()
            require(sha(body) == fund_item['funding_rest_sha256'], 'Funding REST changed')
            exact = {int(x['fundingTime']) * 1000: x.get('markPrice')
                     for x in json.loads(body)}
            for x in funds:
                event, interval = int(x['event_time_us']), int(x['interval_hours'])
                if previous_event is not None:
                    require(any(abs(event - previous_event - h * 3_600_000_000)
                                <= 60_000_000 for h in (interval, previous_interval)),
                            'Funding month gap')
                previous_event, previous_interval = event, interval
                if event < start:
                    continue
                require(event in exact, 'Funding event absent from accepted REST evidence')
                if exact[event] not in (None, ''):
                    mark, source, is_exact = str(D(exact[event])), event, True
                else:
                    j = bisect_right(available, event) - 1
                    last = marks[j] if j >= 0 else previous_mark
                    require(last is not None, 'No past funding mark')
                    mark, source, is_exact = last['close'], int(last['available_time_us']), False
                require(D(mark) > 0 and 0 <= event - source <= 60_000_000,
                        'Invalid funding valuation')
                values.append({'symbol': symbol, 'event_time_us': event,
                               'rate': x['rate'], 'interval_hours': interval,
                               'settlement_mark': mark, 'exact_mark': str(is_exact),
                               'source_available_us': source, 'age_us': event - source})
            previous_mark = marks[-1]
    values.sort(key=lambda x: (int(x['event_time_us']), x['symbol']))
    return compressed_rows(values), len(values)


def prepare(root):
    root = Path(root).resolve()
    plan, _, config = window_config(root, 'extension')
    specs = specs_for(config)
    require(len(specs) == 600, 'Unexpected extension inventory')
    run_id = 'R010-DATA-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/data/phase1_runs' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('phase1_data.py', 'universe_data.py', 'reconcile.py', 'full_data.py', 'quality.py'):
        preserve(folder / 'source' / name, Path(__file__).with_name(name).read_bytes())
    preserve(folder / 'source/phase1.toml', (root / 'configs/phase1.toml').read_bytes())
    cache = cached_records(root)
    records, missing = [], []
    for spec in specs:
        x = cache.get((spec.symbol, spec.kind, spec.period))
        # Public clones include canonical data but intentionally omit raw API bodies.
        # Funding must reacquire that evidence before rebuilding settlement valuations.
        if x and spec.kind == 'fundingRate' and not (root / x['funding_rest_path']).is_file():
            x = None
        if x:
            require(sha((root / x['normalized_path']).read_bytes()) == x['normalized_sha256'],
                    'Cached normalized partition changed')
            records.append(x)
        else:
            missing.append(spec)
    # Probe one source from each missing class before scheduling the full inventory.
    # Unrecoverable marks or REST refusal cannot be fixed by downloading more months.
    probes = []
    for kind in ('markPriceKlines', 'fundingRate', 'klines'):
        item = next((x for x in missing if x.kind == kind), None)
        if item:
            probes.append(item)
    print(json.dumps({'run_id': run_id, 'cached': len(records), 'missing': len(missing),
                      'probe': [[x.symbol, x.kind, x.period] for x in probes]}), flush=True)
    errors = []
    with (folder / 'progress.jsonl').open('x', encoding='utf-8') as journal:
        def collect(batch):
            with ThreadPoolExecutor(max_workers=4) as pool:
                for x in pool.map(lambda s: prepare_partition(root, config, s), batch):
                    journal.write(json.dumps(x, ensure_ascii=False) + '\n')
                    journal.flush()
                    records.append(x)
                    if not x['audit_passed']:
                        errors.append(x)
                    print(json.dumps({'partition': [x['symbol'], x['kind'], x['period']],
                                      'passed': x['audit_passed'], 'error': x.get('error'),
                                      'repair_error': x.get('repair_error')}), flush=True)
        collect(probes)
        if not errors:
            collect([x for x in missing if x not in probes])
    successful = [x for x in records if x.get('audit_passed')]
    complete = len(successful) == len(specs) and not errors
    old_pointer = json.loads((root / 'artifacts/data/universe_latest.json').read_text(encoding='utf-8'))
    old_path = root / old_pointer['manifest']
    require(sha(old_path.read_bytes()) == old_pointer['sha256'], 'Old data binding changed')
    old = json.loads(old_path.read_text(encoding='utf-8'))
    manifest = {'run_id': run_id, 'design_id': plan['design_id'],
                'scope': plan['extension']['scope'], 'window': plan['extension'],
                'plan_sha256': sha((root / 'configs/phase1.toml').read_bytes()),
                'holdout_history_requested': False, 'formal_selection_eligible': False,
                'expected_partitions': len(specs), 'accepted_partitions': len(successful),
                'not_attempted_partitions': len(specs) - len(records),
                'complete_extension_coverage': complete, 'results': records,
                'errors': [{k: x.get(k) for k in ('symbol', 'kind', 'period', 'error', 'repair_error')}
                           for x in errors],
                'rules_path': old['rules_path'], 'rules_sha256': old['rules_sha256']}
    if complete:
        try:
            body, count = value_funding(root, successful, config)
            path = root / 'data/validated/funding_valuation' / (sha(body) + '.csv.gz')
            preserve(path, body)
            manifest.update(funding_valuation_path=relative(root, path),
                            funding_valuation_sha256=sha(body), funding_events=count)
        except (OSError, ValueError) as exc:
            complete = False
            manifest['complete_extension_coverage'] = False
            manifest['errors'].append({'stage': 'funding_valuation',
                                       'error': f'{type(exc).__name__}: {exc}'})
    path = folder / 'manifest.json'
    preserve(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
    # Dedicated pointer records failures as well, never masquerading as a complete corpus.
    (root / 'artifacts/data/phase1_latest.json').write_text(json.dumps(
        {'manifest': relative(root, path), 'sha256': sha(path.read_bytes()),
         'complete': complete}, indent=2), encoding='utf-8')
    print(json.dumps({'manifest': str(path), 'complete': complete,
                      'accepted': len(successful), 'errors': manifest['errors']}), flush=True)
    return path, manifest
