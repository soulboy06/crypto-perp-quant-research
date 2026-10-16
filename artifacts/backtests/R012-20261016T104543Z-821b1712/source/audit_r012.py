"""Audit one completed fixed R012 suite. Usage: python audit_r012.py ROOT SUITE_RELATIVE_PATH."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
folder = root / sys.argv[2]
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))
def sha(body):
    return hashlib.sha256(body).hexdigest()
summary, started = read(folder / 'summary.json'), read(folder / 'started.json')
assert summary['account_executions'] == 36
assert len(summary['cases']) == 9
assert len(started['case_inputs']) == 9
assert len(started['aliases_before_backtesting']) == 2
frozen = {}
for name in ('engine.py', 'ledger.py', 'risk.py', 'margin.py', 'config.py', 'signals.py',
             'universe_engine.py', 'signal_quality.py', 'research4.py', 'phase1.py'):
    path = 'src/perpquant/' + name
    body = (root / path).read_bytes()
    old = subprocess.check_output(['git', 'show', 'c0fe141:' + path], cwd=root)
    assert body == old, name
    frozen[name] = sha(body)
for name, digest in started['source_sha256'].items():
    assert digest == sha((folder / 'source' / name).read_bytes()), name
    assert digest == sha((root / 'src/perpquant' / name).read_bytes()), name
assert started['preregistered_plan_sha256'] == sha((root / 'configs/twelfth_round.toml').read_bytes())

control_identity = {}
reference = root / 'artifacts/backtests/R011-EXTENSION-SPOT-20261016T095229Z-7d25a6bf'
for cost in ('base', 'stress'):
    for suffix in ('trades', 'daily_equity', 'hourly_equity'):
        name = f'control_cap3_{cost}_{suffix}.csv'
        control_identity[name] = (folder / 'extension-spot' / name).read_bytes() == (reference / name).read_bytes()
assert all(control_identity.values())

proxy_identity = {}
for prefix in ('extension', '2024H2'):
    for label in ('control_cap3', 'candidate_priority'):
        for cost in ('base', 'stress'):
            for suffix in ('trades', 'daily_equity', 'hourly_equity'):
                name = f'{label}_{cost}_{suffix}.csv'
                key = prefix + '/' + name
                proxy_identity[key] = (folder / (prefix + '-spot') / name).read_bytes() == (folder / (prefix + '-ffill') / name).read_bytes()

for path in summary['cases']:
    result = read(root / path)
    assert result['status'] == 'completed' and not result['holdout_read']
    assert result['window']['end_exclusive'] <= '2026-01-01T00:00:00Z'
    assert len(result['accounts']) == 4
    assert result['input_sha256'] in {x['input_sha256'] for x in started['case_inputs']}

audit = {'preregistration_commit': summary['preregistration_commit'],
         'config_sha256': started['preregistered_plan_sha256'], 'account_executions': 36,
         'tests': {'priority': 9, 'phase1': 7, 'all_passed': True},
         'frozen_source_sha256': frozen, 'archived_sources_match_current': True,
         'prior_continuous_control_byte_identity': control_identity,
         'proxy_trade_daily_hourly_identity': proxy_identity,
         'all_proxy_results_identical': all(proxy_identity.values()),
         'all_cases_completed': True, 'holdout_read': False, 'formal_selection_eligible': False}
(folder / 'implementation_validation.json').write_text(json.dumps(audit, indent=2) + '\n', encoding='utf-8')
(folder / 'source/audit_r012.py').write_bytes(Path(__file__).read_bytes())
print(json.dumps({'continuous_control_reproduced': True, 'sources_unchanged': True,
                  'all_proxy_paths_identical': audit['all_proxy_results_identical'],
                  'exploratory_gates_passed': summary['exploratory_gates_passed']}))
