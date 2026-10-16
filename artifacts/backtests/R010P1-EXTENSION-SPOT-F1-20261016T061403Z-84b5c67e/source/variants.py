"""Two prespecified R002 entry filters; inherit all R001 execution/accounting."""
from collections import Counter, deque
import json
from pathlib import Path
import tomllib
from .archive import preserve
from .config import decimal_value as D, require
from .engine import Account, HOUR, MINUTE, rounded, sha
from .signals import Hour


def load_plan(root):
    path=root/"configs/second_round.toml"
    plan=tomllib.loads(path.read_text(encoding="utf-8"))
    require(plan["design_id"]=="R002-v1" and plan["parent_design"]=="R001-v1", "Wrong variant design")
    require(plan["maximum_accounts"]==4 and plan["parameter_search"] is False and plan["holdout_locked"] is True,
            "Variant research scope changed")
    trend,mr=plan["trend_filter"],plan["mean_reversion_space"]
    require(trend["timeframe_hours"]==4 and trend["fast_ema_bars"]==20 and trend["slow_ema_bars"]==50
            and trend["partial_4h_bar_allowed"] is False and D(mr["minimum_net_reward_risk"])==1,
            "Only the prespecified two variants are implemented")
    baseline_path=root/"artifacts/backtests"/plan["baseline_run"]
    require(not (baseline_path/"superseded.json").exists(),"Baseline is superseded")
    baseline=json.loads((baseline_path/"summary.json").read_text(encoding="utf-8"))
    require(baseline["status"]=="completed" and baseline["holdout_read"] is False,"Wrong baseline state")
    require(baseline["config_sha256"]==sha((root/"configs/first_round.toml").read_bytes()),"Baseline config changed")
    require(baseline["execution_policy_sha256"]==sha((root/"configs/execution_policy.toml").read_bytes()),"Execution policy changed")
    for name in ("signals.py","ledger.py","risk.py","margin.py","config.py"):
        require(baseline["source_sha256"][name]==sha(Path(__file__).with_name(name).read_bytes()),"Baseline core changed: "+name)
    return plan,baseline


class FourHourContext:
    """EMA state can change only after four complete UTC-aligned hours."""
    def __init__(self):
        self.count=0
        self.seed=deque(maxlen=50)
        self.fast=self.slow=None
        self.latest=None
        self.last_hour=None

    def push(self,available,close):
        require(available%HOUR==0 and (self.last_hour is None or available==self.last_hour+HOUR),"Nonconsecutive closed hours")
        require(self.last_hour is not None or (available-HOUR)%(4*HOUR)==0,"First 4h candle must start on UTC boundary")
        self.last_hour=available
        self.count+=1
        if self.count<4:
            return
        require(available%(4*HOUR)==0,"Misaligned 4h availability")
        self.count=0
        self.seed.append(close)
        if self.fast is None and len(self.seed)>=20:
            self.fast=sum(list(self.seed)[-20:])/20
        elif self.fast is not None:
            self.fast+=2/21*(close-self.fast)
        if self.slow is None and len(self.seed)>=50:
            self.slow=sum(self.seed)/50
        elif self.slow is not None:
            self.slow+=2/51*(close-self.slow)
        if self.fast is not None and self.slow is not None:
            self.latest={"available_us":available,"ema20":self.fast,"ema50":self.slow}


class VariantHour:
    def __init__(self):
        self.hour=Hour()
        self.higher=FourHourContext()

    def push(self,row):
        closed,feature=self.hour.push(row)
        if closed:
            available=int(row["available_time_us"])
            self.higher.push(available,float(row["close"]))
            if feature is not None:
                context=self.higher.latest
                require(context is None or context["available_us"]<=available,"Future higher-timeframe context")
                feature={**feature,"higher_4h":context.copy() if context else None}
        return closed,feature


def room_budget(side,entry,target_exit,stop_exit,fee,funding_buffer,gap_buffer,minimum_ratio):
    entry,target,stop,fee,funding,gap,ratio=map(D,(entry,target_exit,stop_exit,fee,funding_buffer,gap_buffer,minimum_ratio))
    require(side in ("long","short") and min(entry,target,stop)>0 and min(fee,funding,gap)>=0 and ratio>0,"Invalid space-gate input")
    direction=1 if side=="long" else -1
    net_room=direction*(target-entry)-fee*(entry+target)-entry*funding
    stop_risk=direction*(entry-stop)+fee*(entry+stop)+entry*(funding+gap)
    require(stop_risk>0,"Invalid stop risk for space gate")
    return {"accepted":net_room>=ratio*stop_risk,"net_room_per_unit":net_room,"stop_risk_per_unit":stop_risk,
            "net_reward_risk":net_room/stop_risk,"minimum_ratio":ratio}


