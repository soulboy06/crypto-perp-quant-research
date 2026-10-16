"""Normal bounded REST pagination restores independent archive/API cross-checks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json
from .archive import preserve, allowed_development
from .config import require, decimal_value as D
from .full_data import MonthlySpec, fetch, sha, relative
from .quality import utc_us, crosscheck_funding
from .reconcile import normalized
from .phase1_data import window_config
from .mark_proxy import valued_funding, load_policy


def verify_record(root, config, item):
    spec = MonthlySpec(item['symbol'], 'fundingRate', item['period'])
    allowed_development(config, spec)
    start, end = map(utc_us, spec.interval())
    cursor, payload, sources = start // 1000, [], []
    for _ in range(10):
        url = (f'https://fapi.binance.com/fapi/v1/fundingRate?symbol={spec.symbol}'
               f'&startTime={cursor}&endTime={end // 1000 - 1}&limit=100')
        body = fetch(url, 2_000_000)
        page = json.loads(body)
        require(isinstance(page, list) and len(page) <= 100, 'Invalid REST page')
        path = root / 'data/raw/funding_api_pages' / spec.symbol / spec.period / (sha(body) + '.json')
        preserve(path, body)
        sources.append({'url': url, 'path': relative(root, path), 'sha256': sha(body), 'rows': len(page)})
        if not page:
            break
        times = [int(x['fundingTime']) for x in page]
        require(times == sorted(set(times)) and cursor <= times[0] <= times[-1] < end // 1000,
                'REST pagination outside bounds or repeated')
        payload.extend(page)
        if len(page) < 100:
            break
        cursor = times[-1] + 1
        if cursor >= end // 1000:
            break
    else:
        raise ValueError('Funding pagination exceeded explicit bound')
    parsed = [{'event_time_us': int(x['event_time_us']), 'rate': D(x['rate'])}
              for x in normalized(root, item)]
    comparison = crosscheck_funding(parsed, payload, spec.symbol, start, end)
    combined = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    path = root / 'data/raw/funding_api_paged' / spec.symbol / spec.period / (sha(combined) + '.json')
    preserve(path, combined)
    return {**item, 'funding_rest_verified': True, 'provisional_archive_only': False,
            'originally_unverified_archive_only': True,
            'audit_scope': 'official_checksum_archive_and_bounded_REST_pages_crosschecked',
            'funding_rest': comparison, 'funding_rest_path': relative(root, path),
            'funding_rest_sha256': sha(combined), 'funding_rest_parts': sources,
            'funding_rest_evidence_type': 'concatenation_of_preserved_official_REST_pages',
            'source_type': 'official_checksum_archive_with_paged_REST_crosscheck',
            'exact_settlement_marks_from_REST': comparison['missing_exact_settlement_marks'] == 0}


def recover(root):
    root = Path(root).resolve()
    policy = load_policy(root)
    plan, _, config = window_config(root, 'extension')
    old_pointer = json.loads((root / 'artifacts/data/mark_proxy_latest.json').read_text(encoding='utf-8'))
    old = {}
    for scenario, p in old_pointer['scenarios'].items():
        path = root / p['manifest']
        require(sha(path.read_bytes()) == p['sha256'], 'Previous proxy manifest changed')
        old[scenario] = json.loads(path.read_text(encoding='utf-8'))
    targets = [x for x in old['spot']['results'] if x.get('provisional_archive_only')]
    if not targets:
        require(all(x.get('funding_rest_verified_all_partitions') for x in old.values()),
                'Funding status incomplete without registered exceptions')
        print(json.dumps({'funding_rest_already_verified': True}), flush=True)
        return (root / old_pointer['scenarios']['spot']['manifest']).parent, old_pointer
    require(len(targets) == 9, 'Expected exactly the nine declared REST exceptions')
    run_id = 'MARK-PROXY-REST-RECOVERED-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/data/mark_proxy_runs' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('funding_rest_recovery.py', 'mark_proxy.py'):
        preserve(folder / 'source' / name, Path(__file__).with_name(name).read_bytes())
    recovered = {}
    with (folder / 'progress.jsonl').open('x', encoding='utf-8') as journal:
        with ThreadPoolExecutor(max_workers=2) as pool:
            for item in pool.map(lambda x: verify_record(root, config, x), targets):
                recovered[item['symbol'], item['period']] = item
                journal.write(json.dumps(item) + '\n'); journal.flush()
                print(json.dumps({'REST_recovered': [item['symbol'], item['period']],
                                  'verified': item['funding_rest']}), flush=True)
    entries = {}
    for scenario, manifest in old.items():
        records = [recovered.get((x['symbol'], x['period']), x) if x['kind'] == 'fundingRate' else x
                   for x in manifest['results']]
        require(all(x.get('funding_rest_verified', True) for x in records if x['kind'] == 'fundingRate'),
                'Unverified funding remains')
        body, count = valued_funding(root, records, config)
        path = root / 'data/validated/funding_valuation' / (sha(body) + '.csv.gz')
        preserve(path, body)
        previous_valuation = normalized(root, {'normalized_path': manifest['funding_valuation_path'],
                                              'normalized_sha256': manifest['funding_valuation_sha256']})
        new_rows = normalized(root, {'normalized_path': relative(root, path), 'normalized_sha256': sha(body)})
        old_idx = {(x['symbol'], x['event_time_us']): x for x in previous_valuation}
        new_idx = {(x['symbol'], x['event_time_us']): x for x in new_rows}
        require(set(old_idx) == set(new_idx) and all(D(old_idx[k]['rate']) == D(new_idx[k]['rate']) for k in old_idx),
                'Recovered funding rates or times differ')
        changed = [{'symbol': k[0], 'event_time_us': k[1], 'old_mark': old_idx[k]['settlement_mark'],
                    'new_mark': new_idx[k]['settlement_mark']} for k in old_idx
                   if D(old_idx[k]['settlement_mark']) != D(new_idx[k]['settlement_mark'])]
        new = {**manifest, 'run_id': run_id, 'results': records, 'errors': [],
               'funding_rest_verified_all_partitions': True, 'archive_only_funding_partitions': [],
               'archive_only_funding_accepted_as_accurate': False,
               'funding_rest_recovered_partitions': [s + '/' + p for s, p in recovered],
               'funding_recovery_method': 'ordinary_official_GET_with_limit_100_and_time_cursor',
               'parent_manifest': old_pointer['scenarios'][scenario],
               'funding_valuation_path': relative(root, path), 'funding_valuation_sha256': sha(body),
               'funding_events': count, 'funding_recovery_changed_settlement_marks': changed,
               'sensitivity_must_be_reviewed': False, 'complete_with_explicit_proxy': True}
        target = folder / (scenario + '_manifest.json')
        preserve(target, json.dumps(new, ensure_ascii=False, indent=2).encode())
        entries[scenario] = {'manifest': relative(root, target), 'sha256': sha(target.read_bytes()),
                             'complete_with_explicit_proxy': True}
        print(json.dumps({'scenario': scenario, 'settlement_marks_changed_after_REST': len(changed)}), flush=True)
    pointer = {'run_id': run_id, 'scenarios': entries, 'funding_rest_verified_all_partitions': True,
               'strict_mark_price_coverage': False, 'holdout_read': False, 'formal_selection_eligible': False}
    (root / 'artifacts/data/mark_proxy_latest.json').write_text(json.dumps(pointer, indent=2), encoding='utf-8')
    print(json.dumps(pointer), flush=True)
    return folder, pointer
