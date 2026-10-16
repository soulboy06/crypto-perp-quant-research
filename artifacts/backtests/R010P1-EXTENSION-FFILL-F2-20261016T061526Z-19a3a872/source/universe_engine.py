"""Fixed ten-coin extension, using original financial accounting and strategy rules."""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv, gzip, itertools, json, tomllib, time
from .archive import preserve
from .config import validate, require, decimal_value as D
from .engine import Account, ReplayLedger, MINUTE, HOUR, stamp_us
from .ledger import InstrumentRules, digest
from .margin import Tier
from .research4 import FourthAccount, FourthHour
from .universe_data import SYMBOLS, load_plan, load_coverage, sha, relative

class UniverseLedger(ReplayLedger):
    """Explicit universe-only constructor extension; no financial methods replaced.

    Validate unchanged first-round settings with the original validator and initialize
    its canonical BTC/ETH ledger, then expand the supplied instrument mapping before
    any event is allowed. Rebind config/rules hashes to the full actual universe.
    Persistent journals are unsupported for this research replay constructor.
    """
    def __init__(self,config,rules,**kwargs):
        require(kwargs.get('journal') is None,'Universe replay does not support a journal')
        symbols=config['market']['symbols']
        require(symbols in (SYMBOLS,SYMBOLS[:2]),'Unregistered universe')
        require(set(rules)==set(symbols) and all(isinstance(x,InstrumentRules) for x in rules.values()),'Missing instrument rules')
        canonical=deepcopy(config)
        canonical['market']['symbols']=SYMBOLS[:2]
        validate(canonical)
        super().__init__(canonical,{s:rules[s] for s in SYMBOLS[:2]},**kwargs)
        self.config=deepcopy(config)
        self.rules=dict(rules)
        self.binding.update(config_sha256=digest(config),rules_sha256=digest({s:asdict(r) for s,r in rules.items()}),
                            universe_extension='UNIV-001-v1' if len(symbols)>2 else 'two_symbol_control')

class UniverseAccount(FourthAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,cost,folder,*,plan,name):
        self.case='efficiency24'
        self.only_entry_rule_changed=False
        self.trail={}
        self.amendment_counts=Counter()
        self.sizing_counts=Counter()
        self.gate_counts=Counter()
        self.plan={**plan,'efficiency24':{'minimum_efficiency':plan['minimum_efficiency'],
            'changed_field':'market.symbols_only','value':','.join(config['market']['symbols'])}}
        Account.__init__(self,config,rules,ticks,liquidation_fees,'trend',cost,folder,
                         account_name=name+'_'+cost,ledger_class=UniverseLedger)
        self.gate_log=(folder/(self.name+'_gate_events.jsonl')).open('x',encoding='utf-8')
        self.sizing_log=(folder/(self.name+'_sizing.jsonl')).open('x',encoding='utf-8')
        preserve(folder/'source'/(self.name+'_resolved_config.json'),json.dumps(config,ensure_ascii=False,indent=2).encode())

    def decisions(self,available,features):
        require(available%HOUR==0,'Signal must use closed hours')
        if not self.ledger.start<=available<self.ledger.end-MINUTE or self.ledger._state['halt_reasons']:return
        actions=[]
        for symbol in self.config['market']['symbols']:
            f=features[symbol]
            if f is None:continue
            p=self.ledger._state['positions'].get(symbol)
            if p:
                normal=f['close']<f['ema50'] if p['quantity']>0 else f['close']>f['ema50']
                if normal or available-self.meta[symbol]['time']>=self.holding_limit_hours*HOUR:
                    actions.append({'kind':'exit','symbol':symbol,'entry_time':self.meta[symbol]['time']})
                continue
            if not f['trend']:continue
            side=f['trend']
            self.signals[symbol+'_'+side]+=1
            context=f.get('higher_4h')
            require(context is None or context['available_us']<=available,'Future higher timeframe')
            passed=context is not None and (context['ema20']>context['ema50'] if side=='long' else context['ema20']<context['ema50'])
            reason='higher_4h_pass' if passed else 'higher_4h_missing' if context is None else 'higher_4h_direction_mismatch'
            self.gate_counts[reason]+=1
            self.gate_record({'symbol':symbol,'side':side,'signal_available_us':available,'scheduled_entry_us':available+MINUTE,
                              'gate':'closed_4h_ema20_ema50','accepted':passed,'context':context})
            if not passed:
                self.rejects['R002:'+reason]+=1
                continue
            er=f.get('efficiency24')
            passed=er is not None and er>=float(self.plan['minimum_efficiency'])
            self.gate_counts['efficiency_pass' if passed else 'efficiency_reject']+=1
            self.gate_record({'gate':'efficiency24','symbol':symbol,'signal_available_us':available,'efficiency':er,
                              'minimum':self.plan['minimum_efficiency'],'accepted':passed})
            if not passed:
                self.rejects['R004:efficiency_below_threshold']+=1
                continue
            actions.append({'kind':'entry','symbol':symbol,'side':side,
                'stop':format(f['close']+(-2 if side=='long' else 2)*f['atr'],'.10f'),
                'signal_available_us':available,'higher_4h':context})
        self.queue[available+MINUTE]=actions

