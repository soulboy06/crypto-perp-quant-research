"""R011 regime filter run: Phase 1 candidate vs regime-gated candidate, paired accounts."""
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import gzip
import itertools
import json
import math
import time
import tomllib
from .archive import preserve
from .config import require, decimal_value as D
from .engine import MINUTE, stamp_us
from .ledger import InstrumentRules
from .margin import Tier
from .research4 import FourthHour
from .signal_quality import load_signal_plan, read_csv
from .universe_data import SYMBOLS
from .full_data import sha, relative
from .phase1 import CappedAccount
from .phase1_data import window_config
from .phase1_run import corpus as phase1_corpus
from .regime_account import RegimeAccount
from .uncertainty import paired_cagr_diagnostic


def regime_window_config(root, scope):
    plan = tomllib.loads((root / 'configs/eleventh_round.toml').read_text(encoding='utf-8'))
    phase_plan, universe, config = window_config(root, scope)
    require(plan['design_id'] == 'R011-v1-percoin' and not plan['parameter_search']
            and not plan['formal_selection_eligible'] and plan['holdout_locked']
            and not plan['phase_2_enabled'] and plan['maximum_accounts_per_run'] == 4
            and plan['cost_scenarios'] == ['base', 'stress'], 'R011 research scope changed')
    require(plan['regime']['indicator'] == 'wilder_adx_14'
            and plan['regime']['scope'] == 'per_coin_each_symbol_own_adx'
            and D(plan['regime']['threshold']) == 25, 'R011 regime design changed')
    for key in ('warmup_start', 'start', 'end_exclusive'):
        require(plan[scope][key] == phase_plan[scope][key], 'R011 window changed: ' + key)
    return plan, universe, config


def corpus(root, scope, config, proxy_scenario):
    # PR #2 owns the observed-data/proxy policy and all hash/whitelist checks.
    # Reproduction uses the original SIG-001 corpus, never an extension pointer.
    phase_plan, _, _ = window_config(root, scope)
    result = phase1_corpus(root, scope, phase_plan, config,
                           proxy_scenario=proxy_scenario if scope == 'extension' else None)
    if scope == 'extension':
        require(result[1].get('funding_rest_verified_all_partitions') is True,
                'R011 requires the REST-reconciled funding corpus from PR #2')
    return result


def evaluate_gates(metrics, cfg, folder, scope='extension'):
    """Compute acceptance gates from config [gates].

    Compares each candidate (regime) account against its control (cap3) pair
    within the same cost scenario. Returns per-pair pass/fail dict.
    'completed' in summary means the run finished; 'gates' says if it passed.
    """
    by_name = {m['account']: m for m in metrics}
    folder = Path(folder)
    required = {'round_trips', 'net_return', 'cagr', 'max_adverse_bound_drawdown',
                'calmar', 'halt_reasons', 'liquidation_proxy_events', 'execution_ambiguities'}
    results = {}
    for cost in ('base', 'stress'):
        ctrl = by_name.get(f'control_cap3_{cost}')
        cand = by_name.get(f'candidate_regime_{cost}')
        if not ctrl or not cand:
            results[cost] = {'evaluated': False, 'reason': 'missing_account_pair'}
            continue
        if any(not required <= m.keys() for m in (ctrl, cand)):
            results[cost] = {'evaluated': False, 'reason': 'missing_engine_metrics'}
            continue
        def finite(value):
            return value is not None and math.isfinite(float(value))
        ci = None
        try:
            bd, nd = [read_csv(folder / (m['account'] + '_daily_equity.csv')) for m in (ctrl, cand)]
            require([x['date'] for x in bd] == [x['date'] for x in nd], 'Unpaired comparison dates')
            ci = paired_cagr_diagnostic([float(x['equity']) for x in bd],
                                       [float(x['equity']) for x in nd],
                                       block_days=cfg['bootstrap_block_days'],
                                       resamples=cfg['bootstrap_resamples'], seed=cfg['bootstrap_seed'])
        except (OSError, ValueError, KeyError) as exc:
            results[cost] = {'evaluated': False, 'reason': 'bootstrap_unavailable', 'error': str(exc)}
            continue
        checks = {
            'min_trades': cand['round_trips'] >= cfg['minimum_trades'],
            'positive_net': finite(cand['net_return']) and cand['net_return'] > 0
                            if cfg['require_positive_net'] else True,
            'cagr_improvement': all(finite(m['cagr']) for m in (ctrl, cand)) and
                                cand['cagr'] - ctrl['cagr'] >= float(cfg['minimum_cagr_improvement']),
            'drawdown_limit': finite(cand['max_adverse_bound_drawdown']) and
                              cand['max_adverse_bound_drawdown'] <= float(cfg['maximum_drawdown']),
            'calmar_non_decreasing': all(finite(m['calmar']) for m in (ctrl, cand)) and
                                    cand['calmar'] >= ctrl['calmar']
                                    if cfg['require_non_decreasing_calmar'] else True,
            'no_halts': all(not m['halt_reasons'] and m['liquidation_proxy_events'] == 0
                            and m['execution_ambiguities'] == 0 for m in (ctrl, cand))
                            if cfg['require_no_halts_liquidations_ambiguities'] else True,
            'bootstrap_lower_positive': finite(ci['cagr_improvement_lower_95']) and
                                        ci['cagr_improvement_lower_95'] > 0
                                        if cfg['require_positive_bootstrap_lower'] else True}
        results[cost] = {'evaluated': True, 'checks': checks, 'bootstrap': ci,
                         'passed': all(checks.values())}
    results['numerical_checks_passed'] = all(results[cost].get('passed', False) for cost in ('base', 'stress'))
    results['overall_passed'] = scope == 'extension' and results['numerical_checks_passed']
    results['formal_selection_eligible'] = False
    return results


