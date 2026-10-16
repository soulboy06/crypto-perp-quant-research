"""User-authorized, narrowly whitelisted minute mark proxies, never exact marks."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import io
import json
import tomllib
from statistics import median
from .archive import preserve, verify_checksum
from .config import require, decimal_value as D
from .engine import MINUTE, stamp_us
from .full_data import MonthlySpec, specs_for, fetch, unpack, sha, relative
from .phase1_data import window_config, cached_records, prepare_partition, value_funding
from .quality import rows, candles, KLINE_HEADER, utc_us, timestamp


def load_policy(root):
    p = tomllib.loads((root / 'configs/mark_proxy.toml').read_text(encoding='utf-8'))
    require(p['policy_id'] == 'R010-MARK-PROXY-v1' and p['scenarios'] == ['spot', 'ffill']
            and p['user_authorized_proxy_exception'] and p['holdout_locked']
            and not p['parameter_search'] and not p['select_proxy_by_returns']
            and not p['formal_selection_eligible'] and not p['phase_2_enabled'], 'Proxy scope changed')
    require(p['only_period'] == '2024-08' and p['gap_open_times_utc'] ==
            ['2024-08-12T10:02:00Z', '2024-08-12T10:03:00Z']
            and p['affected_symbols'] == ['SOLUSDT', 'XRPUSDT', 'DOGEUSDT', 'ADAUSDT',
                                          'LINKUSDT', 'AVAXUSDT', 'LTCUSDT'], 'Proxy whitelist changed')
    require(p['strategy_accounts_per_scenario'] == 4 and p['maximum_strategy_accounts'] == 8
            and p['maximum_proxy_bars_per_symbol'] == 2
            and p['maximum_ffill_age_at_open_seconds'] == 60
            and p['maximum_ffill_age_at_end_seconds'] == 120, 'Proxy bounds changed')
    return p


def pack_raw(raw):
    stream = io.StringIO(newline='')
    w = csv.writer(stream, lineterminator='\n')
    w.writerow(KLINE_HEADER)
    w.writerows(raw)
    return stream.getvalue().encode()


def fill_missing(raw_mark, raw_spot, start, end, expected_missing, scenario):
    """Only the declared absent timestamps can change; existing mark rows stay byte values."""
    require(scenario in ('spot', 'ffill'), 'Unknown proxy scenario')
    original = {}
    for row in raw_mark:
        t, _ = timestamp(row[0], start, end)
        require(t % MINUTE == 0 and t not in original, 'Invalid or duplicate mark timestamp')
        original[t] = row
    missing = sorted(set(range(start, end, MINUTE)) - set(original))
    require(missing == sorted(expected_missing), 'Unapproved mark gap; no proxy permitted')
    require(raw_spot is not None, 'Observed spot evidence required for both sensitivities')
    # Strict timing/OHLC audit, no spot volume features used or claimed verified.
    candles(pack_raw(raw_spot), start, end, mark=True)
    spot = {timestamp(row[0], start, end)[0]: row for row in raw_spot}
    result, evidence = dict(original), []
    for t in missing:
        if scenario == 'spot':
            source = spot[t]
            result[t] = source[:]
            evidence.append({'open_time_us': t, 'proxy_source': 'spot',
                             'OHLC': source[1:5], 'open_available_us': t,
                             'extrema_available_us': t + MINUTE,
                             'exact_mark': False, 'observed_market': 'spot_not_futures_mark'})
        else:
            # Search only original observations, never a previous constructed proxy.
            anchor = max(x for x in original if x + MINUTE <= t)
            source = original[anchor]
            close = source[4]
            proxy = source[:]
            # Existing 2024 archives use milliseconds; reject unexpected unit changes.
            require(timestamp(source[0], start, end)[1] == 'ms', 'Unexpected ffill timestamp unit')
            proxy[0], proxy[6] = str(t // 1000), str((t + MINUTE) // 1000 - 1)
            proxy[1:5] = [close] * 4
            age = t - (anchor + MINUTE)
            require(0 <= age <= MINUTE and t + MINUTE - (anchor + MINUTE) <= 2 * MINUTE,
                    'Forward-fill anchor stale beyond authorized gap')
            result[t] = proxy
            evidence.append({'open_time_us': t, 'proxy_source': 'last_closed_mark',
                             'source_mark_open_us': anchor, 'source_available_us': anchor + MINUTE,
                             'age_at_open_us': age, 'age_at_end_us': age + MINUTE,
                             'OHLC': [close] * 4, 'exact_mark': False,
                             'flat_constructed_bar_from_past_observation': True})
    parsed, unit = candles(pack_raw([result[t] for t in sorted(result)]), start, end, mark=True)
    require(unit == 'ms' and all(result[t] == original[t] for t in original),
            'Observed mark rows changed')
    return parsed, evidence


def deviation(raw_mark, raw_spot, start, end, missing, neighbor_minutes=30):
    mark = {timestamp(x[0], start, end)[0]: D(x[4]) for x in raw_mark}
    spot = {timestamp(x[0], start, end)[0]: D(x[4]) for x in raw_spot}
    lo, hi = min(missing) - neighbor_minutes * MINUTE, max(missing) + (neighbor_minutes + 1) * MINUTE
    values = sorted(abs(spot[t] / mark[t] - 1) for t in mark if lo <= t < hi and t in spot)
    require(len(values) >= 2 * neighbor_minutes, 'Insufficient neighboring overlap')
    return {'metric': 'abs(spot_close/observed_mark_close-1)', 'overlap_minutes': len(values),
            'from_us': lo, 'to_exclusive_us': hi, 'median': str(median(values)),
            'p95': str(values[int((len(values) - 1) * .95)]), 'maximum': str(values[-1]),
            'true_gap_error_measured': False, 'not_extreme_price_or_liquidation_error_bound': True}


def observed_sources(root, symbol, policy):
    period = policy['only_period']
    spec = MonthlySpec(symbol, 'markPriceKlines', period)
    name = spec.url().rsplit('/', 1)[1]
    # Use verified immutable local monthly archive when available; otherwise retrieve it.
    candidates = list((root / 'data/raw/markPriceKlines' / symbol / period).glob('*/' + name))
    if candidates:
        require(len(candidates) == 1, 'Ambiguous mark source')
        mark_path = candidates[0]
        checks = list(mark_path.parent.glob('*.CHECKSUM'))
        require(len(checks) == 1, 'Ambiguous mark checksum')
        mark_body, mark_check = mark_path.read_bytes(), checks[0].read_bytes()
        verify_checksum(mark_body, mark_check, name)
    else:
        mark_check = fetch(spec.url() + '.CHECKSUM', 4096)
        mark_body = fetch(spec.url(), 20_000_000)
        verify_checksum(mark_body, mark_check, name)
        mark_path = root / 'data/raw/markPriceKlines' / symbol / period / sha(mark_body) / name
        preserve(mark_path, mark_body)
        preserve(mark_path.parent / (sha(mark_check) + '.CHECKSUM'), mark_check)
    spot_url = policy['spot_source'] + '/' + symbol + '/1m/' + symbol + '-1m-' + period + '.zip'
    spot_name = spot_url.rsplit('/', 1)[1]
    spot_check = fetch(spot_url + '.CHECKSUM', 4096)
    spot_digest = spot_check.decode('ascii').split()[0].lower()
    require(len(spot_digest) == 64 and all(c in '0123456789abcdef' for c in spot_digest),
            'Invalid spot checksum digest')
    spot_path = root / 'data/raw/spot_klines' / symbol / period / spot_digest / spot_name
    spot_body = spot_path.read_bytes() if spot_path.is_file() else fetch(spot_url, 20_000_000)
    verify_checksum(spot_body, spot_check, spot_name)
    preserve(spot_path, spot_body)
    preserve(spot_path.parent / (sha(spot_check) + '.CHECKSUM'), spot_check)
    raw_mark = rows(unpack(mark_body, name[:-4] + '.csv'), KLINE_HEADER)
    raw_spot = rows(unpack(spot_body, spot_name[:-4] + '.csv'), KLINE_HEADER)
    return raw_mark, raw_spot, {'original_mark_path': relative(root, mark_path),
                               'original_mark_sha256': sha(mark_body), 'original_mark_url': spec.url(),
                               'spot_path': relative(root, spot_path), 'spot_sha256': sha(spot_body),
                               'spot_url': spot_url, 'spot_checksum_sha256': sha(spot_check),
                               'spot_audit_scope': 'complete_minute_timing_and_OHLC_only'}


def patched_partitions(root, symbol, policy):
    from .reconcile import compressed_rows
    raw_mark, raw_spot, source = observed_sources(root, symbol, policy)
    start, end = map(utc_us, MonthlySpec(symbol, 'markPriceKlines', policy['only_period']).interval())
    missing = list(map(stamp_us, policy['gap_open_times_utc']))
    basis = deviation(raw_mark, raw_spot, start, end, missing,
                      policy['neighbor_deviation_minutes_each_side'])
    result = {}
    for scenario in policy['scenarios']:
        parsed, evidence = fill_missing(raw_mark, raw_spot, start, end, missing, scenario)
        body = compressed_rows(parsed)
        path = root / 'data/validated/mark_proxy' / scenario / symbol / policy['only_period'] / (sha(body) + '.csv.gz')
        preserve(path, body)
        result[scenario] = {'symbol': symbol, 'kind': 'markPriceKlines', 'period': policy['only_period'],
                            'audit_passed': True, 'audit_scope': 'schema_timing_with_user_authorized_mark_proxy',
                            'strict_original_mark_audit_passed': False, 'has_proxy': True,
                            'proxy_source': 'spot' if scenario == 'spot' else 'last_closed_mark',
                            'proxy_scenario': scenario, 'proxy_bars': evidence,
                            'proxy_bar_count': len(evidence), 'observed_spot_mark_deviation': basis,
                            'exact_mark_recovered': False, 'rows': len(parsed), 'source_evidence': source,
                            'first_time_us': parsed[0]['open_time_us'], 'last_time_us': parsed[-1]['open_time_us'],
                            'normalized_path': relative(root, path), 'normalized_sha256': sha(body)}
    return result


def prepare(root):
    root = Path(root).resolve()
    policy = load_policy(root)
    plan, _, config = window_config(root, 'extension')
    specs = specs_for(config)
    require(len(specs) == 600, 'Extension inventory changed')
    run_id = 'MARK-PROXY-DATA-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/data/mark_proxy_runs' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    for name in ('mark_proxy.py', 'phase1_data.py', 'quality.py', 'universe_data.py'):
        preserve(folder / 'source' / name, Path(__file__).with_name(name).read_bytes())
    for name in ('mark_proxy.toml', 'phase1.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    cache = cached_records(root)
    # Only strictly observed successful entries may enter this common cache.
    for path in (root / 'artifacts/data/mark_proxy_runs').glob('*/progress.jsonl'):
        for line in path.read_text(encoding='utf-8').splitlines():
            x = json.loads(line)
            if x.get('audit_passed') and not x.get('has_proxy'):
                cache[x['symbol'], x['kind'], x['period']] = x
    keys = {(s, 'markPriceKlines', policy['only_period']) for s in policy['affected_symbols']}
    records, missing = [], []
    for spec in specs:
        key = spec.symbol, spec.kind, spec.period
        if key in keys:
            continue
        x = cache.get(key)
        if x and spec.kind == 'fundingRate' and not (root / x['funding_rest_path']).is_file():
            x = None
        if x:
            require(not x.get('has_proxy') and sha((root / x['normalized_path']).read_bytes()) == x['normalized_sha256'],
                    'Strict cache changed or contains a proxy')
            records.append(x)
        else:
            missing.append(spec)
    print(json.dumps({'run_id': run_id, 'strict_cached': len(records), 'missing_strict': len(missing),
                      'proxy_partitions': len(keys), 'proxy_minutes_total': len(keys) * 2}), flush=True)
    errors, patches = [], {}
    with (folder / 'progress.jsonl').open('x', encoding='utf-8') as journal:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(prepare_partition, root, config, spec): spec for spec in missing}
            for f in as_completed(futures):
                x = f.result()
                journal.write(json.dumps(x, ensure_ascii=False) + '\n'); journal.flush()
                if x['audit_passed']:
                    records.append(x)
                else:
                    errors.append(x)
                if len(records) % 12 == 0 or not x['audit_passed']:
                    print(json.dumps({'strict_accepted': len(records), 'failed': len(errors),
                                      'latest': [x['symbol'], x['kind'], x['period']], 'error': x.get('error')}), flush=True)
        for symbol in policy['affected_symbols']:
            try:
                patches[symbol] = patched_partitions(root, symbol, policy)
                print(json.dumps({'proxy_prepared': symbol,
                                  'overlap_deviation': patches[symbol]['spot']['observed_spot_mark_deviation']}), flush=True)
            except Exception as exc:
                errors.append({'symbol': symbol, 'stage': 'mark_proxy', 'error': f'{type(exc).__name__}: {exc}'})
    strict_pointer = json.loads((root / 'artifacts/data/universe_latest.json').read_text(encoding='utf-8'))
    strict_path = root / strict_pointer['manifest']
    require(sha(strict_path.read_bytes()) == strict_pointer['sha256'], 'Original strict manifest changed')
    old = json.loads(strict_path.read_text(encoding='utf-8'))
    entries = {}
    for scenario in policy['scenarios']:
        all_records = records + [x[scenario] for x in patches.values()]
        complete = len(all_records) == 600 and not errors
        manifest = {'run_id': run_id, 'scenario': scenario, 'design_id': plan['design_id'],
                    'scope': 'extension_validation_with_explicit_mark_proxy_not_independent_holdout',
                    'window': plan['extension'], 'plan_sha256': sha((root / 'configs/phase1.toml').read_bytes()),
                    'proxy_policy': policy, 'proxy_policy_sha256': sha((root / 'configs/mark_proxy.toml').read_bytes()),
                    'complete_extension_coverage': False, 'strict_mark_price_coverage': False,
                    'complete_with_explicit_proxy': complete, 'exact_mark_recovered': False,
                    'holdout_history_requested': False, 'formal_selection_eligible': False,
                    'proxy_source': 'spot' if scenario == 'spot' else 'last_closed_mark',
                    'proxy_bar_count': sum(x.get('proxy_bar_count', 0) for x in all_records),
                    'results': all_records, 'errors': list(errors),
                    'rules_path': old['rules_path'], 'rules_sha256': old['rules_sha256']}
        if complete:
            try:
                body, count = value_funding(root, all_records, config)
                path = root / 'data/validated/funding_valuation' / (sha(body) + '.csv.gz')
                preserve(path, body)
                manifest.update(funding_valuation_path=relative(root, path),
                                funding_valuation_sha256=sha(body), funding_events=count)
            except (OSError, ValueError) as exc:
                manifest['complete_with_explicit_proxy'] = False
                manifest['errors'].append({'stage': 'funding_valuation', 'error': f'{type(exc).__name__}: {exc}'})
        path = folder / (scenario + '_manifest.json')
        preserve(path, json.dumps(manifest, ensure_ascii=False, indent=2).encode())
        entries[scenario] = {'manifest': relative(root, path), 'sha256': sha(path.read_bytes()),
                             'complete_with_explicit_proxy': manifest['complete_with_explicit_proxy']}
    pointer = {'run_id': run_id, 'scenarios': entries, 'holdout_read': False,
               'formal_selection_eligible': False, 'strict_mark_price_coverage': False}
    (root / 'artifacts/data/mark_proxy_latest.json').write_text(json.dumps(pointer, indent=2), encoding='utf-8')
    print(json.dumps(pointer), flush=True)
    return folder, pointer
