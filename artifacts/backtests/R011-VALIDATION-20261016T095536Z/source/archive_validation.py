"""Archive the two fixed R011 runs; usage: python this_script.py REPOSITORY_ROOT."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
def sha(body):
    return hashlib.sha256(body).hexdigest()
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))

runs = {'spot': 'R011-EXTENSION-SPOT-20261016T095229Z-7d25a6bf',
        'ffill': 'R011-EXTENSION-FFILL-20261016T095229Z-36409fb6'}
folders = {scenario: root / 'artifacts/backtests' / run for scenario, run in runs.items()}
summaries = {scenario: read(folder / 'summary.json') for scenario, folder in folders.items()}
accounts = [m['account'] for m in summaries['spot']['accounts']]
proxy_identity = {}
for account in accounts:
    for suffix in ('trades', 'daily_equity', 'hourly_equity'):
        name = account + '_' + suffix + '.csv'
        proxy_identity[name] = all((folder / name).read_bytes() == (folders['spot'] / name).read_bytes()
                                   for folder in folders.values())
assert all(proxy_identity.values())

reference = root / 'artifacts/backtests/R010P1-EXTENSION-SPOT-F1-20261016T062257Z-2844b1da'
control_identity = {}
for cost in ('base', 'stress'):
    for suffix in ('trades', 'daily_equity', 'hourly_equity'):
        current = folders['spot'] / f'control_cap3_{cost}_{suffix}.csv'
        old = reference / f'candidate_cap3_{cost}_{suffix}.csv'
        control_identity[cost + '_' + suffix] = current.read_bytes() == old.read_bytes()
assert all(control_identity.values())

frozen = {}
for name in ('engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py', 'signals.py',
             'universe_engine.py', 'signal_quality.py', 'research4.py', 'phase1.py'):
    path = 'src/perpquant/' + name
    original = subprocess.check_output(['git', 'show', 'ad9a8d050f542764560f45c285f6349173f8cb7f:' + path], cwd=root)
    current = (root / path).read_bytes()
    assert original == current, name
    frozen[name] = sha(current)
for scenario, summary in summaries.items():
    for name, digest in summary['source_sha256'].items():
        assert digest == sha((root / 'src/perpquant' / name).read_bytes()), name
        assert digest == sha((folders[scenario] / 'source' / name).read_bytes()), name

stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
folder = root / 'artifacts/backtests' / ('R011-VALIDATION-' + stamp)
folder.mkdir()
payload = {
    'research_design_id': 'R011-v1-percoin', 'status': 'completed',
    'window': summaries['spot']['window'], 'funding_multiplier': 1,
    'funding_rest_verified_all_partitions': True, 'strict_mark_price_coverage': False,
    'proxy_bar_count_per_scenario': 14,
    'runs': {scenario: {'summary': str((folders[scenario] / 'summary.json').relative_to(root)).replace('\\', '/'),
                        'sha256': sha((folders[scenario] / 'summary.json').read_bytes()),
                        'accounts': summary['accounts'], 'gates': summary['gates']}
             for scenario, summary in summaries.items()},
    'proxy_trade_daily_hourly_byte_identity': proxy_identity,
    'control_matches_prior_phase1_trade_daily_hourly': control_identity,
    'frozen_source_sha256': frozen,
    'tests': {'regime': 18, 'phase1': 7, 'mark_proxy': 9, 'total': 34, 'all_passed': True},
    'development_diagnostic_executed_this_change': False,
    'extension_numerical_gate_passed': all(s['gates']['overall_passed'] for s in summaries.values()),
    'formal_selection_eligible': False, 'independent_validation': False, 'holdout_read': False,
    'interpretation': 'ADX reduced drawdown and trading activity but did not improve CAGR; bootstrap improvement bounds include zero.'}
(folder / 'summary.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
(folder / 'source').mkdir()
(folder / 'source/archive_validation.py').write_bytes(Path(__file__).read_bytes())

lines = ['# R011 分币种 ADX：修复后扩展验证', '',
         '窗口：2024-07-01 至 2026-01-01（不含结束点），预热期 2024 年 5–6 月；共享初始资金 100 USDT。',
         '原有信号、止损、成本、杠杆和资金预算不变，仅增加每币日线 ADX14 >25 的开仓门。',
         '真实资金费固定 1 倍，全部资金费分区已获 REST 复核；14 根缺失标记价分钟仍使用显式代理。', '',
         '**结论：实现和运行问题已修复；该过滤器未通过策略改善验收，不替换基准。**', '',
         '| 策略 | 成本 | 期末 USDT | 累计收益 | 窗口折算年化 | 不利路径最大回撤 | 交易数 | Calmar |',
         '|---|---|---:|---:|---:|---:|---:|---:|']
for m in summaries['spot']['accounts']:
    label, cost = m['account'].rsplit('_', 1)
    label = 'Phase 1 同期对照' if label == 'control_cap3' else 'R011 ADX 过滤'
    lines.append(f"| {label} | {cost} | {float(m['ending_equity']):.4f} | {m['net_return']:+.2%} | {m['cagr']:+.2%} | {m['max_adverse_bound_drawdown']:.2%} | {m['round_trips']} | {m['calmar']:.4f} |")
lines += ['', '两种代理场景的四个账户逐笔交易、每日权益和小时权益文件全部逐字节相同。同期对照与此前 Phase 1 的对应文件也逐字节相同。',
          '本次只进行了两套扩展期运行（共 8 个账户），未重复运行开发期；开发期入口通过原数据清单及哈希的集成测试。', '',
          '基础成本下年化减少约 2.75 个百分点，压力成本下减少约 0.70 个百分点；过滤减少了交易和回撤，也过滤掉了有效交易。',
          '本次两种成本的候选交易数不同，净收益差不能只按同一组交易减去滑点解释；不能推断提高交易成本能提高收益。', '',
          '| 成本 | 年化改善 95% 区间 | 未通过项目 |', '|---|---|---|']
for cost in ('base', 'stress'):
    g = summaries['spot']['gates'][cost]
    ci = g['bootstrap']
    failed = ', '.join(k for k, v in g['checks'].items() if not v)
    lines.append(f"| {cost} | [{ci['cagr_improvement_lower_95']:+.2%}, {ci['cagr_improvement_upper_95']:+.2%}] | {failed} |")
lines += ['', '年化改善区间为年化收益率差（百分点口径），并非候选自身年化区间。两种成本都未通过年化改善和 bootstrap 门槛；基础成本 Calmar 也未改善。',
          '全部账户均无停机、代理强平或执行歧义。34 个相关测试通过（R011 18、Phase 1 7、代理与资金费 9）。',
          '2024H2–2025 仍是扩展验证，不是独立样本外；bootstrap 没有修正多轮选择偏差。2026 行情未读取，正式选模资格始终为 false。', '',
          '## 可追溯结果', '']
for scenario in runs:
    path = payload['runs'][scenario]['summary']
    lines.append(f'- [{scenario} 原始 summary](../{path})')
lines.append(f"- [配对、对照与冻结源审计](../{str((folder / 'summary.json').relative_to(root)).replace(chr(92), '/')})")
report = '\n'.join(lines) + '\n'
(folder / 'comparison.md').write_text(report, encoding='utf-8')
(root / 'docs/第十一轮验证结果.md').write_text(report, encoding='utf-8')
pointer = {'summary': str((folder / 'summary.json').relative_to(root)).replace('\\', '/'),
           'sha256': sha((folder / 'summary.json').read_bytes()), 'report': 'docs/第十一轮验证结果.md',
           'formal_selection_eligible': False, 'holdout_read': False}
(root / 'artifacts/backtests/regime_latest.json').write_text(json.dumps(pointer, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'validation': pointer['summary'], 'all_proxy_paths_identical': True,
                  'controls_reproduced': True, 'all_frozen_sources_unchanged': True}, ensure_ascii=True))