def run(root):
    root=Path(root).resolve()
    plan,base,expanded=load_plan(root)
    coverage=load_coverage(root)
    for config in [base,expanded]:config['data']['development_end']=coverage['common_complete_end_exclusive']
    pointer=json.loads((root/'artifacts/data/universe_latest.json').read_text(encoding='utf-8'))
    manifest_path=root/pointer['manifest']
    require(sha(manifest_path.read_bytes())==pointer['sha256'],'Universe manifest changed')
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    require(manifest['complete_development_coverage'] and not manifest['holdout_history_requested'],'Universe data unavailable')
    require(manifest['plan_sha256']==sha((root/'configs/universe_expansion.toml').read_bytes()),'Data universe differs')
    require(manifest['range_end_exclusive']==coverage['common_complete_end_exclusive'] and not manifest['full_2023_2025_coverage'],'Data window differs')
    rules_path=root/manifest['rules_path']
    require(sha(rules_path.read_bytes())==manifest['rules_sha256'],'Rules changed')
    snapshot=json.loads(rules_path.read_text(encoding='utf-8'))
    policy=tomllib.loads((root/'configs/execution_policy.toml').read_text(encoding='utf-8'))
    start,end=stamp_us(base['data']['development_start']),stamp_us(base['data']['development_end'])
    rules,ticks,fees={},{},{}
    for s in SYMBOLS:
        instrument=snapshot['symbols'][s]
        filters={x['filterType']:x for x in instrument['filters']}
        lot=filters['MARKET_LOT_SIZE']
        tier=Tier('0',policy['notional_coverage_assumption'],policy['maintenance_rate_assumption'],'0','10')
        rules[s]=InstrumentRules(lot['stepSize'],lot['minQty'],lot['maxQty'],filters['MIN_NOTIONAL']['notional'],
                                 (tier,),start,end,'UNIV-001-v1_CURRENT_FILTERS_COUNTERFACTUAL_NOT_HISTORICAL')
        ticks[s]=D(filters['PRICE_FILTER']['tickSize'])
        fees[s]=D(instrument['liquidationFee'])
    run_id='UNIV-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    folder=root/'artifacts/backtests'/run_id
    folder.mkdir(parents=True,exist_ok=False)
    sources={}
    for name in ['universe_engine.py','universe_data.py','engine.py','signals.py','ledger.py','risk.py','margin.py','config.py','variants.py','research4.py']:
        source=Path(__file__).with_name(name).read_bytes()
        preserve(folder/'source'/name,source);sources[name]=sha(source)
    for name in ['first_round.toml','data_policy.toml','execution_policy.toml','universe_expansion.toml','universe_coverage.toml']:
        preserve(folder/'source'/name,(root/'configs'/name).read_bytes())
    metadata={'run_id':run_id,'research_design_id':plan['design_id'],'phase':'expanded_current_survivor_universe_development',
              'holdout_read':False,'formal_selection_eligible':False,'parameter_search':False,
              'new_candidate_cost_configurations':2,'reproduction_control_accounts':2,
              'base_run':plan['base_run'],'data_manifest':pointer['manifest'],'data_manifest_sha256':pointer['sha256'],
              'source_sha256':sources,'rules_sha256':manifest['rules_sha256'],'plan':plan,
              'coverage_policy':coverage,'full_2023_2025_result':False,'development_end_exclusive':base['data']['development_end'],
              'historical_order_filters_verified':False,'historical_margin_rules_verified':False}
    preserve(folder/'started.json',json.dumps(metadata,ensure_ascii=False,indent=2).encode())
    accounts=[]
    for name,config in [('control2',base),('universe10',expanded)]:
        selected=config['market']['symbols']
        for cost in ['base','stress']:
            accounts.append(UniverseAccount(config,{s:rules[s] for s in selected},{s:ticks[s] for s in selected},
                {s:fees[s] for s in selected},cost,folder,plan=plan,name=name))
    index={(x['symbol'],x['kind'],x['period']):x for x in manifest['results']}
    periods=sorted({x['period'] for x in manifest['results']})
    require(len(index)==10*20*3 and periods[0]=='2022-11' and periods[-1]=='2024-06','Incomplete common-window inventory')
    with gzip.open(root/manifest['funding_valuation_path'],'rt',encoding='utf-8') as f:funding=list(csv.DictReader(f))
    require(all(start<=int(x['event_time_us'])<end for x in funding),'Funding crossed boundary')
    fund_index=0
    hours={s:FourthHour() for s in SYMBOLS}
    previous=None
    clock=time.monotonic()
    print(json.dumps({'run_id':run_id,'accounts':[a.name for a in accounts]}),flush=True)
    try:
        for period in periods:
            with ExitStack() as stack:
                keys=[(s,k) for s in SYMBOLS for k in ['klines','markPriceKlines']]
                readers=[csv.DictReader(stack.enter_context(gzip.open(root/index[s,k,period]['normalized_path'],'rt',encoding='utf-8'))) for s,k in keys]
                for bundle in itertools.zip_longest(*readers):
                    require(all(x is not None for x in bundle),'Source length mismatch')
                    t=int(bundle[0]['open_time_us'])
                    require(all(int(x['open_time_us'])==t for x in bundle) and (previous is None or t==previous+MINUTE),'Candle timing gap')
                    require(stamp_us(base['data']['warmup_start'])<=t<end,'Holdout access forbidden')
                    previous=t
                    data={s:{} for s in SYMBOLS}
                    for (s,k),row in zip(keys,bundle):data[s]['trade' if k=='klines' else 'mark']=row
                    if t>=start:
                        funds=[]
                        while fund_index<len(funding) and int(funding[fund_index]['event_time_us'])<t+MINUTE:
                            require(int(funding[fund_index]['event_time_us'])>=t,'Skipped funding')
                            funds.append(funding[fund_index]);fund_index+=1
                        for a in accounts:
                            # Preserve original two-coin intra-minute phases in the reproduction control.
                            own=[x for x in funds if x['symbol'] in a.config['market']['symbols']]
                            a.minute(t,data,own,t==end-MINUTE)
                    features={}
                    for s in SYMBOLS:closed,features[s]=hours[s].push(data[s]['trade'])
                    if closed:
                        for a in accounts:a.decisions(t+MINUTE,features)
            print(json.dumps({'through':period,'equity':{a.name:str(a.ledger.equity) for a in accounts},'trades':{a.name:len(a.trades) for a in accounts}}),flush=True)
        require(fund_index==len(funding),'Funding not consumed')
        metrics=[a.finish(folder) for a in accounts]
        matched={}
        for cost in ['base','stress']:
            for suffix in ['trades','daily_equity']:
                old=root/'artifacts/backtests'/plan['base_run']/('efficiency24_'+cost+'_'+suffix+'.csv')
                new=folder/('control2_'+cost+'_'+suffix+'.csv')
                with old.open(encoding='utf-8-sig',newline='') as f:before=list(csv.DictReader(f))
                with new.open(encoding='utf-8-sig',newline='') as f:after=list(csv.DictReader(f))
                if suffix=='trades':
                    before=[x for x in before if int(x['exit_time_us'])<end-MINUTE]
                    after=[x for x in after if int(x['exit_time_us'])<end-MINUTE]
                else:
                    final_date=datetime.fromtimestamp((end-1)//1_000_000,timezone.utc).strftime('%Y-%m-%d')
                    before=[x for x in before if x['date']<final_date]
                    after=[x for x in after if x['date']<final_date]
                require(before==after,'Original account reproduction failed: '+cost+' '+suffix)
                matched[cost+'_'+suffix+'_prefix_before_forced_terminal_close']=True
        report={**metadata,'status':'completed','completed_at_utc':datetime.now(timezone.utc).isoformat(),
                'accounts':metrics,'original_two_coin_reproduction':matched,'runtime_seconds':time.monotonic()-clock}
        preserve(folder/'summary.json',json.dumps(report,ensure_ascii=False,indent=2).encode())
        (root/'artifacts/backtests/universe_latest.json').write_text(json.dumps({'run_id':run_id,'summary_sha256':sha((folder/'summary.json').read_bytes())},indent=2),encoding='utf-8')
        print(json.dumps({'completed':str(folder),'reproduced':matched,'seconds':report['runtime_seconds']}),flush=True)
        return folder,report
    except Exception as exc:
        for a in accounts:
            for field in ['event_stream','gate_log','sizing_log']:
                stream=getattr(a,field,None)
                if stream is not None and not stream.closed:stream.close()
        preserve(folder/'failed.json',json.dumps({**metadata,'error':str(exc)},ensure_ascii=False,indent=2).encode())
        raise
