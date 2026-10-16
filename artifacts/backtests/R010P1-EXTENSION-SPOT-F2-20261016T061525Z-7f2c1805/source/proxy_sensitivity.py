"""Paired strategy/cost results under both prescribed mark substitutions."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json
import csv
import gzip
from concurrent.futures import ProcessPoolExecutor, as_completed
from .archive import preserve
from .config import require, decimal_value as D
from .engine import MINUTE, stamp_us
from .full_data import relative, sha
from .mark_proxy import load_policy
from .phase1_run import run
from .signal_quality import read_csv


def compare_runs(root, folders):
    policy = load_policy(root)
    summaries = {s: json.loads((p / 'summary.json').read_text(encoding='utf-8'))
                 for s, p in folders.items()}
    require(all(x['status'] == 'completed' and not x['holdout_read']
                and x['proxy_scenario'] == scenario for scenario, x in summaries.items()),
            'Sensitivity requires both completed prescribed scenarios')
    require(summaries['spot']['window'] == summaries['ffill']['window'], 'Comparison windows differ')
    comparisons = {s: json.loads((p / 'comparison.json').read_text(encoding='utf-8')) for s, p in folders.items()}
    metrics = {s: {x['account']: x for x in summary['accounts']} for s, summary in summaries.items()}
    differences, exposures = {}, {}
    begin, end = stamp_us(policy['gap_open_times_utc'][0]), stamp_us(policy['gap_open_times_utc'][-1]) + MINUTE
    for name in metrics['spot']:
        a, b = metrics['spot'][name], metrics['ffill'][name]
        trade = {s: read_csv(folder / (name + '_trades.csv')) for s, folder in folders.items()}
        daily = {s: read_csv(folder / (name + '_daily_equity.csv')) for s, folder in folders.items()}
        hourly = {s: read_csv(folder / (name + '_hourly_equity.csv')) for s, folder in folders.items()}
        require([x['date'] for x in daily['spot']] == [x['date'] for x in daily['ffill']], 'Unpaired daily dates')
        differences[name] = {'ending_equity_spot_minus_ffill': str(D(a['ending_equity']) - D(b['ending_equity'])),
                             'net_return_difference': a['net_return'] - b['net_return'],
                             'cagr_difference': a['cagr'] - b['cagr'],
                             'max_drawdown_difference': a['max_adverse_bound_drawdown'] - b['max_adverse_bound_drawdown'],
                             'fee_difference': str(D(a['fees']) - D(b['fees'])),
                             'funding_difference': str(D(a['funding']) - D(b['funding'])),
                             'round_trip_counts_spot_ffill': [a['round_trips'], b['round_trips']],
                             'trade_rows_identical': trade['spot'] == trade['ffill'],
                             'daily_equity_identical': daily['spot'] == daily['ffill'],
                             'hourly_equity_identical': hourly['spot'] == hourly['ffill'],
                             'maximum_absolute_daily_equity_difference': str(max(
                                 abs(D(x['equity']) - D(y['equity'])) for x, y in zip(daily['spot'], daily['ffill']))),
                             'proxy_liquidation_events_spot_ffill': [a['liquidation_proxy_events'], b['liquidation_proxy_events']],
                             'execution_ambiguities_spot_ffill': [a['execution_ambiguities'], b['execution_ambiguities']],
                             'halt_reasons_spot_ffill': [a['halt_reasons'], b['halt_reasons']]}
        exposures[name] = {s: [{k: t[k] for k in ('symbol', 'side', 'entry_time_us', 'exit_time_us', 'quantity')}
                              for t in rows if t['symbol'] in policy['affected_symbols']
                              and int(t['entry_time_us']) < end and int(t['exit_time_us']) >= begin]
                           for s, rows in trade.items()}
    robust_gate = all(x['extension_numerical_gate_passed'] for x in comparisons.values())
    run_id = 'MARK-PROXY-SENS-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    payload = {'run_id': run_id, 'status': 'completed', 'policy_id': policy['policy_id'],
               'window': summaries['spot']['window'],
               'runs': {s: {'summary': relative(root, p / 'summary.json'),
                            'sha256': sha((p / 'summary.json').read_bytes())} for s, p in folders.items()},
               'differences_spot_minus_ffill': differences, 'affected_coin_positions_over_gap': exposures,
               'original_numeric_gates_pass_both_proxy_scenarios_both_costs': robust_gate,
               'strict_mark_price_coverage': False, 'exact_gap_error_measured': False,
               'independent_validation': False, 'formal_selection_eligible': False,
               'holdout_read': False, 'phase_2_started': False}
    lines = ['# 两分钟标记价代理敏感性结果', '',
             f"区间：{payload['window']['start']}至{payload['window']['end_exclusive']}（结束点不含）；热身从{payload['window']['warmup_start']}开始。",
             '7个币各缺2024-08-12 10:02和10:03 UTC，共14根分钟标记K线。仅替换这些行，保留原始文件和其他全部标记观测。',
             'spot使用同币同UTC分钟的现货OHLC；ffill使用10:01真实标记K线收盘形成平坦代理OHLC。均不是恢复出的真实标记价。',
             '两种方案各运行SIG-001与Phase 1候选、基础与压力成本，共8个账户；不按收益选择代理，不改参数。', '',
             '| 代理 | 策略 | 成本 | 期末USDT | 累计收益 | 窗口折算年化 | 不利路径回撤 | 交易数 | 代理强平／歧义 |',
             '|---|---|---|---:|---:|---:|---:|---:|---:|']
    for s in ('spot', 'ffill'):
        for name, m in metrics[s].items():
            label, cost = name.rsplit('_', 1)
            lines.append(f"| {s} | {label} | {cost} | {float(m['ending_equity']):.4f} | {m['net_return']:+.2%} | {m['cagr']:+.2%} | {m['max_adverse_bound_drawdown']:.2%} | {m['round_trips']} | {m['liquidation_proxy_events']}／{m['execution_ambiguities']} |")
    lines += ['', '| 账户 | 期末差USDT（spot−ffill） | 年化差百分点 | 回撤差百分点 | 逐笔交易完全一致 | 每日权益完全一致 |',
              '|---|---:|---:|---:|---|---|']
    for name, x in differences.items():
        lines.append(f"| {name} | {x['ending_equity_spot_minus_ffill']} | {x['cagr_difference'] * 100:+.8f} | {x['max_drawdown_difference'] * 100:+.8f} | {x['trade_rows_identical']} | {x['daily_equity_identical']} |")
    lines += ['', f"两种代理、两种成本下候选均通过原数值门槛：{robust_gate}。正式选模资格仍为false；没有启动Phase 2。",
              '两方案一致只说明本次已实现回放对这两种替代不敏感，不能证明缺失分钟的真实标记价一定不会触及止损、强平或账户保护。止损触发仍取原永续成交价，信号仍取原永续K线。', '',
              '## 缺口期间的受影响币持仓', '']
    for name, x in exposures.items():
        lines.append(f'- {name}：spot {len(x["spot"])}笔，ffill {len(x["ffill"])}笔与缺口分钟相交；明细保存于summary.json。')
    lines += ['', '## 观测偏离及限制', '']
    pointer = json.loads((root / 'artifacts/data/mark_proxy_latest.json').read_text(encoding='utf-8'))
    manifest = json.loads((root / pointer['scenarios']['spot']['manifest']).read_text(encoding='utf-8'))
    for x in manifest['results']:
        if x.get('has_proxy'):
            d = x['observed_spot_mark_deviation']
            lines.append(f"- {x['symbol']}：邻近{d['overlap_minutes']}个有标记价可对照的分钟，现货/标记收盘绝对偏离中位数{float(d['median']):.6%}、P95 {float(d['p95']):.6%}、最大{float(d['maximum']):.6%}。")
    lines += ['', '这些偏离是缺口前后有双边观测时的收盘基差，不是缺失分钟的真实代理误差，也不是分钟极值误差或强平安全边界。现货价格不等于永续标记价，前值平坦代理也没有观测到真实分钟振幅。',
              '2024H2—2025仍是扩展验证而非独立样本外。币池存在幸存者偏差，历史订单与维保规则为代理，分钟OHLC不能确定同分钟止损与强平先后。2026历史未读取，没有连接实盘。', '']
    preserve(folder / 'summary.json', json.dumps(payload, ensure_ascii=False, indent=2).encode())
    preserve(folder / 'comparison.md', '\n'.join(lines).encode())
    (root / 'docs/标记价代理敏感性结果.md').write_text('\n'.join(lines), encoding='utf-8')
    (root / 'artifacts/backtests/mark_proxy_latest.json').write_text(json.dumps(
        {'summary': relative(root, folder / 'summary.json'), 'sha256': sha((folder / 'summary.json').read_bytes()),
         'report': relative(root, folder / 'comparison.md'), 'formal_selection_eligible': False}, indent=2), encoding='utf-8')
    return folder, payload


def run_both(root):
    root = Path(root).resolve()
    policy = load_policy(root)
    cases = {}
    with ProcessPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(run_case, str(root), scenario, multiplier): (scenario, multiplier)
                   for multiplier in policy['funding_sensitivity']['multipliers']
                   for scenario in policy['scenarios']}
        for future in as_completed(futures):
            key = futures[future]
            cases[key] = Path(future.result())
    folders = {s: cases[s, 1] for s in policy['scenarios']}
    folder, summary = compare_runs(root, folders)
    funding = compare_funding(root, folder, cases, policy)
    print(json.dumps({'sensitivity_completed': str(folder),
                      'robust_numeric_gate': summary['original_numeric_gates_pass_both_proxy_scenarios_both_costs'],
                      'funding_sensitivity_immaterial': funding['all_funding_sensitivities_immaterial'],
                      'differences': summary['differences_spot_minus_ffill']}), flush=True)
    return folder, summary


def run_case(root, scenario, multiplier):
    folder, _ = run(Path(root), 'extension', proxy_scenario=scenario, funding_multiplier=multiplier)
    return str(folder)


def compare_funding(root, folder, cases, policy):
    summaries = {key: json.loads((p / 'summary.json').read_text(encoding='utf-8')) for key, p in cases.items()}
    metrics = {key: {m['account']: m for m in x['accounts']} for key, x in summaries.items()}
    verdicts = {key: json.loads((p / 'comparison.json').read_text(encoding='utf-8')) for key, p in cases.items()}
    require(len(cases) == 6 and all(x['window'] == summaries['spot', 1]['window'] for x in summaries.values()),
            'Funding scenarios incomplete or unpaired')
    thresholds = policy['funding_sensitivity']
    diagnostics, unverified_exposure = {}, {}
    rows = ['## 资金费0、1、2倍敏感性', '',
            '按用户追加的方案，对整个扩展窗口的资金费率分别乘0、1、2，正负收付同时缩放；原风险预算中的资金费缓冲不变。两种标记价代理×三种资金费假设×两策略×两成本，共24个账户。2倍不是仅加倍支出的最坏成本情景。', '',
            '回放前固定判定线：期末权益范围≤0.10 USDT，年化范围≤0.10个百分点，最大回撤范围≤0.10个百分点，且交易决策时点、停机、强平、歧义和数值门槛判断不变。', '',
            '| 标记代理 | 账户 | 资金费0／1／2倍期末USDT | 权益范围USDT | 年化范围百分点 | 回撤范围百分点 | 差异很小判定 |',
            '|---|---|---:|---:|---:|---:|---|']
    pointer = json.loads((root / 'artifacts/data/mark_proxy_latest.json').read_text(encoding='utf-8'))
    def decision_signature(trades):
        return [tuple(t[k] for k in ('symbol', 'side', 'entry_time_us', 'exit_time_us', 'exit_reason')) for t in trades]
    for scenario in policy['scenarios']:
        manifest = json.loads((root / pointer['scenarios'][scenario]['manifest']).read_text(encoding='utf-8'))
        with gzip.open(root / manifest['funding_valuation_path'], 'rt', encoding='utf-8') as stream:
            unverified = {(x['symbol'], int(x['event_time_us'])) for x in csv.DictReader(stream)
                          if x['funding_rest_verified'] == 'False'}
        for name in metrics[scenario, 1]:
            values = [metrics[scenario, f][name] for f in (0, 1, 2)]
            trades = {f: read_csv(cases[scenario, f] / (name + '_trades.csv')) for f in (0, 1, 2)}
            span_end = max(D(x['ending_equity']) for x in values) - min(D(x['ending_equity']) for x in values)
            span_cagr = max(x['cagr'] for x in values) - min(x['cagr'] for x in values)
            span_dd = max(x['max_adverse_bound_drawdown'] for x in values) - min(x['max_adverse_bound_drawdown'] for x in values)
            gates = [verdicts[scenario, f]['comparisons'][name.rsplit('_', 1)[1]]['numerical_checks_passed']
                     for f in (0, 1, 2)]
            signatures = [decision_signature(trades[f]) for f in (0, 1, 2)]
            checks = {'equity_range': span_end <= D(thresholds['maximum_end_equity_range_usdt']),
                      'cagr_range': span_cagr <= float(thresholds['maximum_cagr_range']),
                      'drawdown_range': span_dd <= float(thresholds['maximum_drawdown_range']),
                      'same_trade_decisions': signatures[0] == signatures[1] == signatures[2],
                      'same_halts': values[0]['halt_reasons'] == values[1]['halt_reasons'] == values[2]['halt_reasons'],
                      'same_liquidations': len({x['liquidation_proxy_events'] for x in values}) == 1,
                      'same_ambiguities': len({x['execution_ambiguities'] for x in values}) == 1,
                      'same_numeric_gate_verdict': len(set(gates)) == 1}
            key = scenario + '/' + name
            diagnostics[key] = {'checks': checks, 'immaterial': all(checks.values()),
                                'end_equity_range_usdt': str(span_end), 'cagr_range': span_cagr,
                                'drawdown_range': span_dd,
                                'end_equity_f0_f1_f2': [x['ending_equity'] for x in values],
                                'fees_f0_f1_f2': [x['fees'] for x in values],
                                'funding_f0_f1_f2': [x['funding'] for x in values]}
            unverified_exposure[key] = {}
            for multiplier in (0, 1, 2):
                hits, cashflow = 0, D(0)
                with (cases[scenario, multiplier] / (name + '_events.jsonl')).open(encoding='utf-8') as stream:
                    for line in stream:
                        event = json.loads(line)
                        if event['type'] != 'funding' or (event['symbol'], event['time_us']) not in unverified:
                            continue
                        hits += 1
                        matched = [t for t in trades[multiplier] if t['symbol'] == event['symbol'] and
                                   int(t['entry_time_us']) < event['time_us'] <= int(t['exit_time_us'])]
                        require(len(matched) == 1, 'Funding exposure/trade attribution differs')
                        t = matched[0]
                        q = D(t['quantity']) * (1 if t['side'] == 'long' else -1)
                        cashflow -= q * D(event['mark_price']) * D(event['rate'])
                unverified_exposure[key][str(multiplier)] = {'funding_events_with_position': hits,
                                                            'cashflow_usdt': str(cashflow)}
            endings = '／'.join(format(float(x['ending_equity']), '.4f') for x in values)
            rows.append(f"| {scenario} | {name} | {endings} | {span_end:.8f} | {span_cagr * 100:.8f} | {span_dd * 100:.8f} | {all(checks.values())} |")
    immaterial = all(x['immaterial'] for x in diagnostics.values())
    payload = {'policy_id': policy['policy_id'], 'all_funding_sensitivities_immaterial': immaterial,
               'archive_only_nine_partitions_conditionally_accepted_for_this_research': immaterial,
               'funding_REST_verified': False, 'archive_accuracy_proven': False,
               'thresholds': thresholds, 'diagnostics': diagnostics,
               'unverified_partition_exposure': unverified_exposure,
               'runs': {s + '_funding' + str(f): {'summary': relative(root, p / 'summary.json'),
                                                'sha256': sha((p / 'summary.json').read_bytes())} for (s, f), p in cases.items()},
               'formal_selection_eligible': False, 'phase_2_started': False, 'holdout_read': False}
    rows += ['', f'所有账户均符合“差异很小”判定：{immaterial}。',
             ('按用户条件，这9个分区可作为本次研究的官方校验归档数据使用；REST复核仍为false。' if immaterial else
              '未满足用户设定的条件放行思路，不能据此忽略REST证据缺口；完整窗口结果仍是带未复核资金费假设的暂定研究。'),
             '该结论描述本次模型对0、1、2倍资金费假设的敏感性，不证明归档值精确或REST来源已恢复。未复核分区的实际持仓资金费事件与金额单独记录于funding_sensitivity.json；没有暴露时的零差异不代表数据准确性得到检验。', '']
    preserve(folder / 'funding_sensitivity.json', json.dumps(payload, ensure_ascii=False, indent=2).encode())
    preserve(folder / 'funding_sensitivity.md', '\n'.join(rows).encode())
    original = (folder / 'comparison.md').read_text(encoding='utf-8')
    (root / 'docs/标记价代理敏感性结果.md').write_text(original + '\n' + '\n'.join(rows), encoding='utf-8')
    (root / 'artifacts/backtests/mark_proxy_latest.json').write_text(json.dumps(
        {'summary': relative(root, folder / 'summary.json'), 'sha256': sha((folder / 'summary.json').read_bytes()),
         'funding_sensitivity': relative(root, folder / 'funding_sensitivity.json'),
         'funding_sensitivity_sha256': sha((folder / 'funding_sensitivity.json').read_bytes()),
         'report': 'docs/标记价代理敏感性结果.md', 'formal_selection_eligible': False}, indent=2), encoding='utf-8')
    return payload
