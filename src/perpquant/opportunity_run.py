"""R013: three independent user-requested hypotheses on a fixed, already-seen corpus."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import ExitStack
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
from .full_data import sha, relative
from .ledger import InstrumentRules
from .margin import Tier
from .opportunity import OpportunityAccount
from .priority_run import prepare_cases, load_plan as load_window_protocol, read_json
from .research4 import FourthHour
from .signal_quality import load_signal_plan, read_csv
from .uncertainty import paired_cagr_diagnostic
from .universe_data import SYMBOLS

PREREGISTRATION = 'a00e694'


def load_plan(root):
    path = root / 'configs/thirteenth_round.toml'
    body = path.read_bytes()
    registered = subprocess.check_output(['git', 'show', PREREGISTRATION + ':configs/thirteenth_round.toml'], cwd=root)
    require(body == registered, 'R013 preregistered plan changed')
    plan = tomllib.loads(body.decode('utf-8'))
    require(sha((root / plan['window_protocol']).read_bytes()) == plan['window_protocol_sha256'],
            'R013 window protocol changed')
    windows = load_window_protocol(root)
    return {**plan, 'continuous': windows['continuous'], 'rolling_windows': windows['rolling_windows'],
            'gates': windows['gates'], 'proxy_scenarios': windows['proxy_scenarios']}


def compare_primary(folder, metrics, gates, candidates):
    by_name = {m['account']: m for m in metrics}
    results = {}
    for candidate_case in candidates:
        results[candidate_case] = {}
        for cost in ('base', 'stress'):
            control, candidate = [by_name[prefix + '_' + cost] for prefix in
                                  ('control_cap3', 'candidate_' + candidate_case)]
            before, after = [read_csv(folder / (m['account'] + '_daily_equity.csv')) for m in (control, candidate)]
            require([x['date'] for x in before] == [x['date'] for x in after], 'Unpaired R013 dates')
            ci = paired_cagr_diagnostic([float(x['equity']) for x in before], [float(x['equity']) for x in after],
                                       block_days=gates['bootstrap_block_days'], resamples=gates['bootstrap_resamples'],
                                       seed=gates['bootstrap_seed'])
            checks = {'positive_net': candidate['net_return'] > 0,
                      'minimum_trades': candidate['round_trips'] >= gates['minimum_trades'],
                      'cagr_improvement': candidate['cagr'] is not None and control['cagr'] is not None and
                          candidate['cagr'] - control['cagr'] >= float(gates['minimum_cagr_improvement']),
                      'drawdown_limit': candidate['max_adverse_bound_drawdown'] <= float(gates['maximum_drawdown']),
                      'no_halts_liquidations_ambiguities': safe(candidate, gates),
                      'calmar_non_decreasing': candidate['calmar'] is not None and control['calmar'] is not None and
                          candidate['calmar'] >= control['calmar'],
                      'bootstrap_lower_positive': ci['cagr_improvement_lower_95'] > 0,
                      'mean_net_R_improvement': candidate['mean_net_trade_R'] is not None and
                          control['mean_net_trade_R'] is not None and candidate['mean_net_trade_R'] > control['mean_net_trade_R']}
            results[candidate_case][cost] = {'checks': checks, 'bootstrap': ci, 'passed': all(checks.values())}
    return results


def safe(metrics, gates):
    return (metrics['max_adverse_bound_drawdown'] <= float(gates['maximum_drawdown'])
            and not metrics['halt_reasons'] and metrics['liquidation_proxy_events'] == 0
            and metrics['execution_ambiguities'] == 0)


def run_case(root, suite_folder, case):
    root, suite_folder = Path(root), Path(suite_folder)
    plan = load_plan(root)
    config, universe, manifest, index = case['config'], case['universe'], case['manifest'], case['index']
    signal, _, _ = load_signal_plan(root)
    snapshot = read_json(root / manifest['rules_path'])
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
                                       'R013_CURRENT_RULES_PROXY_NOT_HISTORICAL')
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
        specs = [('control_cap3', 'control')] + [('candidate_' + c, c) for c in plan['candidate_cases']]
        for name, hypothesis in specs:
            for cost in ('base', 'stress'):
                accounts.append(OpportunityAccount(config, rules, ticks, fees, cost, folder,
                                    plan=universe, signal_plan=signal, name=name, opportunity_case=hypothesis))
        hours = {s: FourthHour() for s in SYMBOLS}
        funds, cursor, previous = case['funding'], 0, None
        for period in sorted({key[2] for key in index}):
            with ExitStack() as stack:
                keys = [(s, kind) for s in SYMBOLS for kind in ('klines', 'markPriceKlines')]
                readers = [csv.DictReader(stack.enter_context(gzip.open(
                    root / index[s, kind, period]['normalized_path'], 'rt', encoding='utf-8')))
                           for s, kind in keys]
                for bundle in itertools.zip_longest(*readers):
                    require(all(x is not None for x in bundle), 'Unaligned R013 minute lengths')
                    t = int(bundle[0]['open_time_us'])
                    require(all(int(x['open_time_us']) == t for x in bundle)
                            and (previous is None and t == warmup or previous is not None and t == previous + MINUTE)
                            and warmup <= t < end, 'R013 timing gap or out-of-scope data')
                    previous = t
                    data = {s: {} for s in SYMBOLS}
                    for (s, kind), row in zip(keys, bundle):
                        data[s]['trade' if kind == 'klines' else 'mark'] = row
                    if t >= start:
                        current_funds = []
                        while cursor < len(funds) and int(funds[cursor]['event_time_us']) < t + MINUTE:
                            require(int(funds[cursor]['event_time_us']) >= t, 'Skipped R013 funding')
                            current_funds.append(funds[cursor])
                            cursor += 1
                        for account in accounts:
                            account.minute(t, data, current_funds, t == end - MINUTE)
                    features, states = {}, []
                    for s in SYMBOLS:
                        closed, features[s] = hours[s].push(data[s]['trade'])
                        states.append(closed)
                    require(len(set(states)) == 1, 'Unaligned R013 hourly closes')
                    if states[0]:
                        for account in accounts:
                            account.decisions(t + MINUTE, features)
            print(json.dumps({'case': case['label'], 'through': period}), flush=True)
        require(previous == end - MINUTE and cursor == len(funds), 'Incomplete R013 terminal coverage')
        metrics = [a.finish(folder) for a in accounts]
        summary = {**metadata, 'status': 'completed', 'accounts': metrics,
                   'runtime_seconds': time.monotonic() - clock}
        if case['window']['id'] == 'extension':
            summary['primary_comparison'] = compare_primary(folder, metrics, plan['gates'], plan['candidate_cases'])
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
    summaries = {read_json(root / p)['case']: read_json(root / p) for p in paths}
    metrics = {label: {m['account']: m for m in s['accounts']} for label, s in summaries.items()}
    names = {**{label: label for label in summaries}, **{x['case']: x['canonical'] for x in aliases}}
    candidates = {}
    for candidate in plan['candidate_cases']:
        rolling = {}
        for scenario in ('spot', 'ffill'):
            for cost in ('base', 'stress'):
                rows = []
                for window in plan['rolling_windows']:
                    label = window['id'] + '-' + (scenario if window['corpus_scope'] == 'extension' else 'actual')
                    actual = names[label]
                    before, after = [metrics[actual][prefix + '_' + cost]
                                     for prefix in ('control_cap3', 'candidate_' + candidate)]
                    rows.append({'window': window['id'], 'canonical_case': actual,
                                 'control_return': before['net_return'], 'candidate_return': after['net_return'],
                                 'improvement': after['net_return'] - before['net_return'],
                                 'candidate_safe': bool(safe(after, plan['gates']))})
                improved = sum(x['improvement'] > 0 for x in rows)
                median = statistics.median(x['improvement'] for x in rows)
                all_safe = all(x['candidate_safe'] for x in rows)
                rolling[scenario + '_' + cost] = {'windows': rows, 'improved_windows': improved,
                    'median_improvement': median, 'all_candidates_safe': all_safe,
                    'passed': improved >= 4 and median > 0 and all_safe}
        primary = all(summaries['extension-' + scenario]['primary_comparison'][candidate][cost]['passed']
                      for scenario in ('spot', 'ffill') for cost in ('base', 'stress'))
        candidates[candidate] = {'primary_passed': primary, 'rolling': rolling,
            'exploratory_gates_passed': primary and all(x['passed'] for x in rolling.values())}
    payload = {'design_id': plan['design_id'], 'status': 'completed', 'preregistration_commit': PREREGISTRATION,
               'preregistered_config_sha256': sha((root / 'configs/thirteenth_round.toml').read_bytes()),
               'candidate_families': 3, 'new_candidate_cost_configurations': 6, 'account_executions': len(paths) * 8,
               'cases': paths, 'aliases': aliases, 'candidates': candidates,
               'formal_selection_eligible': False, 'independent_validation': False, 'holdout_read': False,
               'automatic_strategy_replacement': False, 'automatic_combination': False}
    preserve(folder / 'summary.json', json.dumps(payload, indent=2).encode())
    lines = ['# R013：持仓期限、止损宽度与入场确认的独立实验', '',
             '三个候选在回测前分别锁定；没有组合、按币种或多空挑规则。全部历史已被观察，结果只是探索诊断。',
             '连续期：2024-07-01 至 2026-01-01（不含结束点），两个月预热、共享初始 100 USDT、真实资金费 1 倍。', '',
             '| 方案 | 成本 | 年化 | 不利路径回撤 | 交易数 | 净盈利胜率 | 平均净 R | 24h方向命中率 |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    for m in summaries['extension-spot']['accounts']:
        def pct(value):
            return f'{value:.2%}' if value is not None else '无样本'
        r = f"{m['mean_net_trade_R']:.4f}" if m['mean_net_trade_R'] is not None else '无样本'
        lines.append(f"| {m['case']} | {m['account'].rsplit('_',1)[1]} | {pct(m['cagr'])} | {pct(m['max_adverse_bound_drawdown'])} | {m['round_trips']} | {pct(m['win_rate'])} | {r} | {pct(m['direction_24h_hit_rate'])} |")
    lines += ['', 'hold168：只将最长期限改为 168 小时；stop3atr：只将初始止损改为 3ATR、原风险预算下自动减仓；confirm2：连续两根小时原信号同方向合格再开仓。',
              '胜率为净盈利交易比例；24h方向标签为实际成交样本的固定期限原始价格方向，不含费用、不参与交易。样本随策略路径改变，不能单独视为因果预测提升。',
              'R 为净盈亏/初始价格止损风险；净利润已扣费用和资金费，R 分母不含费用缓冲，也不是严格最大损失保证。', '', '## 预设门槛及半年诊断', '']
    for candidate, result in candidates.items():
        lines.append(f"### {candidate}：探索门槛通过 {result['exploratory_gates_passed']}")
        lines.append('')
        for scenario in ('spot', 'ffill'):
            for cost in ('base', 'stress'):
                primary = summaries['extension-' + scenario]['primary_comparison'][candidate][cost]
                failed = [k for k, v in primary['checks'].items() if not v]
                ci = primary['bootstrap']
                lines.append(f"- {scenario}/{cost}：未通过 {failed}；年化改善 95% 区间 [{100*ci['cagr_improvement_lower_95']:+.2f}, {100*ci['cagr_improvement_upper_95']:+.2f}] 个百分点；半年改善 {result['rolling'][scenario+'_'+cost]['improved_windows']}/6，半年风险全部通过 {result['rolling'][scenario+'_'+cost]['all_candidates_safe']}。")
        lines += ['', '| 半年 | 成本 | 原策略累计收益 | 候选累计收益 | 候选风险门槛 |', '|---|---|---:|---:|---|']
        for cost in ('base', 'stress'):
            for row in result['rolling']['spot_' + cost]['windows']:
                lines.append(f"| {row['window']} | {cost} | {row['control_return']:+.2%} | {row['candidate_return']:+.2%} | {row['candidate_safe']} |")
        lines.append('')
    lines += ['所有半年各重置 100 USDT，不拼接收益，也不是独立滚动样本外。2026 未读取。',
              '普通 bootstrap 没有修正本轮多候选和此前多轮选择偏差；正式选模资格始终 false，不自动采用或组合候选。',
              f'共 {len(paths)} 次、{len(paths)*8} 个账户，3 个机制、6 个候选成本配置，输入重复在回测前按指纹去重。', '',
              '[事前方案](第十三轮预注册方案.md)', f'[完整汇总](../{relative(root, folder / "summary.json")})']
    report = '\n'.join(lines) + '\n'
    preserve(folder / 'comparison.md', report.encode())
    preserve(root / 'docs/第十三轮实验结果.md', report.encode())
    pointer = {'summary': relative(root, folder / 'summary.json'), 'sha256': sha((folder / 'summary.json').read_bytes()),
               'report': 'docs/第十三轮实验结果.md', 'formal_selection_eligible': False}
    (root / 'artifacts/backtests/opportunity_latest.json').write_text(json.dumps(pointer, indent=2)+'\n', encoding='utf-8')
    return payload


def run(root):
    root = Path(root).resolve()
    plan = load_plan(root)
    cases, aliases = prepare_cases(root, plan)
    require(len(cases) * plan['accounts_per_run'] <= plan['maximum_account_executions'], 'R013 execution budget exceeded')
    run_id = 'R013-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir()
    sources = {}
    for name in ('opportunity.py', 'opportunity_run.py', 'priority_run.py', 'priority.py', 'phase1.py',
                 'phase1_data.py', 'phase1_run.py', 'signal_quality.py', 'universe_engine.py', 'universe_data.py',
                 'research4.py', 'engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py', 'signals.py',
                 'variants.py', 'uncertainty.py', 'mark_proxy.py'):
        body = Path(__file__).with_name(name).read_bytes()
        sources[name] = sha(body)
        preserve(folder / 'source' / name, body)
    for name in ('thirteenth_round.toml', 'twelfth_round.toml', 'phase1.toml', 'signal_quality.toml',
                 'first_round.toml', 'execution_policy.toml', 'data_policy.toml', 'universe_expansion.toml', 'mark_proxy.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    preserve(folder / 'source/test_opportunity.py', (root / 'tests/test_opportunity.py').read_bytes())
    started = {'design_id': plan['design_id'], 'preregistration_commit': PREREGISTRATION,
               'config_sha256': sha((root / 'configs/thirteenth_round.toml').read_bytes()),
               'source_sha256': sources, 'case_inputs': [{'case': c['label'], 'input_sha256': c['input_sha256']}
                    for c in cases], 'aliases_before_backtesting': aliases, 'maximum_account_executions': 72,
               'holdout_read': False, 'independent_validation': False}
    preserve(folder / 'started.json', json.dumps(started, indent=2).encode())
    print(json.dumps({'run_id': run_id, 'cases': [c['label'] for c in cases], 'account_executions': len(cases)*8}), flush=True)
    results, failures = [], []
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_case, root, folder, case): case['label'] for case in cases}
        for future in as_completed(futures):
            try:
                results.append(future.result())
                print(json.dumps({'completed': futures[future]}), flush=True)
            except Exception as exc:
                failures.append({'case': futures[future], 'error': str(exc)})
    if failures:
        preserve(folder / 'failed.json', json.dumps({'failures': failures, 'completed': results}, indent=2).encode())
        raise ValueError('R013 cases failed; no acceptance permitted: ' + str(failures))
    summary = aggregate(root, folder, plan, sorted(results), aliases)
    print(json.dumps({'completed': relative(root, folder),
                      'passed': {c:r['exploratory_gates_passed'] for c,r in summary['candidates'].items()}}), flush=True)
    return folder, summary