class VariantAccount(Account):
    def __init__(self,*args,plan,**kwargs):
        super().__init__(*args,**kwargs)
        self.plan=plan
        self.gate_counts=Counter()
        folder=Path(args[-1])
        self.gate_log=(folder/(self.name+"_gate_events.jsonl")).open("x",encoding="utf-8")

    def gate_record(self,record):
        self.gate_log.write(json.dumps(record,ensure_ascii=False,default=str)+"\n")

    def decisions(self,available,features):
        super().decisions(available,features)
        due=available+MINUTE
        actions=self.queue.get(due)
        if actions is None:
            return
        allowed=[]
        for action in actions:
            if action["kind"]!="entry":
                allowed.append(action)
                continue
            symbol,side=action["symbol"],action["side"]
            action={**action,"signal_available_us":available}
            feature=features[symbol]
            if self.strategy=="trend":
                context=feature.get("higher_4h")
                require(context is None or context["available_us"]<=available,"Future higher-timeframe entry")
                accepted=context is not None and (context["ema20"]>context["ema50"] if side=="long" else context["ema20"]<context["ema50"])
                reason="higher_4h_pass" if accepted else "higher_4h_missing" if context is None else "higher_4h_direction_mismatch"
                self.gate_counts[reason]+=1
                self.gate_record({"symbol":symbol,"side":side,"signal_available_us":available,"scheduled_entry_us":due,
                                  "gate":"closed_4h_ema20_ema50","accepted":accepted,"context":context})
                if not accepted:
                    self.rejects["R002:"+reason]+=1
                    continue
                action["higher_4h"]=context
            else:
                action["target_mean"]=format(feature["mean"],".10f")
            allowed.append(action)
        self.queue[due]=allowed

    def entry(self,symbol,t,action,data):
        require(t>=action["signal_available_us"]+MINUTE,"Variant cannot enter before the original delay")
        if self.strategy=="mean_reversion":
            side=action["side"]
            entry=self.execution_price(symbol,data[symbol]["trade"]["open"],side=="long")
            stop=rounded(action["stop"],self.ticks[symbol],side=="long")
            mark=D(data[symbol]["mark"]["open"])
            # Keep the original geometry rejection rather than converting a gap
            # across the stop into an invalid reward/risk estimate.
            if not (stop<min(entry,mark) if side=="long" else stop>max(entry,mark)):
                return super().entry(symbol,t,action,data)
            target=self.execution_price(symbol,action["target_mean"],side=="short")
            stop_exit=self.execution_price(symbol,stop,side=="short")
            assessment=room_budget(side,entry,target,stop_exit,self.ledger.fee,
                                   self.config["risk"]["funding_buffer_rate"],self.config["risk"]["gap_buffer_rate"],
                                   self.plan["mean_reversion_space"]["minimum_net_reward_risk"])
            reason="net_room_pass" if assessment["accepted"] else "net_room_below_stop_risk"
            self.gate_counts[reason]+=1
            self.gate_record({"symbol":symbol,"side":side,"signal_available_us":action["signal_available_us"],
                              "entry_time_us":t,"frozen_signal_mean":action["target_mean"],"entry_execution_price":entry,
                              "target_exit_estimate":target,"stop_exit_estimate":stop_exit,"assessment":assessment})
            if not assessment["accepted"]:
                self.rejects["R002:"+reason]+=1
                return
        super().entry(symbol,t,action,data)

    def finish(self,folder):
        self.gate_log.close()
        metrics=super().finish(folder)
        metrics.update(research_design=self.plan["design_id"],entry_filter="closed_4h_ema20_ema50" if self.strategy=="trend" else "net_reversion_room_ge_stop_risk",
                       entry_filter_counts=dict(self.gate_counts),only_entry_rule_changed=getattr(self,"only_entry_rule_changed",True))
        preserve(folder/(self.name+"_variant_metrics.json"),json.dumps(metrics,ensure_ascii=False,indent=2).encode())
        return metrics
