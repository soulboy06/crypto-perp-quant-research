"""Four fixed development screens under explicit counterfactual execution rules."""
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import csv
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import gzip
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import tomllib
from uuid import uuid4
from .archive import preserve
from .config import load, require, decimal_value as D
from .ledger import Ledger, InstrumentRules, DAY_US
from .margin import Tier, liquidation_price
from .signals import Hour
from .quality import under

MINUTE = 60_000_000
HOUR = 60*MINUTE


def sha(body):
    return hashlib.sha256(body).hexdigest()


def stamp_us(text):
    return int(datetime.fromisoformat(text.replace("Z","+00:00")).timestamp())*1_000_000


def rounded(price,tick,up):
    return (D(price)/tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)*tick


class ReplayLedger(Ledger):
    """Stream observations without millions of redundant JSON mark snapshots.

    Trading events keep Ledger's atomic accounting checks. Reproduce marks by
    replaying the bound source corpus; this mode cannot use the SQLite journal.
    """
    def observe(self,stamp,prices):
        require(self._connection is None and not self._closed,"Observation mode requires an open in-memory ledger")
        require(type(stamp) is int and self.start<=stamp<self.end and stamp>=self._state["last_time_us"],"Invalid observation time")
        require(set(prices)==set(self._state["positions"]),"Incomplete mark snapshot")
        with localcontext() as ctx:
            ctx.prec=40
            state=self._state.copy()
            state["positions"]={s:p.copy() for s,p in state["positions"].items()}
            state["orders"]={s:o.copy() for s,o in state["orders"].items()}
            state["halt_reasons"]=list(state["halt_reasons"])
            day=stamp//DAY_US
            require(day==state["day"] or (day==state["day"]+1 and stamp%DAY_US==0),"Missing UTC day boundary")
            for symbol,price in prices.items():
                require(D(price)>0,"Invalid mark")
                state["positions"][symbol].update(mark=D(price),mark_time_us=stamp)
            if day!=state["day"]:
                state.update(day=day,day_start_equity=self.equity_of(state),daily_paused=False)
            state["last_time_us"]=stamp
            self._protect(state,stamp)
            self._state=state

    def _exit(self,state,event):
        super()._exit(state,event)
        if "liquidation_extra_rate" in event:
            require(event.get("reason")=="liquidation_proxy" and event.get("policy_id")=="EXEC-001-v1","Unbound liquidation surcharge")
            rate=D(event["liquidation_extra_rate"])
            require(0<=rate<1,"Invalid liquidation surcharge")
            fee=D(event["quantity"])*D(event["price"])*rate
            state["cash"]-=fee
            state["fees"]+=fee


