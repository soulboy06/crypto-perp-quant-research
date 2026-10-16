"""R011 regime filter run: Phase 1 candidate vs regime-gated candidate, paired accounts."""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import gzip
import itertools
import json
import time
import tomllib
from .archive import preserve
from .config import require, decimal_value as D
from .engine import MINUTE, stamp_us
from .ledger import InstrumentRules
from .margin import Tier
from .research4 import FourthHour
from .signal_quality import ExtensionAccount, load_signal_plan, read_csv
from .universe_data import SYMBOLS
from .full_data import sha, relative, specs_for
from .phase1 import CappedAccount
from .regime_account import RegimeAccount
from .uncertainty import paired_cagr_diagnostic


def regime_window_config(root, scope):
    plan = tomllib.loads((root / 'configs/eleventh_round.toml').read_text(encoding='utf-8'))
    universe = tomllib.loads((root / 'configs/universe_expansion.toml').read_text(encoding='utf-8'))
    config = tomllib.loads((root / 'configs/first_round.toml').read_text(encoding='utf-8'))
    window = plan[scope]
    config['data']['warmup_start'] = window['warmup_start']
    config['data']['development_start'] = window['start']
    config['data']['development_end'] = window['end_exclusive']
    return plan, universe, config


def corpus(root, scope, plan, config):
    # Reuse Phase 1's frozen-file discipline: SIG-001 reference must be intact.
    reference = root / 'artifacts/backtests' / plan.get(
        'baseline_reproduction_run', 'SIG001-20261016T045339Z-9b2fe0a6')
    if not reference.is_dir():
        # Fall back to the Phase 1 pointer when no explicit reference is set.
        pointer = json.loads((root / 'artifacts/backtests/phase1_reproduction_latest.json').read_text(encoding='utf-8'))
        reference = root / pointer['summary'].rsplit('/', 1)[0]
    old = json.loads((reference / 'summary.json').read_text(encoding='utf-8'))
    for name in ('first_round.toml', 'execution_policy.toml', 'data_policy.toml',
                 'universe_expansion.toml', 'signal_quality.toml', 'phase1.toml'):
        ref_path = reference / 'source' / name
        if ref_path.is_file():
            require((root / 'configs' / name).read_bytes() == ref_path.read_bytes(),
                    'Frozen settings changed: ' + name)
    for name in ('engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py',
                 'signals.py', 'universe_engine.py', 'signal_quality.py', 'research4.py',
                 'phase1.py'):
        if name in old.get('source_sha256', {}):
            require(sha(Path(__file__).with_name(name).read_bytes()) == old['source_sha256'][name],
                    'Frozen source changed: ' + name)
    return reference, old


