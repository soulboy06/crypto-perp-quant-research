"""Add fixed major-coin corpus, retaining original two-symbol evidence unchanged."""
from bisect import bisect_right
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import csv, gzip, json, tomllib
from .archive import preserve, allowed_development, verify_checksum
from .config import load, require, decimal_value as D
from .full_data import specs_for, prepare_one, fetch, sha, relative, unpack
from .reconcile import repair, normalized, compressed_rows
from .quality import rows, FUNDING_HEADER, KLINE_HEADER, timestamp, utc_us, crosscheck_funding, candles

SYMBOLS = ['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','LTCUSDT']

def parse_variable_funding(body,start,end):
    """Accept observed interval transitions only; archive/REST times must still agree."""
    data=rows(body,FUNDING_HEADER)
    result,units,transitions=[],set(),[]
    for raw in data:
        event,unit=timestamp(raw[0],start,end)
        units.add(unit)
        require(raw[1].isdigit() and 1<=int(raw[1])<=24 and abs(D(raw[2]))<1,'Invalid funding row')
        interval=int(raw[1])
        if result:
            previous=result[-1]
            gap=event-previous['event_time_us']
            require(gap>0,'Duplicate/reversed funding event')
            expected={interval,previous['interval_hours']}
            require(any(abs(gap-h*3_600_000_000)<=60_000_000 for h in expected),'Unexplained funding gap')
            if interval!=previous['interval_hours']:
                transitions.append({'event_time_us':event,'previous_interval_hours':previous['interval_hours'],
                                    'reported_interval_hours':interval,'actual_previous_gap_us':gap})
        result.append({'event_time_us':event,'interval_hours':interval,'rate':D(raw[2])})
    require(len(units)==1 and result[0]['event_time_us']-start<=60_000_000,'Invalid funding start/unit')
    require(abs(end-result[-1]['event_time_us']-result[-1]['interval_hours']*3_600_000_000)<=60_000_000,'Funding end boundary missing')
    return result,units.pop(),transitions

def prepare_new_one(root,config,spec):
    if spec.kind!='fundingRate':
        result=prepare_one(root,config,spec)
        if spec.kind=='klines' and result.get('error')=='ValueError: Invalid trade volumes':
            return prepare_price_only_anomaly(root,config,spec,result)
        return result
    allowed_development(config,spec)
    record={'symbol':spec.symbol,'kind':spec.kind,'period':spec.period,'frequency':'monthly','url':spec.url()}
    try:
        name=spec.url().rsplit('/',1)[1]
        checksum=fetch(spec.url()+'.CHECKSUM',4096)
        digest=checksum.decode('ascii').split()[0].lower()
        require(len(digest)==64 and all(c in '0123456789abcdef' for c in digest),'Invalid checksum digest')
        path=root/'data/raw'/spec.kind/spec.symbol/spec.period/digest/name
        cached=path.is_file()
        body=path.read_bytes() if cached else fetch(spec.url(),20_000_000)
        verify_checksum(body,checksum,name)
        preserve(path,body)
        checksum_path=path.parent/(sha(checksum)+'.CHECKSUM');preserve(checksum_path,checksum)
        csv_body=unpack(body,name[:-4]+'.csv')
        start,end=map(utc_us,spec.interval())
        parsed,unit,transitions=parse_variable_funding(csv_body,start,end)
        url=f'https://fapi.binance.com/fapi/v1/fundingRate?symbol={spec.symbol}&startTime={start//1000}&endTime={end//1000-1}&limit=1000'
        api=fetch(url,2_000_000)
        payload=json.loads(api)
        require(isinstance(payload,list) and 0<len(payload)<1000,'Funding REST pagination/response invalid')
        comparison=crosscheck_funding(parsed,payload,spec.symbol,start,end)
        api_path=root/'data/raw/funding_api'/spec.symbol/spec.period/(sha(api)+'.json');preserve(api_path,api)
        packed=compressed_rows(parsed)
        normalized_path=root/'data/validated'/spec.kind/spec.symbol/spec.period/(sha(packed)+'.csv.gz');preserve(normalized_path,packed)
        record.update(audit_passed=True,checksum_verified=True,cached_zip=cached,path=relative(root,path),sha256=digest,
            bytes=len(body),checksum_path=relative(root,checksum_path),csv_sha256=sha(csv_body),rows=len(parsed),timestamp_unit=unit,
            first_time_us=parsed[0]['event_time_us'],last_time_us=parsed[-1]['event_time_us'],normalized_path=relative(root,normalized_path),
            normalized_sha256=sha(packed),funding_rest=comparison,funding_rest_path=relative(root,api_path),funding_rest_sha256=sha(api),
            funding_rest_url=url,observed_interval_transitions=transitions,interval_transition_rest_times_verified=True)
    except Exception as exc:record.update(audit_passed=False,error=f'{type(exc).__name__}: {exc}')
    return record

