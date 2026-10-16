"""Two frozen pullback entry hypotheses; causal hourly confirmation only."""
from collections import Counter,deque
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D
from .engine import MINUTE,HOUR,sha
from .research4 import FourthHour
from .research7 import SeventhAccount
from .variants import VariantAccount

CASES=["ema20_reclaim","breakout_retest"]

def load_ninth_plan(root):
    plan=tomllib.loads((root/"configs/ninth_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R009-v1" and plan["parent_design"]=="R004-v1","Wrong research plan")
    require(plan["candidate_cases"]==CASES and plan["maximum_accounts"]==4 and plan["cost_scenarios"]==["base","stress"]
        and plan["holdout_locked"] and not plan["parameter_search"] and not plan["combine_changes"],"Fixed trial scope changed")
    require(plan["ema20_reclaim"]["ema_hours"]==20 and plan["breakout_retest"]["maximum_wait_hours"]==6
        and plan["breakout_retest"]["same_hour_retest_forbidden"],"Unsupported entry hypothesis")
    folder=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (folder/"superseded.json").exists(),"Superseded baseline")
    baseline=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["research_design_id"]=="R004-v1" and not baseline["holdout_read"],"Wrong baseline")
    for key,file in (("config_sha256","first_round.toml"),("execution_policy_sha256","execution_policy.toml"),("variant_plan_sha256","fourth_round.toml")):
        require(baseline[key]==sha((root/"configs"/file).read_bytes()),"Frozen baseline settings changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py","variants.py","research4.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Core/parent strategy changed")
    reference=json.loads((root/"artifacts/backtests"/plan["reference_run"]/"summary.json").read_text(encoding="utf-8"))
    require(reference["status"]=="completed" and reference["research_design_id"]=="R007-v1" and not reference["holdout_read"]
        and reference["data_manifest_sha256"]==baseline["data_manifest_sha256"],"Comparison scope differs")
    for name in ("research7.py","research5.py"):
        require(reference["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Shared execution instrumentation changed")
    return plan,baseline

def direction_ok(side,available,f):
    ctx=f.get("higher_4h")
    require(ctx is None or ctx["available_us"]<=available,"Future trend direction")
    if ctx is None:return False
    return (f["close"]>f["ema200"] and ctx["ema20"]>ctx["ema50"]) if side=="long" else (f["close"]<f["ema200"] and ctx["ema20"]<ctx["ema50"])

def reclaim_signal(previous,current):
    if previous is None:return None
    p,c=previous,current
    if p["close"]>p["ema200"] and p["close"]<=p["ema20"] and c["close"]>c["ema20"] and c["close"]>c["ema200"]:return "long"
    if p["close"]<p["ema200"] and p["close"]>=p["ema20"] and c["close"]<c["ema20"] and c["close"]<c["ema200"]:return "short"
    return None

class EMA20State:
    def __init__(self):self.seed=deque(maxlen=20);self.value=None

    def push(self,close):
        self.seed.append(close)
        if self.value is None and len(self.seed)==20:self.value=sum(self.seed)/20
        elif self.value is not None:self.value+=2/21*(close-self.value)
        return self.value

class RetestState:
    def __init__(self):self.pending=None;self.counts=Counter()

    def push(self,available,f,high,low):
        signal=context=None
        p=self.pending
        if p is not None:
            if available>p["expiry_us"]:
                self.counts["expired"]+=1;self.pending=None
            elif not direction_ok(p["side"],available,f):
                self.counts["cancelled_trend_state"]+=1;self.pending=None
            elif available>p["arm_available_us"]:
                touched=(low<=p["level"] and f["close"]>p["level"]) if p["side"]=="long" else (high>=p["level"] and f["close"]<p["level"])
                if touched:
                    signal=p["side"];context={**p,"confirmation_available_us":available,"confirmation_high":high,"confirmation_low":low,
                        "confirmation_close":f["close"],"wait_hours":(available-p["arm_available_us"])/HOUR}
                    self.pending=None;self.counts["confirmed"]+=1
        if signal is None:
            side=f["trend"]
            er=f.get("efficiency24")
            if side and er is not None and er>=.30 and direction_ok(side,available,f):
                if self.pending is None or self.pending["side"]!=side:
                    if self.pending is not None:self.counts["replaced_opposite"]+=1
                    self.pending={"side":side,"level":f["prior_high"] if side=="long" else f["prior_low"],
                        "arm_available_us":available,"expiry_us":available+6*HOUR,"arm_close":f["close"],
                        "arm_efficiency":er,"arm_ema200":f["ema200"],"arm_higher_4h":f["higher_4h"].copy()}
                    self.counts["armed"]+=1
        return signal,context

class NinthHour:
    def __init__(self):
        self.parent=FourthHour();self.ema=EMA20State();self.previous=None;self.last_available=None;self.retest=RetestState()

    def push(self,row):
        closed,feature=self.parent.push(row)
        if closed:
            available=int(row["available_time_us"])
            require(self.last_available is None or available==self.last_available+HOUR,"Nonconsecutive pullback hours")
            ema20=self.ema.push(float(row["close"]))
            if feature is not None:
                current={"available_us":available,"close":feature["close"],"ema20":ema20,"ema200":feature["ema200"]}
                previous=self.previous
                require(previous is None or previous["available_us"]==available-HOUR,"Stale previous close")
                reclaim=reclaim_signal(previous,current)
                h=self.parent.parent.hour
                retest,context=self.retest.push(available,feature,h.high,h.low)
                feature={**feature,"entry_signals":{"ema20_reclaim":reclaim,"breakout_retest":retest},
                    "entry_contexts":{"ema20_reclaim":{"previous":previous.copy() if previous else None,"current":current.copy()},
                        "breakout_retest":context}}
                self.previous=current
            self.last_available=available
        return closed,feature

class NinthAccount(SeventhAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        require(case in CASES,"Unregistered pullback hypothesis")
        self.case=case;self.only_entry_rule_changed=True
        self.sizing_counts=Counter();self.origin_counts=Counter();self.entry_origins={}
        self.sizing_log=(Path(folder)/(case+"_"+cost+"_sizing.jsonl")).open("x",encoding="utf-8")
        VariantAccount.__init__(self,config,rules,ticks,liquidation_fees,"trend",cost,folder,plan=plan,account_name=case+"_"+cost)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),json.dumps(config,ensure_ascii=False,indent=2).encode())

    def decisions(self,available,features):
        selected={s:({**f,"trend":f["entry_signals"][self.case]} if f is not None else None) for s,f in features.items()}
        VariantAccount.decisions(self,available,selected)
        for action in self.queue.get(available+MINUTE,[]):
            if action["kind"]!="entry":continue
            f=features[action["symbol"]];ctx=f["entry_contexts"][self.case]
            if self.case=="ema20_reclaim":
                require(ctx["current"]["available_us"]==available and ctx["previous"]["available_us"]==available-HOUR
                    and reclaim_signal(ctx["previous"],ctx["current"])==action["side"],"Invalid reclaim signal")
            else:
                require(ctx is not None and ctx["side"]==action["side"] and ctx["arm_available_us"]<available<=ctx["expiry_us"]
                    and ctx["confirmation_available_us"]==available,"Invalid retest timing")
            overlap=f["trend"]==action["side"]
            action.update(entry_mechanism=self.case,legacy_breakout_overlap=overlap,entry_context=ctx)
            self.origin_counts["candidates_inside_breakout" if overlap else "candidates_outside_breakout"]+=1
            self.gate_record({"gate":self.case,"symbol":action["symbol"],"side":action["side"],"signal_available_us":available,
                "scheduled_entry_us":available+MINUTE,"close":f["close"],"ema200":f["ema200"],"entry_context":ctx,
                "legacy_breakout_signal":f["trend"],"legacy_breakout_overlap":overlap})

    def entry(self,symbol,t,action,data):
        super().entry(symbol,t,action,data)
        if symbol in self.entry_origins and self.meta[symbol]["time"]==t:
            self.entry_origins[symbol]["context"]=action["entry_context"]

    def exit(self,symbol,*args,**kwargs):
        context=self.entry_origins[symbol]["context"]
        super().exit(symbol,*args,**kwargs)
        if self.case=="breakout_retest":
            t=self.trades[-1]
            t.update(retest_arm_time_us=context["arm_available_us"],retest_level=str(context["level"]),
                retest_arm_close=str(context["arm_close"]),retest_wait_hours=context["wait_hours"])