class Account:
    def __init__(self,config,rules,ticks,liquidation_fees,strategy,cost,folder,*,account_name=None,ledger_class=ReplayLedger):
        self.config,self.rules,self.ticks,self.liquidation_fees=config,rules,ticks,liquidation_fees
        self.strategy,self.cost=strategy,cost
        self.name=account_name or strategy+"_"+cost
        self.ledger=ledger_class(config,rules,funding_policy="available_first_credit_cash_research_v1",cost=cost)
        self.adverse=D(config["costs"][cost]["adverse_price_rate"])
        self.queue={}
        self.meta={}
        self.rejects=Counter()
        self.signals=Counter()
        self.events=0
        self.trades=[]
        self.daily=[]
        self.hourly=[]
        self.maxdd=0.0
        self.ambiguities=0
        self.liquidations=0
        self.gross_turnover=D(0)
        self.event_stream=(folder/(self.name+"_events.jsonl")).open("x",encoding="utf-8")

    @property
    def holding_limit_hours(self):
        return self.config["strategies"][self.strategy]["max_holding_hours"]

    def track(self):
        s=self.ledger._state
        self.maxdd=max(self.maxdd,float(1-self.ledger.equity/s["peak_equity"]))

    def observe(self,t,data,field):
        positions=self.ledger._state["positions"]
        prices={symbol:data[symbol]["mark"][field] for symbol in positions}
        self.ledger.observe(t,prices)
        self.track()

    def emit(self,kind,t,**values):
        self.events+=1
        event={"id":f"{self.name}-{self.events}","type":kind,"time_us":t,**values}
        result=self.ledger.apply(event)
        self.event_stream.write(json.dumps(event,ensure_ascii=False)+"\n")
        self.track()
        return result

    def execution_price(self,symbol,reference,buy):
        value=D(reference)*(1+self.adverse if buy else 1-self.adverse)
        return rounded(value,self.ticks[symbol],buy)

    def exit(self,symbol,t,reference,reason,funding_minute=False,alternative=None):
        p=self.ledger._state["positions"][symbol].copy()
        q=p["quantity"]
        price=self.execution_price(symbol,reference,q<0)
        before=self.ledger._state["fees"]
        extra={}
        if reason=="liquidation_proxy":
            extra={"liquidation_extra_rate":str(self.liquidation_fees[symbol]),"policy_id":"EXEC-001-v1"}
            self.liquidations+=1
        if funding_minute:
            self.ambiguities+=1
        self.emit("exit",t,symbol=symbol,quantity=str(abs(q)),price=str(price),mark_price=str(p["mark"]),reason=reason,**extra)
        fee=self.ledger._state["fees"]-before
        meta=self.meta.pop(symbol)
        realized=q*(price-p["entry"])
        net=realized+meta["funding"]-fee-meta["entry_fee"]
        trade={"symbol":symbol,"side":"long" if q>0 else "short","entry_time_us":meta["time"],"exit_time_us":t,
               "quantity":str(abs(q)),"entry_price":str(p["entry"]),"exit_price":str(price),"stop_price":str(p["stop"]),
               "gross_pnl":str(realized),"funding":str(meta["funding"]),"fees":str(fee+meta["entry_fee"]),"net_pnl":str(net),
               "exit_reason":reason,"funding_timing_ambiguous":funding_minute}
        if alternative is not None:
            alternative_price=self.execution_price(symbol,alternative,q<0)
            trade["local_stop_alternative_net_pnl"]=str(q*(alternative_price-p["entry"])+meta["funding"]-meta["entry_fee"]-abs(q)*alternative_price*self.ledger.fee)
            trade["full_path_counterfactual_replayed"]=False
        self.trades.append(trade)
        self.gross_turnover+=abs(q)*price

    def entry(self,symbol,t,action,data):
        if symbol in self.ledger._state["positions"]:
            self.rejects["symbol_already_open"]+=1
            return
        side=action["side"]
        price=self.execution_price(symbol,data[symbol]["trade"]["open"],side=="long")
        stop=rounded(action["stop"],self.ticks[symbol],side=="long")
        mark=D(data[symbol]["mark"]["open"])
        if not (stop<min(price,mark) if side=="long" else stop>max(price,mark)):
            self.rejects["stop_crossed_before_entry"]+=1
            return
        sign=1 if side=="long" else -1
        threshold=liquidation_price(D(sign),price,price/D(self.config["risk"]["venue_leverage"]),self.rules[symbol].tiers)
        buffer=price*(self.adverse+self.ledger.fee+D(self.config["risk"]["gap_buffer_rate"]))
        if threshold is not None and not (stop>threshold+buffer if sign>0 else stop<threshold-buffer):
            self.rejects["stop_outside_assumed_liquidation_buffer"]+=1
            return
        order=f"{symbol}-{t}"
        try:
            self.emit("reserve",t,symbol=symbol,side=side,order_id=order,reference_price=str(price),stop_price=str(stop))
        except ValueError as exc:
            self.rejects[str(exc)]+=1
            return
        quantity=self.ledger._state["orders"][order]["remaining"]
        before=self.ledger._state["fees"]
        try:
            self.emit("fill",t,order_id=order,quantity=str(quantity),price=str(price),mark_price=str(mark))
        except ValueError as exc:
            self.emit("cancel",t,order_id=order)
            self.rejects[str(exc)]+=1
            return
        self.meta[symbol]={"time":t,"funding":D(0),"entry_fee":self.ledger._state["fees"]-before}
        self.gross_turnover+=quantity*price

    def decisions(self,available,features):
        require(available%HOUR==0,"Signal must use a closed hour")
        if not self.ledger.start<=available<self.ledger.end-MINUTE:
            return
        if self.ledger._state["halt_reasons"]:
            return
        actions=[]
        for symbol in ("BTCUSDT","ETHUSDT"):
            f=features[symbol]
            if f is None:
                continue
            p=self.ledger._state["positions"].get(symbol)
            if p:
                limit=self.holding_limit_hours*HOUR
                level=f["ema50"] if self.strategy=="trend" else f["mean"]
                normal=(f["close"]<level if p["quantity"]>0 else f["close"]>level) if self.strategy=="trend" else (f["close"]>=level if p["quantity"]>0 else f["close"]<=level)
                if normal or available-self.meta[symbol]["time"]>=limit:
                    actions.append({"kind":"exit","symbol":symbol,"entry_time":self.meta[symbol]["time"]})
            elif f[self.strategy]:
                side=f[self.strategy]
                stop=f["close"]+(-2 if side=="long" else 2)*f["atr"]
                actions.append({"kind":"entry","symbol":symbol,"side":side,"stop":format(stop,".10f")})
                self.signals[symbol+"_"+side]+=1
        self.queue[available+MINUTE]=actions

    def process_adjustments(self,t,phase,data,due,funded):
        """Optional research stop amendments, after existing exits and before entries."""

    def minute(self,t,data,funds,last):
        state=self.ledger._state
        if state["positions"] or t%DAY_US==0:
            self.observe(t,data,"open")
        phase=t
        funded=set()
        for fund in funds:
            event=int(fund["event_time_us"])
            symbol=fund["symbol"]
            phase=max(phase,event+1)
            if symbol in state["positions"]:
                delta=-state["positions"][symbol]["quantity"]*D(fund["settlement_mark"])*D(fund["rate"])
                self.emit("funding",event,symbol=symbol,mark_price=fund["settlement_mark"],rate=fund["rate"],
                          exact_mark=fund["exact_mark"]=="True",source_available_us=int(fund["source_available_us"]))
                self.meta[symbol]["funding"]+=delta
                funded.add(symbol)
                state=self.ledger._state
        if state["positions"]:
            self.observe(phase,data,"open")
        for symbol in list(self.ledger._state["positions"]):
            p=self.ledger._state["positions"][symbol]
            opening=D(data[symbol]["trade"]["open"])
            crossed=opening<=p["stop"] if p["quantity"]>0 else opening>=p["stop"]
            expired=phase-self.meta[symbol]["time"] >= self.holding_limit_hours*HOUR
            reasons=self.ledger._state["halt_reasons"]
            liq=f"liquidation_required:{symbol}" in reasons
            if last or crossed or expired or reasons or self.ledger._state.get("exposure_reduction_required"):
                reason="liquidation_proxy" if liq else "end_of_partition" if last else "gap_stop" if crossed else "account_protection" if reasons or self.ledger._state.get("exposure_reduction_required") else "time_limit"
                self.exit(symbol,phase,opening,reason,symbol in funded)
        due=self.queue.pop(t,[])
        for action in due:
            symbol=action["symbol"]
            if action["kind"]=="exit" and symbol in self.meta and self.meta[symbol]["time"]==action["entry_time"]:
                self.exit(symbol,phase,data[symbol]["trade"]["open"],"hourly_exit",symbol in funded)
        if not last:
            self.process_adjustments(t,phase,data,due,funded)
            for action in due:
                if action["kind"]=="entry":
                    if self.ledger._state["positions"]:
                        self.observe(phase,data,"open")
                    self.entry(action["symbol"],phase,action,data)
        positions=self.ledger._state["positions"]
        if positions:
            adverse={s:data[s]["mark"]["low" if p["quantity"]>0 else "high"] for s,p in positions.items()}
            self.ledger.observe(t+MINUTE-1,adverse)
            self.track()
            state=self.ledger._state
            for symbol in list(state["positions"]):
                p=self.ledger._state["positions"][symbol]
                extreme=D(data[symbol]["trade"]["low" if p["quantity"]>0 else "high"])
                stopped=extreme<=p["stop"] if p["quantity"]>0 else extreme>=p["stop"]
                liq=f"liquidation_required:{symbol}" in self.ledger._state["halt_reasons"]
                protected=bool(self.ledger._state["halt_reasons"]) or self.ledger._state.get("exposure_reduction_required")
                if stopped or protected:
                    if liq and stopped:
                        self.ambiguities+=1
                    reason="liquidation_proxy" if liq else "stop" if stopped else "account_protection"
                    reference=extreme if liq or protected else p["stop"]
                    self.exit(symbol,t+MINUTE-1,reference,reason,symbol in funded,p["stop"] if liq and stopped else None)
        if self.ledger._state["positions"] and t+MINUTE<self.ledger.end:
            self.observe(t+MINUTE,data,"close")
        if (t+MINUTE)%HOUR==0:
            self.hourly.append({"time_us":t+MINUTE,"equity":str(self.ledger.equity)})
        if (t+MINUTE)%DAY_US==0:
            self.daily.append({"date":datetime.fromtimestamp(t//1_000_000,timezone.utc).strftime("%Y-%m-%d"),"equity":str(self.ledger.equity)})

    def finish(self,folder):
        self.event_stream.close()
        require(not self.ledger._state["positions"] and not self.ledger._state["orders"],"Unclosed terminal exposure")
        returns=[]
        previous=100.0
        years={}
        for row in self.daily:
            year=row["date"][:4]
            if year not in years:
                years[year]={"start_equity":previous}
            value=float(row["equity"])
            returns.append(value/previous-1 if previous>0 else 0.0)
            years[year]["end_equity"]=value
            previous=value
        for year in years.values():
            year["return"]=year["end_equity"]/year["start_equity"]-1 if year["start_equity"]>0 else None
        sd=statistics.stdev(returns) if len(returns)>1 else 0
        mean=statistics.mean(returns) if returns else 0
        downside=math.sqrt(sum(min(0,r)**2 for r in returns)/len(returns)) if returns else 0
        net=float(self.ledger.equity)/100-1
        cagr=(1+net)**(365/len(returns))-1 if net>-1 and returns else None
        pnl=[float(t["net_pnl"]) for t in self.trades]
        losses=-sum(min(0,v) for v in pnl)
        metrics={"account":self.name,"scope":"development_screening_under_proxy_rules","initial_equity":100,
                 "ending_equity":str(self.ledger.equity),"net_return":net,"cagr":cagr,"max_adverse_bound_drawdown":self.maxdd,
                 "daily_sharpe_zero_risk_free":math.sqrt(365)*mean/sd if sd else None,
                 "daily_sortino_zero_target":math.sqrt(365)*mean/downside if downside else None,
                 "calmar":cagr/self.maxdd if cagr is not None and self.maxdd else None,
                 "holding_limit_hours":self.holding_limit_hours,
                 "round_trips":len(pnl),"win_rate":sum(v>0 for v in pnl)/len(pnl) if pnl else None,
                 "profit_factor":sum(max(0,v) for v in pnl)/losses if losses else None,
                 "fees":str(self.ledger._state["fees"]),"funding":str(self.ledger._state["funding"]),
                 "gross_turnover":str(self.gross_turnover),"annual":years,"signals":dict(self.signals),"rejections":dict(self.rejects),
                 "liquidation_proxy_events":self.liquidations,"execution_ambiguities":self.ambiguities,
                 "halt_reasons":self.ledger._state["halt_reasons"],"formal_selection_eligible":False,"holdout_read":False}
        for name,records in (("trades",self.trades),("daily_equity",self.daily),("hourly_equity",self.hourly)):
            with (folder/(self.name+"_"+name+".csv")).open("x",newline="",encoding="utf-8-sig") as f:
                if records:
                    fields=list(dict.fromkeys(k for row in records for k in row))
                    writer=csv.DictWriter(f,fieldnames=fields)
                    writer.writeheader()
                    writer.writerows(records)
        preserve(folder/(self.name+"_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics


def run(root,variant=None):
    root=Path(root).resolve()
    require(variant in (None,"R002-v1","R003-v1","R004-v1","R005-v1","R006-v1"),"Unsupported research variant")
    plan=baseline=None
    account_class,hour_class=Account,Hour
    if variant == "R006-v1":
        from .research6 import load_sixth_plan,SixthAccount,SixthHour
        plan,baseline=load_sixth_plan(root)
        account_class,hour_class=SixthAccount,SixthHour
    elif variant == "R005-v1":
        from .research5 import load_fifth_plan,FifthAccount,FifthHour
        plan,baseline=load_fifth_plan(root)
        account_class,hour_class=FifthAccount,FifthHour
    elif variant == "R004-v1":
        from .research4 import load_fourth_plan,FourthAccount,FourthHour
        plan,baseline=load_fourth_plan(root)
        account_class,hour_class=FourthAccount,FourthHour
    elif variant == "R003-v1":
        from .research3 import load_third_plan,ThirdAccount,ThirdHour
        plan,baseline=load_third_plan(root)
        account_class,hour_class=ThirdAccount,ThirdHour
    elif variant is not None:
        from .variants import load_plan,VariantAccount,VariantHour
        plan,baseline=load_plan(root)
        account_class,hour_class=VariantAccount,VariantHour
    config=load(root/"configs/first_round.toml")
    policy_path=root/"configs/execution_policy.toml"
    policy=tomllib.loads(policy_path.read_text(encoding="utf-8"))
    require(policy["policy_id"]=="EXEC-001-v1" and policy["formal_selection_eligible"] is False and policy["holdout_locked"] is True,"Wrong screening policy")
    pointer=json.loads((root/"artifacts/data_manifest.json").read_text(encoding="utf-8"))
    manifest_path=under(root,pointer["full_manifest"],"artifacts/data/full_runs")
    require(sha(manifest_path.read_bytes())==pointer["full_manifest_sha256"],"Data manifest changed")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest["complete_development_coverage"] and not manifest["holdout_history_requested"],"Dataset unavailable")
    require(manifest["config_sha256"]==sha((root/"configs/first_round.toml").read_bytes()),"Config differs from prepared corpus")
    snapshot_path=root/"artifacts/rules/exchange_rules.json"
    snapshot=json.loads(snapshot_path.read_text(encoding="utf-8"))
    start,end=stamp_us(config["data"]["development_start"]),stamp_us(config["data"]["development_end"])
    rules,ticks,liq_fees={},{},{}
    for symbol in config["market"]["symbols"]:
        instrument=snapshot["symbols"][symbol]
        filters={f["filterType"]:f for f in instrument["filters"]}
        lot=filters["MARKET_LOT_SIZE"]
        tier=Tier("0",policy["notional_coverage_assumption"],policy["maintenance_rate_assumption"],"0","10")
        rules[symbol]=InstrumentRules(lot["stepSize"],lot["minQty"],lot["maxQty"],filters["MIN_NOTIONAL"]["notional"],
                                      (tier,),start,end,"EXEC-001-v1_COUNTERFACTUAL_NOT_HISTORICAL")
        ticks[symbol]=D(filters["PRICE_FILTER"]["tickSize"])
        liq_fees[symbol]=D(instrument["liquidationFee"])
    if baseline is not None:
        require(baseline["data_manifest_sha256"]==pointer["full_manifest_sha256"],"Baseline corpus differs")
        require(baseline["current_rule_snapshot_sha256"]==sha(snapshot_path.read_bytes()),"Baseline rule snapshot differs")
    run_id=(variant.split("-")[0]+"-" if variant else "SCREEN-")+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    folder=root/"artifacts/backtests"/run_id
    folder.mkdir(parents=True,exist_ok=False)
    sources={}
    extra_sources={"R003-v1":("research3.py",),"R004-v1":("research4.py",),"R005-v1":("research5.py","research4.py","factor_data.py"),"R006-v1":("research6.py","research5.py","research4.py","factor_data.py")}.get(variant,())
    for file in ("engine.py","signals.py","ledger.py","risk.py","margin.py","config.py")+(("variants.py",) if variant else ())+extra_sources:
        body=Path(__file__).with_name(file).read_bytes()
        preserve(folder/"source"/file,body)
        sources[file]=sha(body)
    for file in ("first_round.toml","data_policy.toml","execution_policy.toml"):
        preserve(folder/"source"/file,(root/"configs"/file).read_bytes())
    if variant:
        preserve(folder/"source/second_round.toml",(root/"configs/second_round.toml").read_bytes())
    if variant=="R003-v1":
        preserve(folder/"source/third_round.toml",(root/"configs/third_round.toml").read_bytes())
    if variant=="R004-v1":
        preserve(folder/"source/fourth_round.toml",(root/"configs/fourth_round.toml").read_bytes())
    if variant in ("R005-v1","R006-v1"):
        preserve(folder/"source/fifth_round.toml",(root/"configs/fifth_round.toml").read_bytes())
    if variant=="R006-v1":
        preserve(folder/"source/sixth_round.toml",(root/"configs/sixth_round.toml").read_bytes())
    metadata={"run_id":run_id,"phase":"development_screening","started_at_utc":datetime.now(timezone.utc).isoformat(),
              "config_sha256":sha((root/"configs/first_round.toml").read_bytes()),"execution_policy_sha256":sha(policy_path.read_bytes()),
              "data_manifest":pointer["full_manifest"],"data_manifest_sha256":pointer["full_manifest_sha256"],
              "current_rule_snapshot_sha256":sha(snapshot_path.read_bytes()),"source_sha256":sources,
              "strategy_accounts":plan["maximum_accounts"] if plan else 4,"parameter_search":False,"holdout_read":False,"formal_selection_eligible":False,
              "historical_margin_rules_verified":False,"historical_order_filters_verified":False}
    if variant:
        plan_file={"R002-v1":"second_round.toml","R003-v1":"third_round.toml","R004-v1":"fourth_round.toml","R005-v1":"fifth_round.toml","R006-v1":"sixth_round.toml"}[variant]
        metadata.update(research_design_id=variant,variant_plan_sha256=sha((root/"configs"/plan_file).read_bytes()),
                        baseline_run=plan["baseline_run"],baseline_summary_sha256=sha((root/"artifacts/backtests"/plan["baseline_run"]/"summary.json").read_bytes()),
                        maximum_accounts_this_design=plan["maximum_accounts"],single_change_per_mechanism=True)
    if variant in ("R005-v1","R006-v1"):
        fp=json.loads((root/"artifacts/factors/latest.json").read_text(encoding="utf-8"))
        preserve(folder/"source/factor_manifest.json",(root/fp["manifest"]).read_bytes())
        metadata.update(factor_manifest=fp["manifest"],factor_manifest_sha256=fp["manifest_sha256"],
            reference_run=plan["reference_run"],reference_summary_sha256=sha((root/"artifacts/backtests"/plan["reference_run"]/"summary.json").read_bytes()))
    if variant=="R006-v1":
        metadata.update(flow_filter_run=plan["flow_filter_run"],flow_filter_summary_sha256=sha((root/"artifacts/backtests"/plan["flow_filter_run"]/"summary.json").read_bytes()))
    preserve(folder/"started.json",json.dumps(metadata,ensure_ascii=False,indent=2).encode())
    account_options={"plan":plan} if variant else {}
    cases=plan["candidate_cases"] if variant in ("R003-v1","R004-v1","R005-v1","R006-v1") else ("trend","mean_reversion")
    accounts=[account_class(config,rules,ticks,liq_fees,s,c,folder,**account_options) for s in cases for c in ("base","stress")]
    print(json.dumps({"run_id":run_id,"accounts":[a.name for a in accounts],"mode":"proxy_rule_screening"}),flush=True)
    index={(r["symbol"],r["kind"],r["period"]):r for r in manifest["results"]}
    periods=sorted({r["period"] for r in manifest["results"]})
    require(periods[0]=="2022-11" and periods[-1]=="2025-12" and len(periods)==38,"Unexpected data partitions")
    with gzip.open(under(root,manifest["funding_valuation_path"],"data/validated"),"rt",encoding="utf-8") as f:
        funding=list(csv.DictReader(f))
    require(all(start<=int(row["event_time_us"])<end for row in funding),"Funding outside development")
    fund_index=0
    hours={s:(hour_class(root,s,plan) if variant in ("R005-v1","R006-v1") else hour_class()) for s in config["market"]["symbols"]}
    previous=None
    try:
        for period in periods:
            with ExitStack() as stack:
                keys=[(s,k) for s in config["market"]["symbols"] for k in ("klines","markPriceKlines")]
                readers=[]
                for symbol,kind in keys:
                    p=under(root,index[symbol,kind,period]["normalized_path"],"data/validated")
                    readers.append(csv.DictReader(stack.enter_context(gzip.open(p,"rt",encoding="utf-8"))))
                for bundle in itertools.zip_longest(*readers):
                    require(all(row is not None for row in bundle),"Unaligned source lengths")
                    t=int(bundle[0]["open_time_us"])
                    require(all(int(row["open_time_us"])==t for row in bundle) and (previous is None or t==previous+MINUTE),"Candle timing gap")
                    require(stamp_us(config["data"]["warmup_start"])<=t<end,"Attempt to read outside allowed history")
                    previous=t
                    data={s:{} for s in config["market"]["symbols"]}
                    for (symbol,kind),row in zip(keys,bundle):
                        data[symbol]["trade" if kind=="klines" else "mark"]=row
                    if t>=start:
                        funds=[]
                        while fund_index<len(funding) and int(funding[fund_index]["event_time_us"])<t+MINUTE:
                            require(int(funding[fund_index]["event_time_us"])>=t,"Skipped funding event")
                            funds.append(funding[fund_index])
                            fund_index+=1
                        for account in accounts:
                            account.minute(t,data,funds,t==end-MINUTE)
                        if variant in ("R005-v1","R006-v1"):
                            for fund in funds:hours[fund["symbol"]].settled_funding(fund)
                    features={}
                    closed=False
                    for symbol in hours:
                        closed,features[symbol]=hours[symbol].push(data[symbol]["trade"])
                    if closed:
                        for account in accounts:
                            account.decisions(t+MINUTE,features)
            print(json.dumps({"through":period,"equity":{a.name:str(a.ledger.equity) for a in accounts},
                              "trades":{a.name:len(a.trades) for a in accounts}},ensure_ascii=False),flush=True)
        require(fund_index==len(funding),"Funding coverage not consumed")
        metrics=[a.finish(folder) for a in accounts]
        report={**metadata,"completed_at_utc":datetime.now(timezone.utc).isoformat(),"status":"completed",
                "accounts":metrics,"cash_reference_ending_equity":100,"leverage_sensitivity_run":False,
                "selection":"not_permitted_under_unverified_rules","funding_events_consumed":fund_index}
        preserve(folder/"summary.json",json.dumps(report,ensure_ascii=False,indent=2).encode())
        return folder,report
    except Exception as exc:
        for account in accounts:
            account.event_stream.close()
            if hasattr(account,"gate_log"):
                account.gate_log.close()
            if hasattr(account,"sizing_log"):
                account.sizing_log.close()
        preserve(folder/"failed.json",json.dumps({**metadata,"status":"failed","error":f"{type(exc).__name__}: {exc}"},ensure_ascii=False,indent=2).encode())
        raise
