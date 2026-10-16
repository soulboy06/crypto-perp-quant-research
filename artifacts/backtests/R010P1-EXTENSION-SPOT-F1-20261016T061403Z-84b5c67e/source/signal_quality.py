"""One frozen entry-extension hypothesis on the fixed ten-coin common window."""
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv, gzip, itertools, json, math, time, tomllib
from .archive import preserve
from .config import require, decimal_value as D
from .engine import MINUTE, stamp_us
from .ledger import InstrumentRules
from .margin import Tier
from .research4 import FourthHour
from .universe_data import SYMBOLS, load_plan, load_coverage, sha, relative
from .universe_engine import UniverseAccount

def extension_assessment(feature,side,maximum=2.0):
    require(side in ('long','short'),'Invalid side')
    values=[feature.get(k) for k in ('close','mean','atr')]
    if any(v is None or not math.isfinite(float(v)) for v in values) or float(values[2])<=0:
        return {'accepted':False,'directional_extension_atr':None,'reason':'unavailable_or_invalid_closed_hour_features'}
    close,mean,atr=map(float,values)
    value=(1 if side=='long' else -1)*(close-mean)/atr
    return {'accepted':value<=maximum,'directional_extension_atr':value,'reason':'within_extension_limit' if value<=maximum else 'overextended_breakout'}

class ExtensionAccount(UniverseAccount):
    def __init__(self,*args,signal_plan,**kwargs):
        self.signal_plan=signal_plan
        super().__init__(*args,**kwargs)
        self.only_entry_rule_changed=True
        self.plan['design_id']=signal_plan['design_id']
        self.plan['efficiency24'].update(changed_field='additional_entry_directional_SMA20_distance_over_ATR14',
                                        value='directional_extension_atr_le_2')

    def decisions(self,available,features):
        super().decisions(available,features)
        due=available+MINUTE
        if due not in self.queue:return
        allowed=[]
        for action in self.queue[due]:
            if action['kind']!='entry':
                allowed.append(action)
                continue
            feature=features[action['symbol']]
            check=extension_assessment(feature,action['side'],float(self.signal_plan['maximum_directional_extension_atr']))
            self.gate_counts['extension_pass' if check['accepted'] else 'extension_reject']+=1
            self.gate_record({'gate':'maximum_directional_SMA20_extension_ATR14','symbol':action['symbol'],'side':action['side'],
                'signal_available_us':available,'scheduled_entry_us':due,'close':feature['close'],'mean20':feature['mean'],
                'atr14':feature['atr'],'maximum':self.signal_plan['maximum_directional_extension_atr'],**check})
            if check['accepted']:allowed.append(action)
            else:self.rejects['SIG001:'+check['reason']]+=1
        self.queue[due]=allowed

def load_signal_plan(root):
    p=tomllib.loads((root/'configs/signal_quality.toml').read_text(encoding='utf-8'))
    require(p['design_id']=='SIG-001-v1' and p['candidate_case']=='extension2atr' and p['maximum_accounts']==4
            and p['new_candidate_cost_configurations']==2 and p['reproduction_control_accounts']==2
            and not p['parameter_search'] and not p['combine_changes'] and p['holdout_locked'],'Signal budget changed')
    require(D(p['maximum_directional_extension_atr'])==2 and p['mean_lookback_hours']==20 and p['atr_lookback_hours']==14
            and p['entry_only'] and p['costs_and_risk_unchanged'] and p['no_coin_removal_or_performance_ranking'],'Signal design changed')
    reference=root/'artifacts/backtests'/p['baseline_run']
    old=json.loads((reference/'summary.json').read_text(encoding='utf-8'))
    require(old['status']=='completed' and not old['holdout_read'] and not old['full_2023_2025_result'],'Invalid baseline')
    for name in ['ledger.py','risk.py','margin.py','config.py','signals.py','universe_engine.py']:
        require(sha(Path(__file__).with_name(name).read_bytes())==old['source_sha256'][name],'Parent financial or strategy source changed: '+name)
    return p,reference,old

def read_csv(path):
    with path.open(encoding='utf-8-sig',newline='') as f:return list(csv.DictReader(f))

