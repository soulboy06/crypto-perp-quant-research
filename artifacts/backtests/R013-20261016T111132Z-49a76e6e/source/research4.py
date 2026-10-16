"""Separate efficiency gate and causal profit-trailing experiments, frozen R004."""
from collections import Counter,deque
from copy import deepcopy
from decimal import localcontext,ROUND_CEILING,ROUND_FLOOR
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D
from .engine import ReplayLedger,MINUTE,DAY_US,sha,rounded
from .ledger import encoded,digest
from .risk import EntryRequest,preview
from .variants import VariantAccount,VariantHour

def load_fourth_plan(root):
    plan=tomllib.loads((root/"configs/fourth_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R004-v1" and plan["parent_design"]=="R002-v1","Wrong research design")
    require(plan["candidate_cases"]==["efficiency24","profit_trail"] and plan["maximum_accounts"]==4
            and plan["cost_scenarios"]==["base","stress"] and not plan["parameter_search"]
            and not plan["combine_changes"] and plan["holdout_locked"],"Research budget changed")
    require(plan["efficiency24"]["lookback_hours"]==24 and D(plan["efficiency24"]["minimum_efficiency"])==D(".30")
            and D(plan["profit_trail"]["activation_initial_price_risk_multiple"])==1
            and D(plan["profit_trail"]["trail_atr_multiple"])==2
            and plan["profit_trail"]["effective_delay_seconds"]==60,"Unsupported experimental settings")
    path=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (path/"superseded.json").exists(),"Superseded baseline")
    baseline=json.loads((path/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["research_design_id"]=="R002-v1" and not baseline["holdout_read"],"Invalid baseline")
    for key,file in (("config_sha256","first_round.toml"),("execution_policy_sha256","execution_policy.toml"),("variant_plan_sha256","second_round.toml")):
        require(baseline[key]==sha((root/"configs"/file).read_bytes()),"Base settings changed: "+file)
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Accounting/signal core changed: "+name)
    return plan,baseline

class Efficiency:
    def __init__(self,hours=24):
        self.history=deque(maxlen=hours+1)
        self.hours=hours

    def push(self,close):
        self.history.append(float(close))
        if len(self.history)<self.hours+1:return None
        values=list(self.history)
        path=sum(abs(b-a) for a,b in zip(values,values[1:]))
        return abs(values[-1]-values[0])/path if path else 0.0

class FourthHour:
    def __init__(self):
        self.parent=VariantHour()
        self.efficiency=Efficiency()

    def push(self,row):
        closed,feature=self.parent.push(row)
        if closed:
            er=self.efficiency.push(float(row["close"]))
            if feature is not None:feature={**feature,"efficiency24":er}
        return closed,feature

class AmendmentLedger(ReplayLedger):
    """Replay-only atomic stop amendments; all financial events use original ledger."""
    def apply(self,event):
        if event.get("type")!="tighten_stop":return super().apply(event)
        require(not self._closed and self._connection is None,"Amendments require open in-memory replay ledger")
        payload=encoded(event)
        event=json.loads(payload)
        require(isinstance(event.get("id"),str) and event["id"],"Event id required")
        if event["id"] in self._seen:
            require(self._seen[event["id"]]==payload,"Conflicting duplicate event id")
            return self.snapshot()
        t=event["time_us"]
        require(type(t) is int and self.start<=t<self.end and t>=self._state["last_time_us"],"Invalid amendment time")
        require(event["policy_id"]=="R004-v1" and t>=event["signal_available_us"]+MINUTE,"Future stop amendment")
        with localcontext() as ctx:
            ctx.prec=40
            state=deepcopy(self._state)
            require(t//DAY_US==state["day"],"Missing midnight mark")
            symbol=event["symbol"]
            self._rules(symbol,t)
            self._fresh(state,t)
            p=state["positions"][symbol]
            stop,tick=D(event["stop_price"]),D(event["tick_size"])
            require(tick>0 and stop>0 and stop%tick==0,"Invalid stop/tick")
            require(D(event["previous_stop"])==p["stop"],"Stale stop amendment")
            require(stop>p["stop"] if p["quantity"]>0 else stop<p["stop"],"Stop amendment must tighten")
            require(stop<p["mark"] if p["quantity"]>0 else stop>p["mark"],"Amendment crossed current mark; exit instead")
            equity_before=self.equity_of(state)
            p["stop"]=stop
            state["last_time_us"]=t
            self._protect(state,t)
            require(self.equity_of(state)==equity_before,"Stop amendment changed equity")
            state["sequence"]+=1
            chain=digest({"previous":self._chain,"event":event,"state":digest(state)})
            self._state,self._chain=state,chain
            self._seen[event["id"]]=payload
        return self.snapshot()

def sizing_diagnostic(ledger,symbol,side,price,stop):
    """Mirror original reserve preview without modifying quantity or original reasons."""
    state=ledger._state
    rule=ledger.rules[symbol]
    equity=ledger.equity_of(state)
    gross,risk,pending,pending_risk=ledger._risk_values(state)
    before_pending=state["cash"]+sum((o["reserved"] for o in state["orders"].values()),D(0))
    request=EntryRequest(equity,min(before_pending,equity),price,stop,rule.step,rule.min_quantity,
                         rule.min_notional,side=side,existing_gross=gross,pending_gross=pending,
                         existing_stop_risk=risk,pending_stop_risk=pending_risk)
    result=preview(ledger.config,request,ledger.cap,ledger.cost)
    # Reconstruct rooms only to expose why a rejected preview rounded below filters.
    c,r=ledger.config["costs"][ledger.cost],ledger.config["risk"]
    cost=D(c["taker_fee_rate"])+D(c["adverse_price_rate"])
    risk_rate=abs(price-stop)/price+2*cost+D(r["gap_buffer_rate"])+D(r["funding_buffer_rate"])
    budget=min(equity*D(r["per_trade_risk_fraction"]),equity*D(r["total_open_risk_fraction"])-risk-pending_risk)
    margin_rate=1/D(r["venue_leverage"])+2*cost+D(r["funding_buffer_rate"])
    rooms={"stop_risk":budget/risk_rate,
        "account_leverage":(ledger.cap*(equity-pending*cost)-gross-pending)/(1+ledger.cap*cost),
        "symbol_leverage":D(r["max_symbol_leverage"])*(equity-pending*cost)/(1+D(r["max_symbol_leverage"])*cost),
        "collateral":(min(before_pending,equity)-pending*margin_rate-equity*D(r["free_collateral_reserve_fraction"]))/margin_rate}
    maximum=max(D(0),min(rooms.values()))
    quantity=(maximum/price/rule.step).to_integral_value(rounding=ROUND_FLOOR)*rule.step
    failures=[]
    if quantity<=0:failures.append("rounded_quantity_zero")
    if quantity<rule.min_quantity:failures.append("below_min_quantity")
    if quantity*price<rule.min_notional:failures.append("below_min_notional")
    require(result.get("binding_constraint")==min(rooms,key=rooms.get),"Diagnostic differs from original preview")
    if result["status"]=="preview_only":require(quantity==result["quantity"],"Diagnostic quantity mismatch")
    return {"preview_status":result["status"],"binding_constraint":result["binding_constraint"],
        "notional_rooms":rooms,"rounded_candidate_quantity":quantity,"rounded_candidate_notional":quantity*price,
        "minimum_quantity":rule.min_quantity,"minimum_notional":rule.min_notional,"filter_failures":failures,
        "equity":equity,"free_cash":state["cash"],"open_positions":list(state["positions"]),
        "failure_flags_not_mutually_exclusive":True,"original_preview_symbol_exposure_defaults_retained":True}

class FourthAccount(VariantAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        require(case in plan["candidate_cases"],"Unregistered candidate")
        self.case=case
        self.only_entry_rule_changed=case=="efficiency24"
        self.trail={}
        self.amendment_counts=Counter()
        self.sizing_counts=Counter()
        self.sizing_log=(Path(folder)/(case+"_"+cost+"_sizing.jsonl")).open("x",encoding="utf-8")
        super().__init__(config,rules,ticks,liquidation_fees,"trend",cost,folder,plan=plan,
                         account_name=case+"_"+cost,ledger_class=AmendmentLedger)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),json.dumps(config,ensure_ascii=False,indent=2).encode())

    def emit(self,kind,t,**values):
        diagnostic=None
        if kind=="reserve":
            try:
                diagnostic=sizing_diagnostic(self.ledger,values["symbol"],values["side"],D(values["reference_price"]),D(values["stop_price"]))
            except ValueError as exc:
                diagnostic={"diagnostic_unavailable":str(exc)}
        try:
            result=super().emit(kind,t,**values)
        except ValueError as exc:
            if diagnostic is not None:self.record_sizing(t,values,diagnostic,str(exc))
            raise
        if diagnostic is not None:self.record_sizing(t,values,diagnostic,None)
        return result

    def record_sizing(self,t,values,diagnostic,error):
        if error:
            self.sizing_counts["reserve_rejected"]+=1
            self.sizing_counts["rejected_binding:"+diagnostic.get("binding_constraint","unavailable")]+=1
            for reason in diagnostic.get("filter_failures",[]):self.sizing_counts["rejected_filter:"+reason]+=1
        else:self.sizing_counts["reserve_passed"]+=1
        self.sizing_log.write(json.dumps({"time_us":t,"symbol":values["symbol"],"side":values["side"],
            "reserve_error":error,"diagnostic":diagnostic},ensure_ascii=False,default=str)+"\n")

    def entry(self,symbol,t,action,data):
        super().entry(symbol,t,action,data)
        if self.case=="profit_trail" and symbol in self.meta and self.meta[symbol]["time"]==t:
            p=self.ledger._state["positions"][symbol]
            self.trail[symbol]={"entry_time_us":t,"initial_risk":abs(p["entry"]-p["stop"]),
                                "anchor":p["entry"],"active":False}

    def exit(self,symbol,*args,**kwargs):
        super().exit(symbol,*args,**kwargs)
        self.trail.pop(symbol,None)

    def decisions(self,available,features):
        super().decisions(available,features)
        due=available+MINUTE
        if due not in self.queue:return
        if self.case=="efficiency24":
            allowed=[]
            for action in self.queue[due]:
                if action["kind"]=="entry":
                    er=features[action["symbol"]].get("efficiency24")
                    passed=er is not None and er>=float(self.plan[self.case]["minimum_efficiency"])
                    self.gate_counts["efficiency_pass" if passed else "efficiency_reject"]+=1
                    self.gate_record({"gate":"efficiency24","symbol":action["symbol"],"signal_available_us":available,
                        "efficiency":er,"minimum":self.plan[self.case]["minimum_efficiency"],"accepted":passed})
                    if not passed:
                        self.rejects["R004:efficiency_below_threshold"]+=1
                        continue
                allowed.append(action)
            self.queue[due]=allowed
            return
        exiting={a["symbol"] for a in self.queue[due] if a["kind"]=="exit"}
        for symbol,p in self.ledger._state["positions"].items():
            f=features[symbol]
            if f is None or symbol in exiting:continue
            state=self.trail[symbol]
            close,atr=D(str(f["close"])),D(str(f["atr"]))
            long=p["quantity"]>0
            state["anchor"]=max(state["anchor"],close) if long else min(state["anchor"],close)
            favorable=(close-p["entry"]) if long else (p["entry"]-close)
            if favorable>=state["initial_risk"]*D(self.plan[self.case]["activation_initial_price_risk_multiple"]):
                if not state["active"]:self.amendment_counts["activated_positions"]+=1
                state["active"]=True
            if not state["active"]:continue
            distance=D(self.plan[self.case]["trail_atr_multiple"])*atr
            stop=rounded(state["anchor"]-distance if long else state["anchor"]+distance,self.ticks[symbol],not long)
            if stop>0 and (stop>p["stop"] if long else stop<p["stop"]):
                self.queue[due].append({"kind":"tighten_stop","symbol":symbol,"entry_time":self.meta[symbol]["time"],
                    "stop":str(stop),"signal_available_us":available,"anchor":str(state["anchor"]),"atr":str(atr)})

    def process_adjustments(self,t,phase,data,due,funded):
        for action in due:
            if action["kind"]!="tighten_stop":continue
            symbol=action["symbol"]
            if symbol not in self.meta or self.meta[symbol]["time"]!=action["entry_time"]:continue
            require(t>=action["signal_available_us"]+MINUTE,"Stop cannot use future closed hour")
            p=self.ledger._state["positions"][symbol]
            stop=D(action["stop"])
            long=p["quantity"]>0
            if not (stop>p["stop"] if long else stop<p["stop"]):continue
            opening=D(data[symbol]["trade"]["open"])
            if (opening<=stop if long else opening>=stop):
                self.amendment_counts["crossed_at_effective_open"]+=1
                self.exit(symbol,phase,opening,"trailing_gap_stop",symbol in funded)
                continue
            if (p["mark"]<=stop if long else p["mark"]>=stop):
                # Stop triggers use trade prices in this research execution model;
                # a new stop beyond the mark is deferred rather than filled at a
                # better synthetic price or moved retroactively.
                self.amendment_counts["deferred_mark_dislocation"]+=1
                continue
            self.emit("tighten_stop",phase,symbol=symbol,stop_price=str(stop),previous_stop=str(p["stop"]),
                tick_size=str(self.ticks[symbol]),signal_available_us=action["signal_available_us"],policy_id="R004-v1")
            self.amendment_counts["effective_amendments"]+=1

    def finish(self,folder):
        self.sizing_log.close()
        metrics=super().finish(folder)
        metrics.update(case=self.case,parent_strategy="R002_trend",only_changed_field=self.plan[self.case]["changed_field"],
            changed_value=self.plan[self.case]["value"],sizing_diagnostics=dict(self.sizing_counts),
            trailing_diagnostics=dict(self.amendment_counts))
        preserve(Path(folder)/(self.name+"_research_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