def run(root, scope='extension'):
    root = Path(root).resolve()
    plan, universe, config = regime_window_config(root, scope)
    signal, _, _ = load_signal_plan(root)
    # Reuse Phase 1's prepared data manifest (same universe, same window).
    pointer_path = root / 'artifacts/data/phase1_latest.json'
    require(pointer_path.is_file(), 'Prepare Phase 1 extension data first')
    pointer = json.loads(pointer_path.read_text(encoding='utf-8'))
    manifest_path = root / pointer['manifest']
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    require(not manifest['holdout_history_requested'], 'Frozen holdout data forbidden')
    require(manifest['complete_extension_coverage'], 'Extension data incomplete')
    index = {(x['symbol'], x['kind'], x['period']): x for x in manifest['results']
             if x.get('audit_passed')}
    expected = {(x.symbol, x.kind, x.period) for x in specs_for(config)}
    require(set(index) == expected, 'Incomplete or out-of-scope data inventory')
    snapshot = json.loads((root / manifest['rules_path']).read_text(encoding='utf-8'))
    policy = tomllib.loads((root / 'configs/execution_policy.toml').read_text(encoding='utf-8'))
    warmup_start = stamp_us(config['data']['warmup_start'])
    start, end = [stamp_us(config['data'][key]) for key in ('development_start', 'development_end')]
    rules, ticks, fees = {}, {}, {}
    for symbol in SYMBOLS:
        x = snapshot['symbols'][symbol]
        filters = {v['filterType']: v for v in x['filters']}
        lot = filters['MARKET_LOT_SIZE']
        tier = Tier('0', policy['notional_coverage_assumption'], policy['maintenance_rate_assumption'], '0', '10')
        rules[symbol] = InstrumentRules(lot['stepSize'], lot['minQty'], lot['maxQty'],
                                       filters['MIN_NOTIONAL']['notional'], (tier,), start, end,
                                       'R011_CURRENT_RULES_PROXY_NOT_HISTORICAL')
        ticks[symbol], fees[symbol] = D(filters['PRICE_FILTER']['tickSize']), D(x['liquidationFee'])
    run_id = 'R011-' + scope.upper() + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    sources = {}
    for name in ('regime.py', 'regime_account.py', 'regime_run.py', 'phase1.py',
                 'signal_quality.py', 'universe_engine.py', 'engine.py', 'ledger.py',
                 'risk.py', 'margin.py', 'config.py', 'signals.py', 'research4.py'):
        body = Path(__file__).with_name(name).read_bytes()
        sources[name] = sha(body)
        preserve(folder / 'source' / name, body)
    for name in ('eleventh_round.toml', 'tenth_round_proposal.toml', 'phase1.toml',
                 'signal_quality.toml', 'first_round.toml', 'execution_policy.toml',
                 'data_policy.toml', 'universe_expansion.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    preserve(folder / 'source/test_regime.py', (root / 'tests/test_regime.py').read_bytes())
    metadata = {'run_id': run_id, 'research_design_id': plan['design_id'],
                'scope': plan[scope]['scope'], 'run_scope': scope, 'window': plan[scope],
                'plan': plan, 'data_manifest': relative(root, manifest_path),
                'data_manifest_sha256': sha(manifest_path.read_bytes()),
                'source_sha256': sources,
                'baseline': 'same_window_R010P1_rerun', 'new_candidate_cost_configurations': 2,
                'control_accounts': 2, 'holdout_read': False, 'formal_selection_eligible': False,
                'independent_validation': False, 'parameter_search': False}
    preserve(folder / 'started.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode())
    accounts = []
    clock = time.monotonic()
    try:
        for name, cls in [('control_cap3', CappedAccount), ('candidate_regime', RegimeAccount)]:
            for cost in ('base', 'stress'):
                accounts.append(cls(config, rules, ticks, fees, cost, folder,
                                    plan=universe, signal_plan=signal, name=name))
        with gzip.open(root / manifest['funding_valuation_path'], 'rt', encoding='utf-8') as f:
            funding = list(csv.DictReader(f))
        require(all(start <= int(x['event_time_us']) < end for x in funding), 'Funding outside window')
        fund_index, previous = 0, None
        hours = {s: FourthHour() for s in SYMBOLS}
        print(json.dumps({'run_id': run_id, 'scope': metadata['scope'],
                          'accounts': [a.name for a in accounts]}), flush=True)
        for period in sorted({x[2] for x in index}):
            with ExitStack() as stack:
                keys = [(s, k) for s in SYMBOLS for k in ('klines', 'markPriceKlines')]
                readers = [csv.DictReader(stack.enter_context(gzip.open(
                    root / index[s, k, period]['normalized_path'], 'rt', encoding='utf-8')))
                           for s, k in keys]
                for bundle in itertools.zip_longest(*readers):
                    require(all(x is not None for x in bundle), 'Unaligned candle lengths')
                    t = int(bundle[0]['open_time_us'])
                    require(all(int(x['open_time_us']) == t for x in bundle)
                            and (previous is None or t == previous + MINUTE), 'Candle timing gap')
                    require(warmup_start <= t < end, 'Holdout access forbidden')
                    previous = t
                    data = {s: {} for s in SYMBOLS}
                    for (s, k), row in zip(keys, bundle):
                        data[s]['trade' if k == 'klines' else 'mark'] = row
                    if t >= start:
                        funds = []
                        while fund_index < len(funding) and int(funding[fund_index]['event_time_us']) < t + MINUTE:
                            require(int(funding[fund_index]['event_time_us']) >= t, 'Skipped funding')
                            funds.append(funding[fund_index])
                            fund_index += 1
                        for a in accounts:
                            a.minute(t, data, funds, t == end - MINUTE)
                    else:
                        # Warmup: feed regime tracker only, no trading state.
                        for a in accounts:
                            if isinstance(a, RegimeAccount):
                                a.warmup_minute(t, data)
                    features = {}
                    for s in SYMBOLS:
                        closed, features[s] = hours[s].push(data[s]['trade'])
                    if closed:
                        for a in accounts:
                            a.decisions(t + MINUTE, features)
            print(json.dumps({'through': period, 'equity': {a.name: str(a.ledger.equity) for a in accounts},
                              'trades': {a.name: len(a.trades) for a in accounts}}), flush=True)
        require(fund_index == len(funding), 'Funding not consumed')
        metrics = [a.finish(folder) for a in accounts]
        # Regime audit: ADX readiness and gate counts must be sane.
        regime_audit = {}
        for a in accounts:
            if isinstance(a, RegimeAccount):
                require(a.regime.adx is not None, 'Regime ADX never became ready')
                regime_audit[a.name] = {'final_adx': a.regime.adx,
                                       'regime_allows': a.regime_allows,
                                       'regime_blocks': a.regime_blocks}
        summary = {**metadata, 'status': 'completed', 'accounts': metrics,
                   'regime_audit': regime_audit, 'runtime_seconds': time.monotonic() - clock}
        preserve(folder / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2).encode())
        print(json.dumps({'completed': str(folder), 'regime_audit': regime_audit}), flush=True)
        return folder, summary
    except Exception as exc:
        for a in accounts:
            for field in ('event_stream', 'gate_log', 'sizing_log'):
                stream = getattr(a, field, None)
                if stream is not None and not stream.closed:
                    stream.close()
        preserve(folder / 'failed.json', json.dumps({**metadata, 'error': str(exc)}, indent=2).encode())
        raise