def run(root):
    root=Path(root).resolve()
    signal,reference,old=load_signal_plan(root)
    universe,_,config=load_plan(root)
    coverage=load_coverage(root)
    config['data']['development_end']=coverage['common_complete_end_exclusive']
    require(config['data']['development_start']==signal['development_start'] and config['data']['development_end']==signal['development_end_exclusive'],'Comparison dates changed')
    manifest_path=root/old['data_manifest']
    require(sha(manifest_path.read_bytes())==old['data_manifest_sha256'],'Parent corpus changed')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    require(manifest['complete_development_coverage'] and not manifest['holdout_history_requested'],'Data unavailable')
    rules_path=root/manifest['rules_path']
    require(sha(rules_path.read_bytes())==old['rules_sha256'],'Rules changed')
    snapshot=json.loads(rules_path.read_text(encoding='utf-8'))
    policy=tomllib.loads((root/'configs/execution_policy.toml').read_text(encoding='utf-8'))
    start,end=stamp_us(config['data']['development_start']),stamp_us(config['data']['development_end'])
    rules,ticks,fees={},{},{}
    for symbol in SYMBOLS:
        x=snapshot['symbols'][symbol];filters={v['filterType']:v for v in x['filters']};lot=filters['MARKET_LOT_SIZE']
        tier=Tier('0',policy['notional_coverage_assumption'],policy['maintenance_rate_assumption'],'0','10')
        rules[symbol]=InstrumentRules(lot['stepSize'],lot['minQty'],lot['maxQty'],filters['MIN_NOTIONAL']['notional'],(tier,),start,end,'UNIV-001-v1_CURRENT_FILTERS_COUNTERFACTUAL_NOT_HISTORICAL')
        ticks[symbol]=D(filters['PRICE_FILTER']['tickSize']);fees[symbol]=D(x['liquidationFee'])
    run_id='SIG001-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    folder=root/'artifacts/backtests'/run_id;folder.mkdir(parents=True,exist_ok=False)
    sources={}
    for name in ['signal_quality.py','signal_quality_report.py','universe_engine.py','universe_data.py','engine.py','signals.py','ledger.py','risk.py','margin.py','config.py','variants.py','research4.py','uncertainty.py']:
        body=Path(__file__).with_name(name).read_bytes();sources[name]=sha(body);preserve(folder/'source'/name,body)
    for name in ['first_round.toml','execution_policy.toml','data_policy.toml','universe_expansion.toml','universe_coverage.toml','signal_quality.toml']:
        preserve(folder/'source'/name,(root/'configs'/name).read_bytes())
    metadata={'run_id':run_id,'research_design_id':signal['design_id'],'plan':signal,'symbols':SYMBOLS,
        'baseline_run':signal['baseline_run'],'baseline_summary_sha256':sha((reference/'summary.json').read_bytes()),
        'data_manifest':old['data_manifest'],'data_manifest_sha256':old['data_manifest_sha256'],'rules_sha256':old['rules_sha256'],
        'source_sha256':sources,'holdout_read':False,'full_2023_2025_result':False,'formal_selection_eligible':False,
        'new_candidate_cost_configurations':2,'reproduction_control_accounts':2,'parameter_search':False,
        'historical_order_filters_verified':False,'historical_margin_rules_verified':False}
    preserve(folder/'started.json',json.dumps(metadata,ensure_ascii=False,indent=2).encode())
    accounts=[]
    for name in ['control10','extension2atr']:
        cls=UniverseAccount if name=='control10' else ExtensionAccount
        extra={} if name=='control10' else {'signal_plan':signal}
        for cost in ['base','stress']:accounts.append(cls(config,rules,ticks,fees,cost,folder,plan=universe,name=name,**extra))
    baseline_due=defaultdict(list)
    for cost in ['base','stress']:
        for trade in read_csv(reference/('universe10_'+cost+'_trades.csv')):
            baseline_due[int(trade['entry_time_us'])].append((cost,trade))
    impact=[]
    index={(r['symbol'],r['kind'],r['period']):r for r in manifest['results']}
    periods=sorted({r['period'] for r in manifest['results']})
    require(len(index)==10*20*3 and periods[0]=='2022-11' and periods[-1]=='2024-06','Wrong common history')
    with gzip.open(root/manifest['funding_valuation_path'],'rt',encoding='utf-8') as f:funding=list(csv.DictReader(f))
    require(all(start<=int(r['event_time_us'])<end for r in funding),'Funding outside window')
    fund_index=0;previous=None;hours={s:FourthHour() for s in SYMBOLS};clock=time.monotonic()
    print(json.dumps({'run_id':run_id,'accounts':[a.name for a in accounts]}),flush=True)
    try:
        for period in periods:
            with ExitStack() as stack:
                keys=[(s,k) for s in SYMBOLS for k in ['klines','markPriceKlines']]
                readers=[csv.DictReader(stack.enter_context(gzip.open(root/index[s,k,period]['normalized_path'],'rt',encoding='utf-8'))) for s,k in keys]
                for bundle in itertools.zip_longest(*readers):
                    require(all(r is not None for r in bundle),'Unaligned candle lengths')
                    t=int(bundle[0]['open_time_us'])
                    require(all(int(r['open_time_us'])==t for r in bundle) and (previous is None or t==previous+MINUTE),'Candle timing gap')
                    require(stamp_us(config['data']['warmup_start'])<=t<end,'Holdout access forbidden')
                    previous=t;data={s:{} for s in SYMBOLS}
                    for (s,k),row in zip(keys,bundle):data[s]['trade' if k=='klines' else 'mark']=row
                    if t>=start:
                        funds=[]
                        while fund_index<len(funding) and int(funding[fund_index]['event_time_us'])<t+MINUTE:
                            require(int(funding[fund_index]['event_time_us'])>=t,'Skipped funding')
                            funds.append(funding[fund_index]);fund_index+=1
                        for account in accounts:account.minute(t,data,funds,t==end-MINUTE)
                    features={}
                    for s in SYMBOLS:closed,features[s]=hours[s].push(data[s]['trade'])
                    if closed:
                        for account in accounts:account.decisions(t+MINUTE,features)
                        # Retrospective diagnostic is written after decisions and never feeds the accounts.
                        for cost,trade in baseline_due.pop(t+2*MINUTE,[]):
                            f=features[trade['symbol']]
                            check=extension_assessment(f,trade['side'],float(signal['maximum_directional_extension_atr']))
                            impact.append({'cost':cost,'symbol':trade['symbol'],'side':trade['side'],
                                'signal_available_us':t+MINUTE,'entry_time_us':trade['entry_time_us'],
                                'gate_passed':check['accepted'],'extension_atr':check['directional_extension_atr'],
                                'baseline_net_pnl':trade['net_pnl'],'baseline_exit_reason':trade['exit_reason'],
                                'not_counterfactual_account_contribution':True})
            print(json.dumps({'through':period,'equity':{a.name:str(a.ledger.equity) for a in accounts},'trades':{a.name:len(a.trades) for a in accounts}}),flush=True)
        require(fund_index==len(funding) and not baseline_due,'Incomplete funding or baseline attribution')
        metrics=[a.finish(folder) for a in accounts]
        matches={}
        for cost in ['base','stress']:
            for kind in ['trades','daily_equity']:
                require(read_csv(reference/('universe10_'+cost+'_'+kind+'.csv'))==read_csv(folder/('control10_'+cost+'_'+kind+'.csv')),'Ten-coin control changed: '+cost+' '+kind)
                matches[cost+'_'+kind]=True
        with (folder/'baseline_gate_impact.csv').open('x',newline='',encoding='utf-8-sig') as f:
            writer=csv.DictWriter(f,fieldnames=list(impact[0]));writer.writeheader();writer.writerows(impact)
        summary={**metadata,'status':'completed','accounts':metrics,'ten_coin_control_reproduction':matches,
                 'completed_at_utc':datetime.now(timezone.utc).isoformat(),'runtime_seconds':time.monotonic()-clock}
        preserve(folder/'summary.json',json.dumps(summary,ensure_ascii=False,indent=2).encode())
        (root/'artifacts/backtests/signal_latest.json').write_text(json.dumps({'run_id':run_id,'summary_sha256':sha((folder/'summary.json').read_bytes())},indent=2),encoding='utf-8')
        print(json.dumps({'completed':str(folder),'reproduced':matches,'seconds':summary['runtime_seconds']}),flush=True)
        return folder,summary
    except Exception as exc:
        for a in accounts:
            for field in ['event_stream','gate_log','sizing_log']:
                stream=getattr(a,field,None)
                if stream and not stream.closed:stream.close()
        preserve(folder/'failed.json',json.dumps({**metadata,'status':'failed','error':str(exc)},ensure_ascii=False,indent=2).encode())
        raise
