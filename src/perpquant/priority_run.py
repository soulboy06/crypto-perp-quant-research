"""R012 preregistered single-family experiment; all seen-history results are diagnostic."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv
import gzip
import itertools
import json
import statistics
import subprocess
import time
import tomllib
from .archive import preserve
from .config import decimal_value as D, require
from .engine import MINUTE, stamp_us
from .full_data import sha, relative, specs_for
from .ledger import InstrumentRules
from .margin import Tier
from .phase1 import CappedAccount
from .phase1_data import window_config
from .phase1_run import corpus
from .priority import PriorityAccount, PriorityHour
from .signal_quality import load_signal_plan, read_csv
from .uncertainty import paired_cagr_diagnostic
from .universe_data import SYMBOLS

PREREGISTRATION = '286d6e0'


def load_plan(root):
    path = root / 'configs/twelfth_round.toml'
    body = path.read_bytes()
    registered = subprocess.check_output(['git', 'show', PREREGISTRATION + ':configs/twelfth_round.toml'], cwd=root)
    require(body == registered, 'R012 preregistered plan changed')
    plan = tomllib.loads(body.decode('utf-8'))
    require(plan['design_id'] == 'R012-v1' and not plan['parameter_search']
            and plan['maximum_candidate_families'] == 1 and plan['priority']['lookback_hours'] == 168
            and not plan['formal_selection_eligible'] and plan['holdout_locked'], 'R012 scope changed')
    return plan


def prepare_cases(root, plan):
    """Deduplicate on audited input content, before any candidate returns exist."""
    corpora = {}
    for scope, scenario in [('reproduction', None), ('extension', 'spot'), ('extension', 'ffill')]:
        phase_plan, universe, config = window_config(root, scope)
        manifest_path, manifest, index, reference = corpus(root, scope, phase_plan, config, proxy_scenario=scenario)
        require(scope != 'extension' or manifest.get('funding_rest_verified_all_partitions') is True,
                'R012 requires REST-reconciled funding')
        with gzip.open(root / manifest['funding_valuation_path'], 'rt', encoding='utf-8') as stream:
            funds = list(csv.DictReader(stream))
        corpora[scope, scenario] = (universe, config, manifest_path, manifest, index, funds)
    cases, aliases = [], []
    for window in [plan['continuous'], *plan['rolling_windows']]:
        scenarios = [None] if window['corpus_scope'] == 'reproduction' else plan['proxy_scenarios']
        seen = {}
        for scenario in scenarios:
            universe, config, manifest_path, manifest, source, funds = corpora[window['corpus_scope'], scenario]
            config = deepcopy(config)
            config['data'].update(warmup_start=window['warmup_start'], development_start=window['start'],
                                  development_end=window['end_exclusive'])
            require(window['end_exclusive'] <= '2026-01-01T00:00:00Z', 'Frozen 2026 access forbidden')
            expected = {(s.symbol, s.kind, s.period) for s in specs_for(config)}
            require(expected <= source.keys(), 'Rolling corpus incomplete')
            index = {key: source[key] for key in sorted(expected)}
            start, end = stamp_us(window['start']), stamp_us(window['end_exclusive'])
            selected_funds = [x for x in funds if start <= int(x['event_time_us']) < end]
            fingerprint = sha(json.dumps({'config': config, 'rules': manifest['rules_sha256'],
                                         'partitions': [(list(key), x['normalized_sha256']) for key, x in index.items()],
                                         'funding': selected_funds}, sort_keys=True).encode())
            label = window['id'] + '-' + (scenario or 'actual')
            if fingerprint in seen:
                aliases.append({'case': label, 'canonical': seen[fingerprint], 'input_sha256': fingerprint})
                continue
            seen[fingerprint] = label
            cases.append({'label': label, 'window': window, 'proxy_scenario': scenario,
                          'universe': universe, 'config': config, 'manifest': manifest,
                          'manifest_path': relative(root, manifest_path), 'index': index,
                          'funding': selected_funds, 'input_sha256': fingerprint})
    require(len(cases) * 4 <= plan['maximum_account_executions'], 'R012 execution budget exceeded')
    return cases, aliases


def compare_primary(folder, metrics, gates):
    by_name = {m['account']: m for m in metrics}
    results = {}
    for cost in ('base', 'stress'):
        control, candidate = [by_name[prefix + '_' + cost] for prefix in ('control_cap3', 'candidate_priority')]
        before, after = [read_csv(folder / (m['account'] + '_daily_equity.csv')) for m in (control, candidate)]
        require([x['date'] for x in before] == [x['date'] for x in after], 'Unpaired R012 dates')
        ci = paired_cagr_diagnostic([float(x['equity']) for x in before], [float(x['equity']) for x in after],
                                   block_days=gates['bootstrap_block_days'], resamples=gates['bootstrap_resamples'],
                                   seed=gates['bootstrap_seed'])
        checks = {'positive_net': candidate['net_return'] > 0,
                  'minimum_trades': candidate['round_trips'] >= gates['minimum_trades'],
                  'cagr_improvement': candidate['cagr'] is not None and control['cagr'] is not None and
                                      candidate['cagr'] - control['cagr'] >= float(gates['minimum_cagr_improvement']),
                  'drawdown_limit': candidate['max_adverse_bound_drawdown'] <= float(gates['maximum_drawdown']),
                  'no_halts_liquidations_ambiguities': not candidate['halt_reasons'] and
                      candidate['liquidation_proxy_events'] == 0 and candidate['execution_ambiguities'] == 0,
                  'calmar_non_decreasing': candidate['calmar'] is not None and control['calmar'] is not None and
                                          candidate['calmar'] >= control['calmar'],
                  'bootstrap_lower_positive': ci['cagr_improvement_lower_95'] > 0}
        results[cost] = {'checks': checks, 'bootstrap': ci, 'passed': all(checks.values())}
    return results


def run_case(root, suite_folder, case):
    root, suite_folder = Path(root), Path(suite_folder)
    plan = load_plan(root)
    config, universe = case['config'], case['universe']
    manifest, index = case['manifest'], case['index']
    signal, _, _ = load_signal_plan(root)
    snapshot = json.loads((root / manifest['rules_path']).read_text(encoding='utf-8'))
    policy = tomllib.loads((root / 'configs/execution_policy.toml').read_text(encoding='utf-8'))
    start, end = stamp_us(config['data']['development_start']), stamp_us(config['data']['development_end'])
    warmup = stamp_us(config['data']['warmup_start'])
    rules, ticks, fees = {}, {}, {}
    for symbol in SYMBOLS:
        row = snapshot['symbols'][symbol]
        filters = {x['filterType']: x for x in row['filters']}
        lot = filters['MARKET_LOT_SIZE']
        tier = Tier('0', policy['notional_coverage_assumption'], policy['maintenance_rate_assumption'], '0', '10')
        rules[symbol] = InstrumentRules(lot['stepSize'], lot['minQty'], lot['maxQty'],
                                       filters['MIN_NOTIONAL']['notional'], (tier,), start, end,
                                       'R012_CURRENT_RULES_PROXY_NOT_HISTORICAL')
        ticks[symbol], fees[symbol] = D(filters['PRICE_FILTER']['tickSize']), D(row['liquidationFee'])
    folder = suite_folder / case['label']
    folder.mkdir()
    metadata = {'design_id': plan['design_id'], 'case': case['label'], 'window': case['window'],
                'proxy_scenario': case['proxy_scenario'], 'input_sha256': case['input_sha256'],
                'data_manifest': case['manifest_path'],
                'data_manifest_sha256': sha((root / case['manifest_path']).read_bytes()),
                'funding_multiplier': 1, 'historical_order_filters_verified': False,
                'historical_margin_rules_verified': False,
                'strict_mark_price_coverage': case['proxy_scenario'] is None,
                'formal_selection_eligible': False, 'independent_validation': False, 'holdout_read': False}
    preserve(folder / 'started.json', json.dumps(metadata, indent=2).encode())
    accounts, clock = [], time.monotonic()
    try:
        for name, cls in [('control_cap3', CappedAccount), ('candidate_priority', PriorityAccount)]:
            for cost in ('base', 'stress'):
                accounts.append(cls(config, rules, ticks, fees, cost, folder,
                                    plan=universe, signal_plan=signal, name=name))
        hours = {s: PriorityHour() for s in SYMBOLS}
        funds, cursor, previous = case['funding'], 0, None
        for period in sorted({key[2] for key in index}):
            with ExitStack() as stack:
                keys = [(s, kind) for s in SYMBOLS for kind in ('klines', 'markPriceKlines')]
                readers = [csv.DictReader(stack.enter_context(gzip.open(
                    root / index[s, kind, period]['normalized_path'], 'rt', encoding='utf-8')))
                           for s, kind in keys]
                for bundle in itertools.zip_longest(*readers):
                    require(all(x is not None for x in bundle), 'Unaligned R012 minute lengths')
                    t = int(bundle[0]['open_time_us'])
                    require(all(int(x['open_time_us']) == t for x in bundle)
                            and (previous is None and t == warmup or previous is not None and t == previous + MINUTE)
                            and warmup <= t < end, 'R012 timing gap or out-of-scope data')
                    previous = t
                    data = {s: {} for s in SYMBOLS}
                    for (s, kind), row in zip(keys, bundle):
                        data[s]['trade' if kind == 'klines' else 'mark'] = row
                    if t >= start:
                        current_funds = []
                        while cursor < len(funds) and int(funds[cursor]['event_time_us']) < t + MINUTE:
                            require(int(funds[cursor]['event_time_us']) >= t, 'Skipped R012 funding')
                            current_funds.append(funds[cursor])
                            cursor += 1
                        for account in accounts:
                            account.minute(t, data, current_funds, t == end - MINUTE)
                    features, states = {}, []
                    for s in SYMBOLS:
                        closed, features[s] = hours[s].push(data[s]['trade'])
                        states.append(closed)
                    require(len(set(states)) == 1, 'Unaligned R012 hourly closes')
                    if states[0]:
                        for account in accounts:
                            account.decisions(t + MINUTE, features)
            print(json.dumps({'case': case['label'], 'through': period}), flush=True)
        require(previous == end - MINUTE and cursor == len(funds), 'Incomplete R012 terminal coverage')
        metrics = [a.finish(folder) for a in accounts]
        audit = {a.name: {'multiple_signal_batches': a.priority_batches, 'reordered_batches': a.reordered_batches,
                          'maximum_same_direction': a.maximum_observed_same_direction}
                 for a in accounts if isinstance(a, PriorityAccount)}
        summary = {**metadata, 'status': 'completed', 'accounts': metrics,
                   'priority_audit': audit, 'runtime_seconds': time.monotonic() - clock}
        if case['window']['id'] == 'extension':
            summary['primary_comparison'] = compare_primary(folder, metrics, plan['gates'])
        preserve(folder / 'summary.json', json.dumps(summary, indent=2).encode())
        return relative(root, folder / 'summary.json')
    except Exception as exc:
        for account in accounts:
            for field in ('event_stream', 'gate_log', 'sizing_log'):
                stream = getattr(account, field, None)
                if stream is not None and not stream.closed:
                    stream.close()
        preserve(folder / 'failed.json', json.dumps({**metadata, 'error': str(exc)}, indent=2).encode())
        raise


def aggregate(root, folder, plan, paths, aliases):
    summaries = {read_json(root / path)['case']: read_json(root / path) for path in paths}
    metrics = {label: {m['account']: m for m in s['accounts']} for label, s in summaries.items()}
    all_cases = {**{label: label for label in summaries}, **{x['case']: x['canonical'] for x in aliases}}
    rolling = {}
    for scenario in ('spot', 'ffill'):
        for cost in ('base', 'stress'):
            differences, safe = [], True
            rows = []
            for window in plan['rolling_windows']:
                label = window['id'] + '-' + (scenario if window['corpus_scope'] == 'extension' else 'actual')
                actual = all_cases[label]
                control, candidate = [metrics[actual][prefix + '_' + cost]
                                      for prefix in ('control_cap3', 'candidate_priority')]
                delta = candidate['net_return'] - control['net_return']
                differences.append(delta)
                candidate_safe = candidate['max_adverse_bound_drawdown'] <= float(plan['gates']['maximum_drawdown'])
                candidate_safe = candidate_safe and not candidate['halt_reasons'] and candidate['liquidation_proxy_events'] == 0
                candidate_safe = candidate_safe and candidate['execution_ambiguities'] == 0
                safe = safe and candidate_safe
                rows.append({'window': window['id'], 'canonical_case': actual,
                             'control_return': control['net_return'], 'candidate_return': candidate['net_return'],
                             'improvement': delta, 'candidate_safe': candidate_safe})
            improved = sum(x > 0 for x in differences)
            median = statistics.median(differences)
            rolling[scenario + '_' + cost] = {'windows': rows, 'improved_windows': improved,
                'median_improvement': median, 'all_candidates_safe': bool(safe),
                'passed': improved >= plan['gates']['minimum_improved_rolling_windows_each_cost'] and median > 0 and safe}
    primary_passed = all(summaries['extension-' + scenario]['primary_comparison'][cost]['passed']
                         for scenario in ('spot', 'ffill') for cost in ('base', 'stress'))
    passed = primary_passed and all(x['passed'] for x in rolling.values())
    payload = {'design_id': plan['design_id'], 'status': 'completed',
               'preregistration_commit': PREREGISTRATION,
               'preregistered_config_sha256': sha((root / 'configs/twelfth_round.toml').read_bytes()),
               'candidate_families': 1, 'new_candidate_cost_configurations': 2,
               'account_executions': len(paths) * 4, 'cases': paths, 'aliases': aliases,
               'primary_passed': primary_passed, 'rolling': rolling, 'exploratory_gates_passed': passed,
               'formal_selection_eligible': False, 'independent_validation': False,
               'holdout_read': False, 'automatic_strategy_replacement': False}
    preserve(folder / 'summary.json', json.dumps(payload, indent=2).encode())
    lines = ['# R012：固定动量优先级实验', '',
             '参数及预算在候选回测前提交锁定。全部 2023–2025 历史均已被观察，以下结果为探索诊断；无训练或参数选择。',
             '只改同小时现有信号排序，多空对称，不使用 ADX 门、不改止损退出、不加杠杆、不删币。', '',
             '| 连续扩展期策略 | 成本 | 期末 USDT | 年化 | 不利路径回撤 | 交易数 |',
             '|---|---|---:|---:|---:|---:|']
    for m in summaries['extension-spot']['accounts']:
        label = '原 Phase 1' if m['account'].startswith('control') else '动量优先级'
        cost = m['account'].rsplit('_', 1)[1]
        lines.append(f"| {label} | {cost} | {float(m['ending_equity']):.4f} | {m['cagr']:+.2%} | {m['max_adverse_bound_drawdown']:.2%} | {m['round_trips']} |")
    lines += ['', '连续扩展期为 2024-07-01 至 2026-01-01（不含结束点），每账户共享初始 100 USDT；真实资金费 1 倍。',
              '下面半年窗口各自重新从 100 USDT 开始，不能拼接成可投资曲线，也不能称为滚动样本外。', '',
              '| 半年 | 成本 | 原策略累计收益 | 候选累计收益 | 差额（百分点） | 候选风险门槛 |',
              '|---|---|---:|---:|---:|---|']
    for cost in ('base', 'stress'):
        for row in rolling['spot_' + cost]['windows']:
            lines.append(f"| {row['window']} | {cost} | {row['control_return']:+.2%} | {row['candidate_return']:+.2%} | {100 * row['improvement']:+.2f} | {row['candidate_safe']} |")
    lines += ['', '## 事前固定门槛', '']
    for scenario in ('spot', 'ffill'):
        for cost in ('base', 'stress'):
            result = summaries['extension-' + scenario]['primary_comparison'][cost]
            ci = result['bootstrap']
            failed = [key for key, value in result['checks'].items() if not value]
            lines.append(f"- {scenario}/{cost}：连续期未通过项 {failed}；年化改善 95% 区间 [{100 * ci['cagr_improvement_lower_95']:+.2f}, {100 * ci['cagr_improvement_upper_95']:+.2f}] 个百分点；半年改善 {rolling[scenario + '_' + cost]['improved_windows']}/6。")
    lines += ['', f'探索门槛全部通过：{passed}。正式选模资格始终 false；不自动替换基准。',
              f'执行 {len(paths)} 次、{len(paths) * 4} 个账户；只有 1 个候选机制、2 个候选成本配置。重复输入合并发生在回测前，依据输入内容指纹，非根据收益。',
              '2026 未读取；普通 bootstrap 未修正前面多轮研究的累计选择偏差。失败后不追加参数或方向特例。', '',
              f'- [预注册方案](第十二轮预注册方案.md)',
              f'- [完整汇总](../{relative(root, folder / "summary.json")})']
    text = '\n'.join(lines) + '\n'
    preserve(folder / 'comparison.md', text.encode())
    preserve(root / 'docs/第十二轮实验结果.md', text.encode())
    pointer = {'summary': relative(root, folder / 'summary.json'), 'sha256': sha((folder / 'summary.json').read_bytes()),
               'report': 'docs/第十二轮实验结果.md', 'formal_selection_eligible': False}
    (root / 'artifacts/backtests/priority_latest.json').write_text(json.dumps(pointer, indent=2) + '\n', encoding='utf-8')
    return payload


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def run(root):
    root = Path(root).resolve()
    plan = load_plan(root)
    cases, aliases = prepare_cases(root, plan)
    run_id = 'R012-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir()
    sources = {}
    for name in ('priority.py', 'priority_run.py', 'phase1.py', 'phase1_data.py', 'phase1_run.py',
                 'signal_quality.py', 'universe_engine.py', 'universe_data.py', 'research4.py',
                 'engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py', 'signals.py',
                 'variants.py', 'uncertainty.py', 'mark_proxy.py'):
        body = Path(__file__).with_name(name).read_bytes()
        sources[name] = sha(body)
        preserve(folder / 'source' / name, body)
    for name in ('twelfth_round.toml', 'phase1.toml', 'signal_quality.toml', 'first_round.toml',
                 'execution_policy.toml', 'data_policy.toml', 'universe_expansion.toml', 'mark_proxy.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    preserve(folder / 'source/test_priority.py', (root / 'tests/test_priority.py').read_bytes())
    manifest = {'design_id': plan['design_id'], 'preregistration_commit': PREREGISTRATION,
                'preregistered_plan_sha256': sha((root / 'configs/twelfth_round.toml').read_bytes()),
                'case_inputs': [{'case': c['label'], 'input_sha256': c['input_sha256'], 'window': c['window']}
                                for c in cases], 'aliases_before_backtesting': aliases, 'source_sha256': sources,
                'maximum_account_executions': plan['maximum_account_executions'], 'holdout_read': False}
    preserve(folder / 'started.json', json.dumps(manifest, indent=2).encode())
    print(json.dumps({'run_id': run_id, 'cases': [c['label'] for c in cases],
                      'aliases_before_backtesting': aliases}), flush=True)
    results, failures = [], []
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_case, root, folder, case): case['label'] for case in cases}
        for future in as_completed(futures):
            try:
                path = future.result()
                results.append(path)
                print(json.dumps({'completed': futures[future], 'summary': path}), flush=True)
            except Exception as exc:
                failures.append({'case': futures[future], 'error': str(exc)})
    if failures:
        preserve(folder / 'failed.json', json.dumps({'failures': failures, 'completed': results}, indent=2).encode())
        raise ValueError('R012 has failed cases; no acceptance permitted: ' + str(failures))
    summary = aggregate(root, folder, plan, sorted(results), aliases)
    print(json.dumps({'completed': relative(root, folder),
                      'exploratory_gates_passed': summary['exploratory_gates_passed']}), flush=True)
    return folder, summary
