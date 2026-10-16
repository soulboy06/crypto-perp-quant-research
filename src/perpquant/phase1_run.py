"""Four paired Phase 1 accounts; extension and reproduction have distinct labels."""
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
from .phase1_data import window_config
from .uncertainty import paired_cagr_diagnostic


def corpus(root, scope, plan, config, proxy_scenario=None):
    reference = root / 'artifacts/backtests' / plan['baseline_reproduction_run']
    old = json.loads((reference / 'summary.json').read_text(encoding='utf-8'))
    require(old['status'] == 'completed' and old['research_design_id'] == 'SIG-001-v1',
            'Invalid SIG-001 reference')
    for name in ('first_round.toml', 'execution_policy.toml', 'data_policy.toml',
                 'universe_expansion.toml', 'signal_quality.toml'):
        require((root / 'configs' / name).read_bytes() == (reference / 'source' / name).read_bytes(),
                'Frozen SIG-001 settings changed: ' + name)
    for name in ('engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py',
                 'signals.py', 'universe_engine.py', 'signal_quality.py', 'research4.py'):
        require(sha(Path(__file__).with_name(name).read_bytes()) == old['source_sha256'][name],
                'Frozen source changed: ' + name)
    require(proxy_scenario in (None, 'spot', 'ffill'), 'Unknown proxy sensitivity')
    require(proxy_scenario is None or scope == 'extension', 'Proxies only permitted in extension window')
    if proxy_scenario is not None:
        from .mark_proxy import load_policy
        policy = load_policy(root)
        pointer_path = root / 'artifacts/data/mark_proxy_latest.json'
        require(pointer_path.is_file(), 'Prepare authorized mark-proxy data first')
        pointer = json.loads(pointer_path.read_text(encoding='utf-8'))['scenarios'][proxy_scenario]
        path = root / pointer['manifest']
        require(sha(path.read_bytes()) == pointer['sha256'], 'Proxy manifest changed')
    elif scope == 'reproduction':
        path = root / old['data_manifest']
        require(sha(path.read_bytes()) == old['data_manifest_sha256'], 'Old corpus changed')
    else:
        pointer_path = root / 'artifacts/data/phase1_latest.json'
        require(pointer_path.is_file(), 'Prepare Phase 1 extension data first')
        pointer = json.loads(pointer_path.read_text(encoding='utf-8'))
        path = root / pointer['manifest']
        require(sha(path.read_bytes()) == pointer['sha256'], 'Extension manifest changed')
    manifest = json.loads(path.read_text(encoding='utf-8'))
    require(not manifest['holdout_history_requested'], 'Frozen holdout data forbidden')
    if scope == 'extension':
        if proxy_scenario is None:
            require(manifest['complete_extension_coverage'],
                    'Extension data incomplete: no extension backtest is permitted')
        else:
            require(manifest['scenario'] == proxy_scenario and manifest['complete_with_explicit_proxy']
                    and manifest['strict_mark_price_coverage'] is False
                    and manifest['proxy_policy'] == policy
                    and manifest['proxy_policy_sha256'] == sha((root / 'configs/mark_proxy.toml').read_bytes())
                    and manifest['proxy_bar_count'] == 14, 'Proxy data binding or coverage differs')
            actual = {(x['symbol'], b['open_time_us']) for x in manifest['results']
                      for b in x.get('proxy_bars', [])}
            expected = {(s, stamp_us(t)) for s in policy['affected_symbols']
                        for t in policy['gap_open_times_utc']}
            require(actual == expected, 'Proxy timestamp whitelist differs')
        require(manifest['window'] == plan['extension'] and manifest['plan_sha256'] ==
                sha((root / 'configs/phase1.toml').read_bytes()), 'Extension data binding differs')
    index = {(x['symbol'], x['kind'], x['period']): x for x in manifest['results']
             if x.get('audit_passed')}
    expected = {(x.symbol, x.kind, x.period) for x in specs_for(config)}
    require(set(index) == expected, 'Incomplete or out-of-scope data inventory')
    for x in index.values():
        require(sha((root / x['normalized_path']).read_bytes()) == x['normalized_sha256'],
                'Market data changed')
    for label in ('rules', 'funding_valuation'):
        require(sha((root / manifest[label + '_path']).read_bytes()) == manifest[label + '_sha256'],
                label + ' source changed')
    return path, manifest, index, reference


