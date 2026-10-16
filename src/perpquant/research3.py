"""Two fixed trend hypotheses; no added risk and no search across parameters."""
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require
from .engine import sha
from .variants import VariantAccount,VariantHour


def load_third_plan(root):
    plan=tomllib.loads((root/"configs/third_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R003-v1" and plan["parent_design"]=="R002-v1","Wrong third-round plan")
    require(plan["candidate_cases"]==["breakout24","hold168"] and plan["maximum_accounts"]==4
            and plan["parameter_search"] is False and plan["combine_changes"] is False
            and plan["holdout_locked"] is True,"Prespecified research budget changed")
    require(plan["breakout24"]["value"]==24 and plan["breakout24"]["maximum_holding_hours"]==72
            and plan["hold168"]["value"]==168 and plan["hold168"]["breakout_hours"]==48,"Unsupported hypothesis")
    baseline_path=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (baseline_path/"superseded.json").exists(),"Superseded baseline")
    baseline=json.loads((baseline_path/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["research_design_id"]=="R002-v1"
            and baseline["holdout_read"] is False,"Invalid R002 baseline")
    require(baseline["config_sha256"]==sha((root/"configs/first_round.toml").read_bytes()),"Base capital/risk/strategy settings changed")
    require(baseline["execution_policy_sha256"]==sha((root/"configs/execution_policy.toml").read_bytes()),"Execution assumptions changed")
    require(baseline["variant_plan_sha256"]==sha((root/"configs/second_round.toml").read_bytes()),"Parent filter design changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Core changed: "+name)
    return plan,baseline


def case_config(config,case,plan):
    require(case in ("breakout24","hold168"),"Unregistered case")
    result=deepcopy(config)
    if case=="breakout24":
        result["strategies"]["trend"]["breakout_hours"]=plan[case]["value"]
    else:
        result["strategies"]["trend"]["max_holding_hours"]=plan[case]["value"]
    return result


class ThirdHour:
    """Keep R002 features, add a prior-24-hour channel without the current hour."""
    def __init__(self):
        self.parent=VariantHour()
        self.highs=deque(maxlen=24)
        self.lows=deque(maxlen=24)

    def push(self,row):
        closed,feature=self.parent.push(row)
        if closed:
            high=max(self.highs) if len(self.highs)==24 else None
            low=min(self.lows) if len(self.lows)==24 else None
            self.highs.append(self.parent.hour.high)
            self.lows.append(self.parent.hour.low)
            if feature is not None:
                close=feature["close"]
                signal=None
                if high is not None:
                    if close>high and close>feature["ema200"]:
                        signal="long"
                    elif close<low and close<feature["ema200"]:
                        signal="short"
                feature={**feature,"trend_24":signal,"prior24_high":high,"prior24_low":low}
        return closed,feature


class ThirdAccount(VariantAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        resolved=case_config(config,case,plan)
        self.case=case
        self.only_entry_rule_changed=case=="breakout24"
        super().__init__(resolved,rules,ticks,liquidation_fees,"trend",cost,folder,
                         plan=plan,account_name=case+"_"+cost)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),
                 json.dumps(resolved,ensure_ascii=False,indent=2).encode())

    def decisions(self,available,features):
        if self.case=="breakout24":
            features={s:({**f,"trend":f["trend_24"]} if f is not None else None) for s,f in features.items()}
        super().decisions(available,features)

    def finish(self,folder):
        metrics=super().finish(folder)
        metrics.update(case=self.case,parent_strategy="R002_trend",only_changed_field=self.plan[self.case]["changed_field"],
                       changed_value=self.plan[self.case]["value"])
        preserve(Path(folder)/(self.name+"_research_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