def prepare_price_only_anomaly(root,config,spec,original):
    """Retain vendor volume defects; validate price-only inputs against REST overlap."""
    allowed_development(config,spec)
    record=dict(original)
    try:
        name=spec.url().rsplit('/',1)[1]
        candidates=list((root/'data/raw'/spec.kind/spec.symbol/spec.period).glob('*/'+name))
        require(len(candidates)==1,'Ambiguous original volume-defect source')
        path=candidates[0];body=path.read_bytes()
        checks=list(path.parent.glob('*.CHECKSUM'))
        require(len(checks)==1,'Ambiguous checksum')
        verify_checksum(body,checks[0].read_bytes(),name)
        csv_body=unpack(body,name[:-4]+'.csv')
        raw=rows(csv_body,KLINE_HEADER)
        start,end=map(utc_us,spec.interval())
        anomalous=[x for x in raw if not (0<=D(x[9])<=D(x[5]) and 0<=D(x[10])<=D(x[7]))]
        require(0<len(anomalous)<=10,'Unexpected number of volume defects')
        evidence=[]
        originals={timestamp(x[0],start,end)[0]:x for x in raw}
        for row in anomalous:
            event,_=timestamp(row[0],start,end)
            left,right=max(start,event-60_000_000),min(end,event+120_000_000)
            require(left<right<=end,'REST price audit outside month')
            url=f'https://fapi.binance.com/fapi/v1/klines?symbol={spec.symbol}&interval=1m&startTime={left//1000}&endTime={right//1000-1}&limit=10'
            api=fetch(url,100_000)
            payload=json.loads(api)
            require(isinstance(payload,list) and len(payload)==(right-left)//60_000_000,'Incomplete price overlap response')
            api_path=root/'data/raw/volume_anomaly_api'/spec.symbol/spec.period/(sha(api)+'.json');preserve(api_path,api)
            for x in payload:
                t,_=timestamp(str(x[0]),left,right)
                require([D(v) for v in originals[t][1:5]]==[D(str(v)) for v in x[1:5]],'REST/archive OHLC conflict at volume defect')
            require(any(int(x[0])*1000==event for x in payload),'REST did not include anomalous minute')
            evidence.append({'event_time_us':event,'original_row':row,'url':url,'path':relative(root,api_path),
                             'sha256':sha(api),'OHLC_overlap_rows':len(payload),'volume_not_replaced':True})
        # Canonical strategy table contains only timestamps and OHLC; no volume field is invented.
        parsed,unit=candles(csv_body,start,end,mark=True)
        packed=compressed_rows(parsed)
        target=root/'data/validated'/spec.kind/spec.symbol/spec.period/(sha(packed)+'.csv.gz');preserve(target,packed)
        record.pop('error',None)
        record.update(audit_passed=True,audit_scope='price_and_timing_only_with_explicit_excluded_volume_anomaly',
            volume_fields_valid=False,volume_anomalies=evidence,volume_features_permitted=False,
            checksum_verified=True,path=relative(root,path),sha256=sha(body),checksum_path=relative(root,checks[0]),
            csv_sha256=sha(csv_body),bytes=len(body),rows=len(parsed),timestamp_unit=unit,first_time_us=parsed[0]['open_time_us'],
            last_time_us=parsed[-1]['open_time_us'],normalized_path=relative(root,target),normalized_sha256=sha(packed))
    except Exception as exc:record.update(audit_passed=False,error=f'{type(exc).__name__}: {exc}')
    return record

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

def load_coverage(root):
    p=tomllib.loads((root/'configs/universe_coverage.toml').read_text(encoding='utf-8'))
    require(p['policy_id']=='UNIV-COVERAGE-001-v1' and p['common_complete_end_exclusive']=='2024-07-01T00:00:00Z'
            and p['declared_before_any_expanded_universe_backtest'] and not p['date_selection_uses_returns']
            and not p['full_2023_2025_result_claim_permitted'] and not p['allow_missing_price_fill'],'Coverage policy changed')
    return p

def prepare(root):
    root=Path(root).resolve()
    plan,base,config=load_plan(root)
    coverage=load_coverage(root)
    config['data']['development_end']=coverage['common_complete_end_exclusive']
    run_id='UNIVERSE-DATA-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid4().hex[:8]
    folder=root/'artifacts/data/universe_runs'/run_id
    folder.mkdir(parents=True,exist_ok=False)
    preserve(folder/'source/universe_data.py',Path(__file__).read_bytes())
    preserve(folder/'source/universe_expansion.toml',(root/'configs/universe_expansion.toml').read_bytes())
    preserve(folder/'source/universe_coverage.toml',(root/'configs/universe_coverage.toml').read_bytes())
    baseline_path=root/plan['base_manifest']
    baseline=json.loads(baseline_path.read_text(encoding='utf-8'))
    require(baseline['complete_development_coverage'] and not baseline['holdout_history_requested'], 'Old corpus invalid')
    metadata={'run_id':run_id,'design_id':plan['design_id'],'symbols':plan['symbols'],'new_symbols':plan['new_symbols'],
              'base_manifest':plan['base_manifest'],'base_manifest_sha256':sha(baseline_path.read_bytes()),
              'plan_sha256':sha((root/'configs/universe_expansion.toml').read_bytes()),
              'holdout_history_requested':False,'historical_order_filters_verified':False,'historical_margin_rules_verified':False,
              'survivorship_bias':plan['survivorship_bias'],'scope':'warmup_and_common_complete_development_window_only',
              'range_end_exclusive':config['data']['development_end'],'coverage_policy':coverage,
              'full_2023_2025_coverage':False}
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
    require(len(specs)==8*20*3,'Unexpected inventory')
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
        with ProcessPoolExecutor(max_workers=16) as pool:
            futures={}
            for s in specs:
                key=(s.symbol,s.kind,s.period)
                if key in cached:
                    records.append(cached[key]);journal.write(json.dumps(cached[key])+'\n')
                else:futures[pool.submit(prepare_new_one,root,config,s)]=s
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
    retained_base=[x for x in baseline['results'] if x['period']<config['data']['development_end'][:7]]
    report={**metadata,'results':retained_base+records,'new_archives':len(records),'errors':errors,'complete_development_coverage':False,
            'rules_path':relative(root,rules_path),'rules_sha256':sha(rules_path.read_bytes())}
    if not errors:
        idx={(x['symbol'],x['kind'],x['period']):x for x in records}
        periods=sorted({x['period'] for x in records})
        cutoff=int(datetime.fromisoformat(config['data']['development_end'].replace('Z','+00:00')).timestamp()*1_000_000)
        with gzip.open(root/baseline['funding_valuation_path'],'rt',encoding='utf-8') as f:values=[x for x in csv.DictReader(f) if int(x['event_time_us'])<cutoff]
        dev=int(datetime(2023,1,1,tzinfo=timezone.utc).timestamp()*1_000_000)
        for symbol in plan['new_symbols']:
            previous_mark=previous_event=previous_interval=None
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
                    if previous_event is not None:require(any(abs(event-previous_event-h*3_600_000_000)<=60_000_000 for h in {interval,previous_interval}),'Funding month discontinuity')
                    previous_event,previous_interval=event,interval
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