def run(root, scope='extension', *, proxy_scenario=None, funding_multiplier=1):
    root = Path(root).resolve()
    plan, universe, config = window_config(root, scope)
    require(type(funding_multiplier) is int and funding_multiplier in (0, 1, 2)
            and (proxy_scenario is not None or funding_multiplier == 1), 'Unapproved funding sensitivity')
    signal, _, _ = load_signal_plan(root)
    manifest_path, manifest, index, reference = corpus(root, scope, plan, config, proxy_scenario)
    snapshot = json.loads((root / manifest['rules_path']).read_text(encoding='utf-8'))
    policy = tomllib.loads((root / 'configs/execution_policy.toml').read_text(encoding='utf-8'))
    start, end = [stamp_us(config['data'][key]) for key in ('development_start', 'development_end')]
    rules, ticks, fees = {}, {}, {}
    for symbol in SYMBOLS:
        x = snapshot['symbols'][symbol]
        filters = {v['filterType']: v for v in x['filters']}
        lot = filters['MARKET_LOT_SIZE']
        tier = Tier('0', policy['notional_coverage_assumption'], policy['maintenance_rate_assumption'], '0', '10')
        rules[symbol] = InstrumentRules(lot['stepSize'], lot['minQty'], lot['maxQty'],
                                       filters['MIN_NOTIONAL']['notional'], (tier,), start, end,
                                       'R010P1_CURRENT_RULES_PROXY_NOT_HISTORICAL')
        ticks[symbol], fees[symbol] = D(filters['PRICE_FILTER']['tickSize']), D(x['liquidationFee'])
    scope_tag = scope.upper() + ('-' + proxy_scenario.upper() if proxy_scenario else '')
    if proxy_scenario:
        scope_tag += '-F' + str(funding_multiplier)
    run_id = 'R010P1-' + scope_tag + '-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid4().hex[:8]
    folder = root / 'artifacts/backtests' / run_id
    folder.mkdir(parents=True, exist_ok=False)
    sources = {}
    for name in ('phase1.py', 'phase1_data.py', 'phase1_run.py', 'signal_quality.py',
                 'universe_engine.py', 'universe_data.py', 'engine.py', 'ledger.py', 'risk.py',
                 'margin.py', 'config.py', 'signals.py', 'research4.py', 'variants.py', 'uncertainty.py'):
        body = Path(__file__).with_name(name).read_bytes()
        sources[name] = sha(body)
        preserve(folder / 'source' / name, body)
    for name in ('phase1.toml', 'tenth_round_proposal.toml', 'signal_quality.toml',
                 'first_round.toml', 'execution_policy.toml', 'data_policy.toml', 'universe_expansion.toml'):
        preserve(folder / 'source' / name, (root / 'configs' / name).read_bytes())
    if proxy_scenario:
        for name in ('mark_proxy.py', 'proxy_sensitivity.py', 'funding_rest_recovery.py'):
            body = Path(__file__).with_name(name).read_bytes()
            sources[name] = sha(body)
            preserve(folder / 'source' / name, body)
        preserve(folder / 'source/mark_proxy.toml', (root / 'configs/mark_proxy.toml').read_bytes())
        preserve(folder / 'source/test_mark_proxy.py', (root / 'tests/test_mark_proxy.py').read_bytes())
    preserve(folder / 'source/test_phase1.py', (root / 'tests/test_phase1.py').read_bytes())
    metadata = {'run_id': run_id, 'research_design_id': plan['design_id'], 'phase': 1,
                'scope': manifest['scope'] if proxy_scenario else plan[scope]['scope'],
                'run_scope': scope, 'proxy_scenario': proxy_scenario,
                'funding_multiplier': funding_multiplier,
                'funding_rest_verified_all_partitions': manifest.get('funding_rest_verified_all_partitions', True),
                'archive_only_funding_partitions': manifest.get('archive_only_funding_partitions', []),
                'funding_rest_recovered_partitions': manifest.get('funding_rest_recovered_partitions', []),
                'strict_mark_price_coverage': not bool(proxy_scenario),
                'proxy_bar_count': manifest.get('proxy_bar_count', 0), 'window': plan[scope], 'plan': plan,
                'data_manifest': relative(root, manifest_path), 'data_manifest_sha256': sha(manifest_path.read_bytes()),
                'source_sha256': sources, 'rules_sha256': manifest['rules_sha256'],
                'baseline': 'same_window_SIG001_rerun', 'new_candidate_cost_configurations': 2,
                'control_accounts': 2, 'holdout_read': False, 'formal_selection_eligible': False,
                'independent_validation': False, 'phase_2_enabled': False, 'parameter_search': False,
                'historical_order_filters_verified': False, 'historical_margin_rules_verified': False}
    preserve(folder / 'started.json', json.dumps(metadata, ensure_ascii=False, indent=2).encode())
    accounts = []
    clock = time.monotonic()
    try:
        for name, cls in [('control_sig001', ExtensionAccount), ('candidate_cap3', CappedAccount)]:
            for cost in ('base', 'stress'):
                accounts.append(cls(config, rules, ticks, fees, cost, folder,
                                    plan=universe, signal_plan=signal, name=name))
        with gzip.open(root / manifest['funding_valuation_path'], 'rt', encoding='utf-8') as f:
            funding = list(csv.DictReader(f))
        require(all(start <= int(x['event_time_us']) < end for x in funding), 'Funding outside window')
        if funding_multiplier != 1:
            funding = [{**x, 'rate': str(D(x['rate']) * funding_multiplier)} for x in funding]
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
                    require(stamp_us(config['data']['warmup_start']) <= t < end, 'Holdout access forbidden')
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
                    features = {}
                    for s in SYMBOLS:
                        closed, features[s] = hours[s].push(data[s]['trade'])
                    if closed:
                        for a in accounts:
                            a.decisions(t + MINUTE, features)
            print(json.dumps({'run_id': run_id, 'through': period, 'equity': {a.name: str(a.ledger.equity) for a in accounts},
                              'trades': {a.name: len(a.trades) for a in accounts}}), flush=True)
        require(fund_index == len(funding), 'Funding not consumed')
        metrics = [a.finish(folder) for a in accounts]
        matches = {}
        if scope == 'reproduction':
            for cost in ('base', 'stress'):
                for kind in ('trades', 'daily_equity'):
                    require(read_csv(reference / f'extension2atr_{cost}_{kind}.csv') ==
                            read_csv(folder / f'control_sig001_{cost}_{kind}.csv'),
                            'SIG-001 reproduction differs: ' + cost + ' ' + kind)
                    matches[cost + '_' + kind] = True
        cap_audit = {a.name: {'maximum_observed_same_direction': a.maximum_observed_same_direction,
                              'pending_slots_at_end': len(a.direction_slots.pending)}
                     for a in accounts if isinstance(a, CappedAccount)}
        summary = {**metadata, 'status': 'completed', 'accounts': metrics,
                   'SIG001_reproduction': matches, 'direction_cap_audit': cap_audit,
                   'runtime_seconds': time.monotonic() - clock}
        preserve(folder / 'summary.json', json.dumps(summary, ensure_ascii=False, indent=2).encode())
        report(root, folder, summary)
        print(json.dumps({'completed': str(folder), 'reproduced': matches,
                          'direction_cap_audit': cap_audit}), flush=True)
        return folder, summary
    except Exception as exc:
        for a in accounts:
            for field in ('event_stream', 'gate_log', 'sizing_log'):
                stream = getattr(a, field, None)
                if stream is not None and not stream.closed:
                    stream.close()
        preserve(folder / 'failed.json', json.dumps({**metadata, 'error': str(exc)}, indent=2).encode())
        raise


