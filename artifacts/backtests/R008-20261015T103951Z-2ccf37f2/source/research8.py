"""Two fixed per-trade risk caps on exactly the R004 efficiency strategy."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import require,decimal_value as D,validate
from .engine import sha
from .research4 import FourthAccount,AmendmentLedger
from .variants import VariantAccount

def load_eighth_plan(root):
    plan=tomllib.loads((root/"configs/eighth_round.toml").read_text(encoding="utf-8"))
    require(plan["design_id"]=="R008-v1" and plan["parent_design"]=="R004-v1","Wrong risk experiment")
    require(plan["candidate_cases"]==["risk05","risk10"] and plan["maximum_accounts"]==4 and plan["cost_scenarios"]==["base","stress"]
        and not plan["parameter_search"] and not plan["combine_changes"] and plan["holdout_locked"],"Fixed experiment changed")
    require(plan["risk05"]["value"]=="0.005" and plan["risk10"]["value"]=="0.01"
        and not plan["diagnostics"]["automatic_default_risk_increase"],"Unsupported risk caps")
    folder=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (folder/"superseded.json").exists(),"Superseded baseline")
    baseline=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["research_design_id"]=="R004-v1" and not baseline["holdout_read"],"Invalid saved strategy")
    for key,file in (("config_sha256","first_round.toml"),("execution_policy_sha256","execution_policy.toml"),("variant_plan_sha256","fourth_round.toml")):
        require(baseline[key]==sha((root/"configs"/file).read_bytes()),"Frozen baseline settings changed")
    parent=tomllib.loads((root/"configs/fourth_round.toml").read_text(encoding="utf-8"))
    for key in ("lookback_hours","minimum_efficiency","formula","zero_path_policy"):
        require(plan["efficiency24"][key]==parent["efficiency24"][key],"Entry efficiency rule changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py","variants.py","research4.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Strategy/accounting implementation changed: "+name)
    return plan,baseline

def resolve_risk(config,case,plan):
    require(case in plan["candidate_cases"],"Unregistered risk case")
    result=deepcopy(config)
    result["risk"]["per_trade_risk_fraction"]=plan[case]["value"]
    require(D(result["risk"]["total_open_risk_fraction"])==D(".01")
        and D(result["risk"]["drawdown_halt_fraction"])==D(".20")
        and D(result["risk"]["daily_loss_pause_fraction"])==D(".03")
        and D(result["risk"]["baseline_leverage_cap"])==1
        and D(result["account"]["stop_equity_usd"])==50,"Account protection changed")
    return validate(result)

class EighthAccount(FourthAccount):
    def __init__(self,config,rules,ticks,liquidation_fees,case,cost,folder,*,plan):
        resolved=resolve_risk(config,case,plan)
        self.risk_case=case
        self.case="efficiency24"  # Exact original R004 branch, no trailing.
        self.only_entry_rule_changed=False
        self.trail={};self.amendment_counts=Counter();self.sizing_counts=Counter()
        self.sizing_log=(Path(folder)/(case+"_"+cost+"_sizing.jsonl")).open("x",encoding="utf-8")
        VariantAccount.__init__(self,resolved,rules,ticks,liquidation_fees,"trend",cost,folder,
            plan=plan,account_name=case+"_"+cost,ledger_class=AmendmentLedger)
        preserve(Path(folder)/"source"/(self.name+"_resolved_config.json"),json.dumps(resolved,ensure_ascii=False,indent=2).encode())

    def finish(self,folder):
        self.sizing_log.close()
        metrics=VariantAccount.finish(self,folder)
        metrics.update(case=self.risk_case,reference_strategy="R004_efficiency24",only_risk_budget_changed=True,
            per_trade_risk_fraction=self.config["risk"]["per_trade_risk_fraction"],
            total_open_risk_fraction=self.config["risk"]["total_open_risk_fraction"],
            sizing_diagnostics=dict(self.sizing_counts),trailing_diagnostics=dict(self.amendment_counts))
        preserve(Path(folder)/(self.name+"_risk_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