def run(root, scope='extension', *, proxy_scenario='spot'):
    root = Path(root).resolve()
    plan, universe, config = regime_window_config(root, scope)
    signal, _, _ = load_signal_plan(root)
    require(proxy_scenario in ('spot', 'ffill'), 'Unknown R011 proxy scenario')
    proxy_scenario = proxy_scenario if scope == 'extension' else None
    manifest_path, manifest, index, reference = corpus(root, scope, config, proxy_scenario)
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
    scope_tag = scope.upper() + ('-' + proxy_scenario.upper() if proxy_scenario else '')
    run_id = 'R011-' + scope_tag + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    sources = {}
    for name in ('regime.py', 'regime_account.py', 'regime_run.py', 'phase1.py',
                 'signal_quality.py', 'universe_engine.py', 'engine.py', 'ledger.py',
                 'risk.py', 'margin.py', 'config.py', 'signals.py', 'research4.py',
                 'phase1_run.py', 'phase1_data.py', 'universe_data.py', 'uncertainty.py',
                 'mark_proxy.py'):
        body = Path(__file__).with_name(name).read_bytes()
        sources[name] = sha(body)
        preserve(folder / 'source' / name, body)
    for name in ('eleventh_round.toml', 'tenth_round_proposal.toml', 'phase1.toml',
                 'signal_quality.toml', 'first_round.toml', 'execution_policy.toml',
                 'data_policy.toml', 'universe_expansion.toml', 'mark_proxy.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    preserve(folder / 'source/test_regime.py', (root / 'tests/test_regime.py').read_bytes())
    preserve(folder / 'source/test_regime_integration.py', (root / 'tests/test_regime_integration.py').read_bytes())
    metadata = {'run_id': run_id, 'research_design_id': plan['design_id'],
                'scope': plan[scope]['scope'], 'run_scope': scope, 'window': plan[scope],
                'plan': plan, 'data_manifest': relative(root, manifest_path),
                'data_manifest_sha256': sha(manifest_path.read_bytes()),
                'reference_run': relative(root, reference), 'proxy_scenario': proxy_scenario,
                'funding_multiplier': 1,
                'funding_rest_verified_all_partitions': manifest.get('funding_rest_verified_all_partitions', True),
                'strict_mark_price_coverage': proxy_scenario is None,
                'proxy_bar_count': manifest.get('proxy_bar_count', 0),
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
                kwargs = {'regime_threshold': float(plan['regime']['threshold'])} if cls is RegimeAccount else {}
                accounts.append(cls(config, rules, ticks, fees, cost, folder,
                                    plan=universe, signal_plan=signal, name=name, **kwargs))
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
        # Regime audit: every coin's ADX must be ready; gate counts sane.
        regime_audit = {}
        for a in accounts:
            if isinstance(a, RegimeAccount):
                not_ready = [s for s, r in a.regimes.items() if r.adx is None]
                require(not not_ready, 'Regime ADX never ready: ' + ','.join(not_ready))
                regime_audit[a.name] = {
                    'final_adx': {s: round(r.adx, 4) for s, r in a.regimes.items()},
                    'regime_allows': a.regime_allows, 'regime_blocks': a.regime_blocks}
        # Acceptance gates (config [gates]): compute pass/fail per account pair.
        gates_cfg = plan.get('gates', {})
        gates = evaluate_gates(metrics, gates_cfg, folder, scope)
        summary = {**metadata, 'status': 'completed', 'accounts': metrics,
                   'regime_audit': regime_audit, 'gates': gates,
                   'runtime_seconds': time.monotonic() - clock}
        preserve(folder / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2).encode())
        print(json.dumps({'completed': str(folder), 'regime_audit': regime_audit,
                          'gates': gates}), flush=True)
        return folder, summary
    except Exception as exc:
        for a in accounts:
            for field in ('event_stream', 'gate_log', 'sizing_log'):
                stream = getattr(a, field, None)
                if stream is not None and not stream.closed:
                    stream.close()
        preserve(folder / 'failed.json', json.dumps({**metadata, 'error': str(exc)}, indent=2).encode())
        raise