def report(root, folder, summary):
    metrics = {x['account']: x for x in summary['accounts']}
    gates = summary['plan']['gates']
    comparisons = {}
    for cost in ('base', 'stress'):
        b, n = [metrics[name + '_' + cost] for name in ('control_sig001', 'candidate_cap3')]
        bd, nd = [read_csv(folder / (name + '_' + cost + '_daily_equity.csv'))
                  for name in ('control_sig001', 'candidate_cap3')]
        require([x['date'] for x in bd] == [x['date'] for x in nd], 'Unpaired comparison dates')
        ci = paired_cagr_diagnostic([float(x['equity']) for x in bd], [float(x['equity']) for x in nd],
                                   block_days=gates['bootstrap_block_days'], resamples=gates['bootstrap_resamples'],
                                   seed=gates['bootstrap_seed'])
        checks = {'positive_net': n['net_return'] > 0,
                  'minimum_trades': n['round_trips'] >= gates['minimum_trades'],
                  'minimum_cagr_improvement': n['cagr'] - b['cagr'] >= float(gates['minimum_cagr_improvement']),
                  'maximum_drawdown': n['max_adverse_bound_drawdown'] <= float(gates['maximum_drawdown']),
                  'no_halts_liquidations_ambiguities': not n['halt_reasons'] and
                  n['liquidation_proxy_events'] == 0 and n['execution_ambiguities'] == 0,
                  'calmar_non_decreasing': n['calmar'] is not None and b['calmar'] is not None and n['calmar'] >= b['calmar'],
                  'bootstrap_lower_positive': ci['cagr_improvement_lower_95'] > 0}
        comparisons[cost] = {'checks': checks, 'bootstrap': ci,
                             'numerical_checks_passed': all(checks.values())}
    accepted = summary['run_scope'] == 'extension' and all(x['numerical_checks_passed'] for x in comparisons.values())
    payload = {'run_id': summary['run_id'], 'scope': summary['scope'], 'comparisons': comparisons,
               'proxy_scenario': summary.get('proxy_scenario'),
               'numeric_gate_uses_proxy_marks': bool(summary.get('proxy_scenario')),
               'funding_multiplier': summary.get('funding_multiplier', 1),
               'extension_numerical_gate_passed': accepted, 'formal_selection_eligible': False,
               'holdout_read': False, 'phase_2_started': False,
               'reproduction_does_not_authorize_phase_2': True}
    lines = ['# R010 Phase 1：同向最多3仓', '',
             f"运行：{summary['run_id']}；范围：{summary['scope']}。", '',
             f"区间：{summary['window']['start']} 至 {summary['window']['end_exclusive']}（不含结束点）。",
             '候选仅在SIG-001上加同向3仓限制；原信号、止损、退出、成本、资金与风险预算不变。',
             '按原固定币种顺序接受信号，预留名额计入限制；成交检查排除当前订单。拒单释放名额，但不回补同批已拒绝的信号；计划退出成交前仍占位。', '',
             '2026历史未读取。当前币池存在幸存者偏差；历史订单过滤器和维保规则仍为代理。分钟不利路径回撤不保证覆盖真实路径，停机后持有现金不等于持续交易稳定。', '',
             '| 策略 | 成本 | 期末USDT | 累计收益 | 窗口折算年化 | 不利路径最大回撤 | 交易数 | 代理强平／歧义 |',
             '|---|---|---:|---:|---:|---:|---:|---:|']
    if summary.get('proxy_scenario'):
        funding_status = ('全部资金费分区已获REST复核，先前9个分区的例外已解除。' if
                          summary.get('funding_rest_verified_all_partitions') else
                          '另有9个资金费分区仅获官方归档校验、未获REST复核。')
        lines[2:2] = [f"**显式标记价代理场景：{summary['proxy_scenario']}，资金费倍率{summary['funding_multiplier']}，替代14根分钟标记K线；真实标记价未恢复。{funding_status}须与所有其他敏感性场景一起评估，不能挑选更高收益的一组。**", '']
    for name, label in [('control_sig001', 'SIG-001同期对照'), ('candidate_cap3', '同向最多3仓')]:
        for cost in ('base', 'stress'):
            m = metrics[name + '_' + cost]
            lines.append(f"| {label} | {cost} | {float(m['ending_equity']):.4f} | {m['net_return']:+.2%} | {m['cagr']:+.2%} | {m['max_adverse_bound_drawdown']:.2%} | {m['round_trips']} | {m['liquidation_proxy_events']}／{m['execution_ambiguities']} |")
    lines += ['', ('扩展窗口两种成本均通过预设数值门槛；仍非独立验证，不具备正式选模资格。' if accepted else
                   '未获得扩展验证通过资格；不启动Phase 2。'),
              '旧开发窗口仅作实现与复现诊断，不能替代2024H2–2025扩展验证。', '',
              '净收益为正、至少30笔、同期年化改善≥1个百分点、回撤≤20%、无停机/代理强平/歧义、Calmar不降、14天配对循环区块重采样2000次改善区间下限>0，两种成本均需通过。重采样未修正多轮选择偏差，不是未来盈利概率。', '']
    for cost, x in comparisons.items():
        m = metrics['candidate_cap3_' + cost]
        lines.append(f"- {cost}：未通过项目{[k for k, v in x['checks'].items() if not v]}；停机原因{m['halt_reasons']}；同向名额拒单{ {k: v for k, v in m['rejections'].items() if k.startswith('R010P1:')} }。")
    lines += ['', f"SIG-001旧窗口逐笔交易和每日权益复现：{summary['SIG001_reproduction']}。",
              f"仓位名额审计：{summary['direction_cap_audit']}。", '']
    preserve(folder / 'comparison.json', json.dumps(payload, ensure_ascii=False, indent=2).encode())
    preserve(folder / 'comparison.md', '\n'.join(lines).encode())
    pointer = {'run_id': summary['run_id'], 'scope': summary['scope'],
               'summary': relative(root, folder / 'summary.json'),
               'summary_sha256': sha((folder / 'summary.json').read_bytes()),
               'report': relative(root, folder / 'comparison.md'),
               'extension_numerical_gate_passed': accepted, 'formal_selection_eligible': False}
    pointer['proxy_scenario'] = summary.get('proxy_scenario')
    pointer['funding_multiplier'] = summary.get('funding_multiplier', 1)
    report_scope = summary['run_scope'] + ('_' + summary['proxy_scenario'] if summary.get('proxy_scenario') else '')
    if summary.get('proxy_scenario'):
        report_scope += '_funding' + str(summary['funding_multiplier'])
    (root / 'artifacts/backtests' / ('phase1_' + report_scope + '_latest.json')).write_text(
        json.dumps(pointer, ensure_ascii=False, indent=2), encoding='utf-8')
    (root / 'docs' / ('Phase1_' + report_scope + '_结果.md')).write_text(
        '\n'.join(lines), encoding='utf-8')
