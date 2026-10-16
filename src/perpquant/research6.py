"""One frozen independent aggressive-flow entry mechanism, R006-v1."""
from collections import Counter
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D
from .engine import MINUTE,HOUR,sha
from .research5 import FifthHour,FifthAccount,load_fifth_plan
from .variants import VariantAccount

def load_sixth_plan(root):
    plan=tomllib.loads((root/"configs/sixth_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R006-v1" and plan["parent_design"]=="R002-v1","Wrong sixth-round plan")
    require(plan["maximum_accounts"]==2 and plan["candidate_cases"]==["flow_cross"] and plan["cost_scenarios"]==["base","stress"]
        and not plan["parameter_search"] and not plan["combine_changes"] and plan["holdout_locked"],"Fixed research scope changed")
    require(D(plan["flow_cross"]["long_threshold"])==D(".55") and D(plan["flow_cross"]["short_threshold"])==D(".45")
        and not plan["flow_cross"]["original_breakout_required"],"Unsupported mechanism")
    _,baseline=load_fifth_plan(root)
    require(plan["baseline_run"]==baseline["run_id"],"Baseline mismatch")
    prior=json.loads((root/"artifacts/backtests"/plan["flow_filter_run"]/"summary.json").read_text(encoding="utf-8"))
    require(prior["status"]=="completed" and prior["research_design_id"]=="R005-v1" and not prior["holdout_read"]
        and prior["data_manifest_sha256"]==baseline["data_manifest_sha256"],"Invalid previous factor comparison")
    for name in ("research5.py","variants.py","research4.py","factor_data.py"):
        require(prior["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Shared feature/execution dependency changed")
    return plan,baseline

def flow_signal(previous,current,close,ema200,long_threshold=.55,short_threshold=.45):
    if previous is None or current is None:return None
    require(0<=previous<=1 and 0<=current<=1,"Invalid flow share")
    if previous<long_threshold<=current and close>ema200:return "long"
    if previous>short_threshold>=current and close<ema200:return "short"
    return None

class SixthHour(FifthHour):
    def __init__(self,root,symbol,plan):
        super().__init__(root,symbol,plan)
        self.previous_share=None
        self.previous_available=None

    def push(self,row):
        closed,feature=super().push(row)
        if closed:
            available=int(row["available_time_us"])
            require(self.previous_available is None or available==self.previous_available+HOUR,"Nonconsecutive flow hours")
            quote,buy=self.volume[available]
            share=buy/quote if quote>0 else None
            if feature is not None:
                feature={**feature,"legacy_breakout_signal":feature["trend"],"previous_flow_share":self.previous_share,
                    "previous_flow_available_us":self.previous_available,"flow_share":share,
                    "trend":flow_signal(self.previous_share,share,feature["close"],feature["ema200"])}
            self.previous_share,self.previous_available=share,available
        return closed,feature

class SixthAccount(FifthAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        require(case=="flow_cross","Only one new mechanism allowed")
        self.case=case
        self.only_entry_rule_changed=True
        self.sizing_counts=Counter()
        self.origin_counts=Counter()
        self.entry_origins={}
        self.sizing_log=(Path(folder)/(case+"_"+cost+"_sizing.jsonl")).open("x",encoding="utf-8")
        VariantAccount.__init__(self,config,rules,ticks,liquidation_fees,"trend",cost,folder,
            plan=plan,account_name=case+"_"+cost)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),json.dumps(config,ensure_ascii=False,indent=2).encode())

    def decisions(self,available,features):
        # Use original decisions/exits and 4h gate, bypass R005 additional filter.
        VariantAccount.decisions(self,available,features)
        due=available+MINUTE
        for action in self.queue.get(due,[]):
            if action["kind"]!="entry":continue
            feature=features[action["symbol"]]
            previous=feature["previous_flow_share"]
            current=feature["flow_share"]
            require(feature["previous_flow_available_us"]==available-HOUR,"Stale flow cross history")
            require(flow_signal(previous,current,feature["close"],feature["ema200"])==action["side"],"Invalid independent flow signal")
            overlap=feature["legacy_breakout_signal"]==action["side"]
            action.update(entry_mechanism="independent_flow_cross",legacy_breakout_overlap=overlap,
                previous_flow_share=previous,flow_share=current,previous_flow_available_us=feature["previous_flow_available_us"])
            self.origin_counts["candidates_inside_breakout" if overlap else "candidates_outside_breakout"]+=1
            self.gate_record({"gate":"independent_flow_cross","symbol":action["symbol"],"side":action["side"],
                "signal_available_us":available,"scheduled_entry_us":due,"previous_share_available_us":feature["previous_flow_available_us"],
                "previous_share":previous,"current_share":current,"close":feature["close"],"ema200":feature["ema200"],
                "legacy_breakout_signal":feature["legacy_breakout_signal"],"legacy_breakout_overlap":overlap})

    def entry(self,symbol,t,action,data):
        require(action.get("entry_mechanism")=="independent_flow_cross","Unbound flow entry")
        super().entry(symbol,t,action,data)
        if symbol in self.meta and self.meta[symbol]["time"]==t:
            overlap=action["legacy_breakout_overlap"]
            self.entry_origins[symbol]={"legacy_breakout_overlap":overlap,"signal_available_us":action["signal_available_us"]}
            self.origin_counts["filled_inside_breakout" if overlap else "filled_outside_breakout"]+=1

    def exit(self,symbol,*args,**kwargs):
        origin=self.entry_origins[symbol]
        super().exit(symbol,*args,**kwargs)
        self.entry_origins.pop(symbol)
        self.trades[-1].update(entry_mechanism="independent_flow_cross",legacy_breakout_overlap=origin["legacy_breakout_overlap"],
            entry_signal_available_us=origin["signal_available_us"])

    def finish(self,folder):
        metrics=super().finish(folder)
        metrics.update(entry_mechanism_replaced=True,original_breakout_required=False,entry_origins=dict(self.origin_counts))
        preserve(Path(folder)/(self.name+"_mechanism_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
