"""Add fixed major-coin corpus, retaining original two-symbol evidence unchanged."""
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv, gzip, json, tomllib
from .archive import preserve
from .config import load, require, decimal_value as D
from .full_data import specs_for, prepare_one, fetch, sha, relative
from .reconcile import repair, normalized, compressed_rows

SYMBOLS = ['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','LTCUSDT']

def load_plan(root):
    p=tomllib.loads((root/'configs/universe_expansion.toml').read_text(encoding='utf-8'))
    require(p['design_id']=='UNIV-001-v1' and p['symbols']==SYMBOLS and p['new_symbols']==SYMBOLS[2:], 'Universe plan changed')
    require(p['holdout_locked'] and not p['parameter_search'] and p['maximum_new_strategy_accounts']==2, 'Research budget changed')
    require(p['data_start']=='2022-11-01T00:00:00Z' and p['data_end_exclusive']=='2026-01-01T00:00:00Z', 'History boundary changed')
    c=load(root/'configs/first_round.toml')
    require(c['risk']['per_trade_risk_fraction']==p['per_trade_risk_fraction'] and c['risk']['total_open_risk_fraction']==p['total_open_risk_fraction'], 'Risk changed')
    expanded=deepcopy(c)
    expanded['market']['symbols']=p['symbols'][:]
    return p,c,expanded

