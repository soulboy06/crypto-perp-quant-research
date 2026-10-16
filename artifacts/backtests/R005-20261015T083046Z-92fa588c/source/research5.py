"""Four prespecified independent factor gates on the saved R002 architecture."""
from collections import Counter,deque
import csv
import io
import json
import math
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D
from .engine import MINUTE,HOUR,sha,rounded
from .quality import under
from .variants import VariantAccount,VariantHour
from .research4 import FourthAccount,sizing_diagnostic

CASES=["volume_confirm","taker_pressure","close_location","funding_crowding"]

def factor_manifest(root,plan):
    pointer=json.loads((root/"artifacts/factors/latest.json").read_text(encoding="utf-8"))
    path=under(root,pointer["manifest"],"artifacts/factors")
    body=path.read_bytes()
    require(sha(body)==pointer["manifest_sha256"],"New factor manifest changed")
    result=json.loads(body)
    require(result["status"]=="completed" and not result["holdout_read"] and result["new_volume_fields_validated"],"New factor fields incomplete")
    require(result["plan_sha256"]==sha((root/"configs/fifth_round.toml").read_bytes()),"Factor preparation belongs to different plan")
    data=json.loads((root/"artifacts/data_manifest.json").read_text(encoding="utf-8"))
    require(result["source_manifest_sha256"]==data["full_manifest_sha256"],"Factor corpus differs")
    return result

