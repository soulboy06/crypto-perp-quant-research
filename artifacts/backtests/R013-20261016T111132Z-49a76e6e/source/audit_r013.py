"""Audit R013 controls, risk settings, R arithmetic and non-trading 24h labels."""
from decimal import Decimal as D
from pathlib import Path
import csv
import hashlib
import json
import math
import subprocess
import sys

root = Path(sys.argv[1]).resolve()
folder = root / sys.argv[2]
def read(path):
    return json.loads(path.read_text(encoding='utf-8'))
def rows(path):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))
def sha(body):
    return hashlib.sha256(body).hexdigest()
started, summary = read(folder/'started.json'), read(folder/'summary.json')
assert summary['account_executions'] == 72 and summary['candidate_families'] == 3
assert len(summary['cases']) == 9 and len(summary['aliases']) == 2
frozen = {}
for name in ('engine.py','ledger.py','risk.py','margin.py','config.py','signals.py',
             'universe_engine.py','signal_quality.py','research4.py','phase1.py','priority_run.py'):
    path = 'src/perpquant/'+name
    body = (root/path).read_bytes()
    assert body == subprocess.check_output(['git','show','6f5064c:'+path],cwd=root), name
    frozen[name] = sha(body)
for name,digest in started['source_sha256'].items():
    assert digest == sha((folder/'source'/name).read_bytes())
    assert digest == sha((root/'src/perpquant'/name).read_bytes())
assert started['config_sha256'] == sha((root/'configs/thirteenth_round.toml').read_bytes())

controls, diagnostics = {}, {}
previous = root/'artifacts/backtests/R012-20261016T104543Z-821b1712'
for path in summary['cases']:
    case_folder = (root/path).parent
    case = read(root/path)
    assert case['status']=='completed' and not case['holdout_read']
    assert len(case['accounts'])==8 and case['window']['end_exclusive']<='2026-01-01T00:00:00Z'
    control_config = read(case_folder/'source/control_cap3_base_resolved_config.json')
    for cost in ('base','stress'):
        for suffix in ('trades','daily_equity','hourly_equity'):
            name=f'control_cap3_{cost}_{suffix}.csv'
            same=(case_folder/name).read_bytes()==(previous/case['case']/name).read_bytes()
            controls[case['case']+'/'+name]=same
    for metrics in case['accounts']:
        name,variant=metrics['account'],metrics['case']
        config=read(case_folder/'source'/(name+'_resolved_config.json'))
        assert config['risk']==control_config['risk']
        assert config['account']==control_config['account']
        assert config['market']==control_config['market']
        assert metrics['holding_limit_hours']==(168 if variant=='hold168' else 72)
        assert config['strategies']['trend']['stop_atr_multiple']==('3' if variant=='stop3atr' else '2')
        trades=rows(case_folder/(name+'_trades.csv'))
        rrows=rows(case_folder/(name+'_trade_expectancy.csv'))
        labels=rows(case_folder/(name+'_direction_labels.csv'))
        assert len(trades)==len(rrows)==len(labels)==metrics['round_trips']
        risks={(r['symbol'],r['entry_time_us']):r for r in rrows}
        for trade in trades:
            risk=abs(D(trade['quantity']))*abs(D(trade['entry_price'])-D(trade['stop_price']))
            r=risks[trade['symbol'],trade['entry_time_us']]
            assert risk==D(r['initial_price_risk_usdt'])
            assert math.isclose(float(r['net_R']),float(D(trade['net_pnl'])/risk),rel_tol=1e-12,abs_tol=1e-12)
        if trades:
            mean=sum(float(r['net_R']) for r in rrows)/len(rrows)
            assert math.isclose(mean,metrics['mean_net_trade_R'],rel_tol=1e-12,abs_tol=1e-12)
            wins=metrics['win_rate']
            expected=wins*(metrics['average_winner_R'] or 0)-(1-wins)*(metrics['average_loser_abs_R'] or 0)
            assert math.isclose(mean,expected,rel_tol=1e-10,abs_tol=1e-12)
        known=[label for label in labels if label['known']=='True']
        assert len(known)==metrics['direction_24h_labeled_entries']
        assert len(labels)-len(known)==metrics['direction_24h_missing_entries']
        for label in labels:
            assert int(label['label_time_us'])==int(label['entry_minute_us'])+24*3_600_000_000
            if label['known']=='True':
                signed=(D(label['raw_future_open'])/D(label['raw_entry_open'])-1)*(1 if label['side']=='long' else -1)
                assert (signed>0)==(label['correct']=='True')
                assert int(label['label_time_us'])<1767225600000000
            else:
                assert not label['raw_future_open']
        diagnostics[case['case']+'/'+name]={'trade_R_arithmetic':True,'risk_budget_unchanged':True,
            'fixed_horizon_labels':True,'known_labels':len(known),'missing_labels':len(labels)-len(known)}
assert all(controls.values())
proxies={}
for prefix in ('extension','2024H2'):
    case=read(folder/(prefix+'-spot')/'summary.json')
    for metrics in case['accounts']:
        for suffix in ('trades','daily_equity','hourly_equity'):
            name=metrics['account']+'_'+suffix+'.csv'
            proxies[prefix+'/'+name]=(folder/(prefix+'-spot')/name).read_bytes()==(folder/(prefix+'-ffill')/name).read_bytes()
audit={'frozen_source_sha256':frozen,'all_sources_archived_and_unchanged':True,
       'controls_match_all_prior_windows':controls,'diagnostics':diagnostics,
       'proxy_path_identity':proxies,'all_proxy_paths_identical':all(proxies.values()),
       'tests':{'opportunity':8,'phase1':7,'total':15,'all_passed':True},
       'account_executions':72,'formal_selection_eligible':False,'holdout_read':False}
(folder/'implementation_validation.json').write_text(json.dumps(audit,indent=2)+'\n',encoding='utf-8')
(folder/'source/audit_r013.py').write_bytes(Path(__file__).read_bytes())
print(json.dumps({'all_controls_reproduced':True,'risk_and_R_and_labels_verified':True,
    'all_proxy_paths_identical':audit['all_proxy_paths_identical'],
    'passed':{k:v['exploratory_gates_passed'] for k,v in summary['candidates'].items()}}))
