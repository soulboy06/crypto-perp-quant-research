"""Fixed slower entries and pre-breakout volatility contraction, independent R007."""
from collections import Counter,deque
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D
from .engine import MINUTE,HOUR,sha
from .research5 import FifthAccount
from .variants import VariantAccount,VariantHour

CASES=["slow4h","compression_breakout"]

def load_seventh_plan(root):
    plan=tomllib.loads((root/"configs/seventh_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R007-v1" and plan["parent_design"]=="R002-v1","Wrong seventh-round plan")
    require(plan["maximum_accounts"]==4 and plan["candidate_cases"]==CASES and plan["cost_scenarios"]==["base","stress"]
        and not plan["parameter_search"] and not plan["combine_changes"] and plan["holdout_locked"],"Research scope changed")
    require(plan["slow4h"]["entry_timeframe_hours"]==4 and plan["slow4h"]["channel_bars"]==12
        and plan["compression_breakout"]["recent_hours"]==6 and plan["compression_breakout"]["background_hours"]==24
        and D(plan["compression_breakout"]["maximum_compression_ratio"])==D(".75")
        and D(plan["compression_breakout"]["minimum_expansion_ratio"])==D("1.5"),"Unsupported fixed mechanism")
    folder=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (folder/"superseded.json").exists(),"Superseded baseline")
    baseline=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["research_design_id"]=="R002-v1" and not baseline["holdout_read"],"Invalid baseline")
    for key,file in (("config_sha256","first_round.toml"),("execution_policy_sha256","execution_policy.toml"),("variant_plan_sha256","second_round.toml")):
        require(baseline[key]==sha((root/"configs"/file).read_bytes()),"Base settings changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Core changed")
    for run,design in ((plan["reference_run"],"R004-v1"),(plan["flow_filter_run"],"R005-v1")):
        saved=json.loads((root/"artifacts/backtests"/run/"summary.json").read_text(encoding="utf-8"))
        require(saved["status"]=="completed" and saved["research_design_id"]==design and not saved["holdout_read"]
            and saved["data_manifest_sha256"]==baseline["data_manifest_sha256"],"Comparison corpus differs")
    for name in ("variants.py","research4.py","research5.py"):
        require(saved["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Shared execution instrumentation changed")
    return plan,baseline

class SlowChannel:
    def __init__(self):
        self.highs=deque(maxlen=12);self.lows=deque(maxlen=12)
        self.count=0;self.last_available=None

    def push(self,available,high,low):
        require(available%HOUR==0 and (self.last_available is None or available==self.last_available+HOUR),"Nonconsecutive slower hours")
        if self.last_available is None:require((available-HOUR)%(4*HOUR)==0,"First four-hour block misaligned")
        self.last_available=available
        if self.count==0:self.high,self.low=high,low
        else:self.high,self.low=max(self.high,high),min(self.low,low)
        self.count+=1
        if self.count<4:return None
        require(available%(4*HOUR)==0,"Non-UTC four-hour close")
        before={"available_us":available,"prior_high":max(self.highs) if len(self.highs)==12 else None,
            "prior_low":min(self.lows) if len(self.lows)==12 else None,"completed_4h_high":self.high,"completed_4h_low":self.low}
        self.highs.append(self.high);self.lows.append(self.low);self.count=0
        return before

class CompressionState:
    def __init__(self):
        self.ranges=deque(maxlen=30);self.highs=deque(maxlen=6);self.lows=deque(maxlen=6)
        self.previous_close=None

    def push(self,high,low,close):
        tr=high-low if self.previous_close is None else max(high-low,abs(high-self.previous_close),abs(low-self.previous_close))
        values=list(self.ranges)
        recent=sum(values[-6:])/6 if len(values)==30 else None
        background=sum(values[:24])/24 if len(values)==30 else None
        result={"prior_recent_mean_tr":recent,"prior_background_mean_tr":background,"current_tr":tr,
            "compression_ratio":recent/background if background and recent is not None else None,
            "expansion_ratio":tr/recent if recent else None,
            "prior_high":max(self.highs) if len(self.highs)==6 else None,
            "prior_low":min(self.lows) if len(self.lows)==6 else None}
        self.ranges.append(tr);self.highs.append(high);self.lows.append(low);self.previous_close=close
        return result

def entry_signal(case,context,close,ema200):
    if context is None:return None
    high,low=context["prior_high"],context["prior_low"]
    if high is None or low is None:return None
    if case=="compression_breakout":
        contraction,expansion=context["compression_ratio"],context["expansion_ratio"]
        if contraction is None or expansion is None or contraction>.75 or expansion<1.5:return None
    else:require(case=="slow4h","Unknown mechanism")
    return "long" if close>high and close>ema200 else "short" if close<low and close<ema200 else None

class SeventhHour:
    def __init__(self):
        self.parent=VariantHour();self.slow=SlowChannel();self.compression=CompressionState()

    def push(self,row):
        closed,feature=self.parent.push(row)
        if closed:
            available=int(row["available_time_us"]);h=self.parent.hour
            slow=self.slow.push(available,h.high,h.low)
            compression=self.compression.push(h.high,h.low,float(row["close"]))
            if feature is not None:
                feature={**feature,"entry_contexts":{"slow4h":slow,"compression_breakout":compression},
                    "entry_signals":{case:entry_signal(case,ctx,feature["close"],feature["ema200"])
                        for case,ctx in (("slow4h",slow),("compression_breakout",compression))}}
        return closed,feature

class SeventhAccount(FifthAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        require(case in CASES,"Unregistered mechanism")
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
            f=features[action["symbol"]];context=f["entry_contexts"][self.case]
            require(entry_signal(self.case,context,f["close"],f["ema200"])==action["side"],"Signal differs from fixed entry rule")
            if self.case=="slow4h":require(available%(4*HOUR)==0 and context["available_us"]==available,"Partial 4h entry")
            overlap=f["trend"]==action["side"]
            action.update(entry_mechanism=self.case,legacy_breakout_overlap=overlap)
            self.origin_counts["candidates_inside_breakout" if overlap else "candidates_outside_breakout"]+=1
            self.gate_record({"gate":self.case,"symbol":action["symbol"],"side":action["side"],"signal_available_us":available,
                "scheduled_entry_us":available+MINUTE,"close":f["close"],"ema200":f["ema200"],"entry_context":context,
                "legacy_breakout_signal":f["trend"],"legacy_breakout_overlap":overlap})

    def entry(self,symbol,t,action,data):
        require(action.get("entry_mechanism")==self.case,"Unbound mechanism entry")
        super().entry(symbol,t,action,data)
        if symbol in self.meta and self.meta[symbol]["time"]==t:
            overlap=action["legacy_breakout_overlap"]
            self.entry_origins[symbol]={"overlap":overlap,"signal_available_us":action["signal_available_us"]}
            self.origin_counts["filled_inside_breakout" if overlap else "filled_outside_breakout"]+=1

    def exit(self,symbol,*args,**kwargs):
        origin=self.entry_origins[symbol]
        super().exit(symbol,*args,**kwargs)
        self.entry_origins.pop(symbol)
        self.trades[-1].update(entry_mechanism=self.case,legacy_breakout_overlap=origin["overlap"],entry_signal_available_us=origin["signal_available_us"])

    def finish(self,folder):
        metrics=super().finish(folder)
        metrics.update(entry_mechanism_replaced=True,entry_origins=dict(self.origin_counts),
            entry_timeframe_hours=4 if self.case=="slow4h" else 1,exits_remain_hourly=True)
        preserve(Path(folder)/(self.name+"_mechanism_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