def load_fifth_plan(root):
    plan=tomllib.loads((root/"configs/fifth_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R005-v1" and plan["parent_design"]=="R002-v1","Wrong factor plan")
    require(plan["candidate_cases"]==CASES and plan["maximum_accounts"]==8 and plan["holdout_locked"]
        and not plan["parameter_search"] and not plan["combine_changes"],"Factor research scope changed")
    require(plan["volume_confirm"]["lookback_hours"]==24 and D(plan["volume_confirm"]["minimum_ratio"])==1
        and D(plan["taker_pressure"]["long_minimum_buy_share"])==D(".55")
        and D(plan["taker_pressure"]["short_maximum_buy_share"])==D(".45")
        and D(plan["close_location"]["long_minimum_position"])==D(".75")
        and D(plan["close_location"]["short_maximum_position"])==D(".25")
        and D(plan["funding_crowding"]["maximum_directional_rate_8h"])==D(".0001")
        and plan["funding_crowding"]["knowledge_lag_seconds"]==60,"Unsupported single factor defaults")
    path=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (path/"superseded.json").exists(),"Superseded baseline")
    base=json.loads((path/"summary.json").read_text(encoding="utf-8"))
    require(base["status"]=="completed" and base["research_design_id"]=="R002-v1" and not base["holdout_read"],"Invalid baseline")
    for key,file in (("config_sha256","first_round.toml"),("execution_policy_sha256","execution_policy.toml"),("variant_plan_sha256","second_round.toml")):
        require(base[key]==sha((root/"configs"/file).read_bytes()),"Base settings changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py"):
        require(base["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Core changed: "+name)
    reference=json.loads((root/"artifacts/backtests"/plan["reference_run"]/"summary.json").read_text(encoding="utf-8"))
    require(reference["status"]=="completed" and not reference["holdout_read"] and reference["data_manifest_sha256"]==base["data_manifest_sha256"],"Invalid saved ER reference")
    for name in ("variants.py","research4.py"):
        require(reference["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Shared gate/diagnostic implementation changed")
    factor_manifest(root,plan)
    return plan,base

class FactorState:
    def __init__(self):
        self.volumes=deque(maxlen=24)
        self.funding_queue=deque()
        self.latest_funding=None

    def funding(self,row):
        t=int(row["event_time_us"])
        interval=float(row["interval_hours"])
        rate=float(row["rate"])
        require(math.isfinite(rate) and math.isfinite(interval) and interval>0,"Invalid settled funding input")
        require(not self.funding_queue or t>self.funding_queue[-1]["settlement_time_us"],"Funding order invalid")
        require(self.latest_funding is None or t>self.latest_funding["settlement_time_us"],"Stale funding event")
        self.funding_queue.append({"settlement_time_us":t,"known_time_us":t+MINUTE,"rate_normalized_8h":rate*8/interval})

    def close(self,available,high,low,close,quote,buy):
        require(all(math.isfinite(v) for v in (high,low,close,quote,buy)) and high>=close>=low and 0<=buy<=quote,"Invalid hour factor values")
        average=sum(self.volumes)/24 if len(self.volumes)==24 else None
        relative=quote/average if average is not None and average>0 else None
        self.volumes.append(quote)
        while self.funding_queue and self.funding_queue[0]["known_time_us"]<=available:
            self.latest_funding=self.funding_queue.popleft()
        funding=self.latest_funding
        require(funding is None or funding["known_time_us"]<=available,"Future funding feature")
        return {"relative_quote_volume":relative,"prior24_mean_quote_volume":average,
            "taker_buy_share":buy/quote if quote>0 else None,
            "close_location":(close-low)/(high-low) if high>low else None,
            "settled_funding":funding.copy() if funding else None}

class FifthHour:
    def __init__(self,root,symbol,plan):
        self.parent=VariantHour()
        self.state=FactorState()
        manifest=factor_manifest(root,plan)
        output=next(o for o in manifest["outputs"] if o["symbol"]==symbol)
        body=under(root,output["path"],"artifacts/factors").read_bytes()
        require(sha(body)==output["sha256"],"New hourly factor table changed")
        self.volume={int(r["available_time_us"]):(float(r["quote_volume"]),float(r["taker_buy_quote_volume"]))
            for r in csv.DictReader(io.StringIO(body.decode()))}
        require(len(self.volume)==output["hours"],"Factor timestamp duplicates")

    def settled_funding(self,row):self.state.funding(row)

    def push(self,row):
        closed,feature=self.parent.push(row)
        if closed:
            available=int(row["available_time_us"])
            require(available in self.volume,"Missing volume hour")
            quote,buy=self.volume[available]
            h=self.parent.hour
            factors=self.state.close(available,h.high,h.low,float(row["close"]),quote,buy)
            if feature is not None:feature={**feature,"factors":factors}
        return closed,feature

def factor_gate(case,side,available,factors,plan):
    require(case in CASES and side in ("long","short"),"Invalid factor gate")
    long=side=="long"
    if case=="volume_confirm":
        value=factors["relative_quote_volume"]
        return value is not None and value>=float(plan[case]["minimum_ratio"]),value
    if case=="taker_pressure":
        value=factors["taker_buy_share"]
        return value is not None and (value>=float(plan[case]["long_minimum_buy_share"]) if long else value<=float(plan[case]["short_maximum_buy_share"])),value
    if case=="close_location":
        value=factors["close_location"]
        return value is not None and (value>=float(plan[case]["long_minimum_position"]) if long else value<=float(plan[case]["short_maximum_position"])),value
    value=factors["settled_funding"]
    if value is None:return False,None
    require(value["known_time_us"]<=available,"Future settled rate")
    fresh=available-value["settlement_time_us"]<=plan[case]["maximum_age_hours"]*HOUR
    signed=value["rate_normalized_8h"]*(1 if long else -1)
    return fresh and signed<=float(plan[case]["maximum_directional_rate_8h"]),{"directional_rate_8h":signed,**value}

class FifthAccount(VariantAccount):
    # Reuse only read-only sizing instrumentation, no R004 strategy or trailing.
    record_sizing=FourthAccount.record_sizing

    def emit(self,kind,t,**values):
        diagnostic=None
        if kind=="reserve":
            try:diagnostic=sizing_diagnostic(self.ledger,values["symbol"],values["side"],D(values["reference_price"]),D(values["stop_price"]))
            except ValueError as exc:diagnostic={"diagnostic_unavailable":str(exc)}
        try:result=super().emit(kind,t,**values)
        except ValueError as exc:
            if diagnostic is not None:self.record_sizing(t,values,diagnostic,str(exc))
            raise
        if diagnostic is not None:self.record_sizing(t,values,diagnostic,None)
        return result

    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        require(case in CASES,"Unregistered factor")
        self.case=case
        self.only_entry_rule_changed=True
        self.sizing_counts=Counter()
        self.sizing_log=(Path(folder)/(case+"_"+cost+"_sizing.jsonl")).open("x",encoding="utf-8")
        super().__init__(config,rules,ticks,liquidation_fees,"trend",cost,folder,plan=plan,account_name=case+"_"+cost)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),json.dumps(config,ensure_ascii=False,indent=2).encode())

    def decisions(self,available,features):
        super().decisions(available,features)
        due=available+MINUTE
        if due not in self.queue:return
        allowed=[]
        for action in self.queue[due]:
            if action["kind"]=="entry":
                passed,value=factor_gate(self.case,action["side"],available,features[action["symbol"]]["factors"],self.plan)
                self.gate_counts["factor_pass" if passed else "factor_reject"]+=1
                self.gate_record({"gate":self.case,"symbol":action["symbol"],"side":action["side"],"signal_available_us":available,
                    "scheduled_entry_us":due,"factor_value":value,"accepted":passed})
                if not passed:
                    self.rejects["R005:"+self.case]+=1
                    continue
            allowed.append(action)
        self.queue[due]=allowed

    def finish(self,folder):
        self.sizing_log.close()
        metrics=super().finish(folder)
        metrics.update(case=self.case,parent_strategy="R002_trend",only_changed_field=self.plan[self.case]["changed_field"],
            changed_value=self.plan[self.case]["value"],sizing_diagnostics=dict(self.sizing_counts))
        preserve(Path(folder)/(self.name+"_research_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