def prepare(root):
    root=Path(root).resolve()
    plan,base,config=load_plan(root)
    run_id='UNIVERSE-DATA-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    folder=root/'artifacts/data/universe_runs'/run_id
    folder.mkdir(parents=True,exist_ok=False)
    preserve(folder/'source/universe_data.py',Path(__file__).read_bytes())
    preserve(folder/'source/universe_expansion.toml',(root/'configs/universe_expansion.toml').read_bytes())
    baseline_path=root/plan['base_manifest']
    baseline=json.loads(baseline_path.read_text(encoding='utf-8'))
    require(baseline['complete_development_coverage'] and not baseline['holdout_history_requested'], 'Old corpus invalid')
    metadata={'run_id':run_id,'design_id':plan['design_id'],'symbols':plan['symbols'],'new_symbols':plan['new_symbols'],
              'base_manifest':plan['base_manifest'],'base_manifest_sha256':sha(baseline_path.read_bytes()),
              'plan_sha256':sha((root/'configs/universe_expansion.toml').read_bytes()),
              'holdout_history_requested':False,'historical_order_filters_verified':False,'historical_margin_rules_verified':False,
              'survivorship_bias':plan['survivorship_bias'],'scope':'warmup_and_development_only'}
    preserve(folder/'started.json',json.dumps(metadata,indent=2).encode())
    # Current public rules contain no historical price series. Retain originals for BTC/ETH.
    url='https://fapi.binance.com/fapi/v1/exchangeInfo'
    body=fetch(url,8_000_000)
    preserve(folder/'exchange_info_raw.json',body)
    inventory={x['symbol']:x for x in json.loads(body)['symbols']}
    original_rules=json.loads((root/'artifacts/rules/exchange_rules.json').read_text(encoding='utf-8'))
    chosen=dict(original_rules['symbols'])
    onboard_cutoff=int(datetime(2022,11,1,tzinfo=timezone.utc).timestamp()*1000)
    for symbol in plan['new_symbols']:
        x=inventory[symbol]
        require(x['status']=='TRADING' and x['contractType']=='PERPETUAL' and x['marginAsset']=='USDT', 'Unavailable contract: '+symbol)
        require(x['onboardDate']<onboard_cutoff, 'Insufficient pre-start contract age: '+symbol)
        require({'PRICE_FILTER','MARKET_LOT_SIZE','MIN_NOTIONAL'} <= {f['filterType'] for f in x['filters']}, 'Missing order filters')
        chosen[symbol]=x
    rules={'source':url,'retrieved_at_utc':datetime.now(timezone.utc).isoformat(),'source_sha256':sha(body),
           'scope':'current_snapshot_not_historical','original_BTC_ETH_snapshot_sha256':sha((root/'artifacts/rules/exchange_rules.json').read_bytes()),
           'symbols':chosen}
    rules_path=folder/'exchange_rules.json'
    preserve(rules_path,json.dumps(rules,ensure_ascii=False,indent=2).encode())
    specs=[s for s in specs_for(config) if s.symbol in plan['new_symbols']]
    require(len(specs)==8*38*3,'Unexpected inventory')
    print(json.dumps({'run_id':run_id,'new_archives':len(specs),'rules_verified':plan['symbols']}),flush=True)
    # Reuse successful records of an interrupted/failed preparation; do not re-download audited old data.
    cached={}
    for old in (root/'artifacts/data/universe_runs').glob('*/progress.jsonl'):
        if old.parent==folder:continue
        started=old.parent/'started.json'
        if not started.is_file():continue
        previous=json.loads(started.read_text(encoding='utf-8'))
        if previous.get('plan_sha256')!=metadata['plan_sha256']:continue
        for line in old.read_text(encoding='utf-8').splitlines():
            x=json.loads(line)
            if x.get('audit_passed') and (root/x['normalized_path']).is_file():cached[x['symbol'],x['kind'],x['period']]=x
    records=[]
    with (folder/'progress.jsonl').open('x',encoding='utf-8') as journal:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures={}
            for s in specs:
                key=(s.symbol,s.kind,s.period)
                if key in cached:
                    records.append(cached[key]);journal.write(json.dumps(cached[key])+'\n')
                else:futures[pool.submit(prepare_one,root,config,s)]=s
            for future in as_completed(futures):
                x=future.result()
                if not x['audit_passed'] and x['kind'] in ('klines','markPriceKlines') and 'Missing or extra minute bars' in x.get('error',''):
                    try:x=repair(root,config,x)
                    except Exception as exc:x['repair_error']=str(exc)
                records.append(x)
                journal.write(json.dumps(x,ensure_ascii=False)+'\n');journal.flush()
                if len(records)%24==0 or not x['audit_passed']:
                    print(json.dumps({'completed':len(records),'total':len(specs),'failed':sum(not r['audit_passed'] for r in records),'latest':[x['symbol'],x['kind'],x['period']],'error':x.get('error'),'repair_error':x.get('repair_error')}),flush=True)
    records.sort(key=lambda x:(x['period'],x['symbol'],x['kind']))
    errors=[{k:x.get(k) for k in ['symbol','kind','period','error','repair_error']} for x in records if not x['audit_passed']]
    report={**metadata,'results':baseline['results']+records,'new_archives':len(records),'errors':errors,'complete_development_coverage':False,
            'rules_path':relative(root,rules_path),'rules_sha256':sha(rules_path.read_bytes())}
    if not errors:
        idx={(x['symbol'],x['kind'],x['period']):x for x in records}
        periods=sorted({x['period'] for x in records})
        with gzip.open(root/baseline['funding_valuation_path'],'rt',encoding='utf-8') as f:values=list(csv.DictReader(f))
        dev=int(datetime(2023,1,1,tzinfo=timezone.utc).timestamp()*1_000_000)
        for symbol in plan['new_symbols']:
            previous_mark=previous_event=None
            for period in periods:
                mark_item,fund_item=[idx[symbol,k,period] for k in ['markPriceKlines','fundingRate']]
                marks,funds=[normalized(root,x) for x in [mark_item,fund_item]]
                available=[int(x['available_time_us']) for x in marks]
                if previous_mark:require(int(marks[0]['open_time_us'])==int(previous_mark['available_time_us']),'Month gap')
                api_body=(root/fund_item['funding_rest_path']).read_bytes()
                require(sha(api_body)==fund_item['funding_rest_sha256'],'Funding source changed')
                exact={int(x['fundingTime'])*1000:x.get('markPrice') for x in json.loads(api_body)}
                for x in funds:
                    event,interval=int(x['event_time_us']),int(x['interval_hours'])
                    if previous_event is not None:require(abs(event-previous_event-interval*3_600_000_000)<=60_000_000,'Funding month discontinuity')
                    previous_event=event
                    if event<dev:continue
                    if exact[event] not in (None,''):
                        mark,source,is_exact=str(D(exact[event])),event,True
                    else:
                        j=bisect_right(available,event)-1
                        last=marks[j] if j>=0 else previous_mark
                        require(last is not None,'No past funding mark')
                        mark,source,is_exact=last['close'],int(last['available_time_us']),False
                    require(D(mark)>0 and 0<=event-source<=60_000_000,'Invalid/stale funding mark')
                    values.append({'symbol':symbol,'event_time_us':event,'rate':x['rate'],'interval_hours':interval,'settlement_mark':mark,
                                   'exact_mark':str(is_exact),'source_available_us':source,'age_us':event-source})
                previous_mark=marks[-1]
            print(json.dumps({'funding_valued':symbol}),flush=True)
        values.sort(key=lambda x:(int(x['event_time_us']),x['symbol']))
        packed=compressed_rows(values)
        funding_path=root/'data/validated/funding_valuation'/(sha(packed)+'.csv.gz')
        preserve(funding_path,packed)
        report.update(complete_development_coverage=True,funding_valuation_path=relative(root,funding_path),funding_valuation_sha256=sha(packed),
                      development_funding_valuations=len(values),proxy_valuations=sum(str(x['exact_mark'])=='False' for x in values))
    path=folder/'manifest.json'
    preserve(path,json.dumps(report,ensure_ascii=False,indent=2).encode())
    if not errors:
        (root/'artifacts/data/universe_latest.json').write_text(json.dumps({'manifest':relative(root,path),'sha256':sha(path.read_bytes())},indent=2),encoding='utf-8')
    print(json.dumps({'manifest':str(path),'complete':report['complete_development_coverage'],'errors':errors}),flush=True)
    return path,report
