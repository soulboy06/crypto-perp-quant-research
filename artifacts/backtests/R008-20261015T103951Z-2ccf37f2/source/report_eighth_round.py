"""Risk/return sensitivity with financial reproduction controls, no alpha claims."""
from collections import Counter
import csv
from datetime import datetime,timezone
from decimal import Decimal as D
import json
from pathlib import Path
import statistics
import sys
import tomllib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.config import require
from perpquant.engine import sha,HOUR,MINUTE,stamp_us
from perpquant.quality import under
from perpquant.uncertainty import paired_cagr_diagnostic

def rows(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def halfyears(records):
    output={};previous=100.
    for row in records:
        key=row["date"][:4]+("H1" if row["date"][5:7]<="06" else "H2")
        if key not in output:output[key]={"start":previous}
        previous=float(row["equity"]);output[key]["end"]=previous
    return {k:v["end"]/v["start"]-1 for k,v in output.items()}

def utilization(trades,start,end):
    duration=(end-start)/1_000_000
    intervals=sorted((int(t["entry_time_us"]),int(t["exit_time_us"])) for t in trades)
    union=0;left=right=None
    for a,b in intervals:
        if right is None:left,right=a,b
        elif a<=right:right=max(right,b)
        else:union+=right-left;left,right=a,b
    if right is not None:union+=right-left
    sizes=[float(t["quantity"])*float(t["entry_price"]) for t in trades]
    hours=[(int(t["exit_time_us"])-int(t["entry_time_us"]))/HOUR for t in trades]
    proxy=sum(size*hour*3600 for size,hour in zip(sizes,hours))/duration
    return {"mean_entry_notional_usdt":statistics.mean(sizes) if sizes else 0,
        "median_entry_notional_usdt":statistics.median(sizes) if sizes else 0,
        "mean_holding_hours":statistics.mean(hours) if hours else 0,
        "time_average_entry_notional_proxy_usdt":proxy,"account_time_in_market_fraction":union/(end-start),
        "proxy_is_actual_margin_or_mark_to_market_exposure":False}

def trade_key(t):return tuple(t[k] for k in ("symbol","side","entry_time_us","exit_time_us","exit_reason"))

def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    plan=tomllib.loads((folder/"source/eighth_round.toml").read_text(encoding="utf-8"))
    require(report["status"]=="completed" and report["research_design_id"]=="R008-v1" and report["strategy_accounts"]==4
        and not report["holdout_read"] and not report["parameter_search"],"Wrong risk run")
    require(sha((folder/"source/eighth_round.toml").read_bytes())==report["variant_plan_sha256"],"Frozen risk plan changed")
    baseline_folder=ROOT/"artifacts/backtests"/report["baseline_run"]
    require(sha((baseline_folder/"summary.json").read_bytes())==report["baseline_summary_sha256"],"Old financial baseline changed")
    old={a["account"]:a for a in json.loads((baseline_folder/"summary.json").read_text(encoding="utf-8"))["accounts"]}
    accounts={a["account"]:a for a in report["accounts"]}
    require(set(accounts)=={case+"_"+cost for case in ("risk05","risk10") for cost in ("base","stress")},"Extra/missing risk trial")
    config=tomllib.loads((folder/"source/first_round.toml").read_text(encoding="utf-8"))
    start,end=stamp_us(config["data"]["development_start"]),stamp_us(config["data"]["development_end"])
    trade_cache={};daily_cache={};details=[];controls=[]
    for name,a in accounts.items():
        case,cost=name.rsplit("_",1)
        resolved=json.loads((folder/"source"/(name+"_resolved_config.json")).read_text(encoding="utf-8"))
        expected=json.loads(json.dumps(config));expected["risk"]["per_trade_risk_fraction"]=plan[case]["value"]
        require(resolved==expected,"Changed settings beyond per-trade risk")
        require(resolved["risk"]["total_open_risk_fraction"]=="0.01" and resolved["risk"]["drawdown_halt_fraction"]=="0.20","Shared risk/protection changed")
        trades=rows(folder/(name+"_trades.csv"));trade_cache[name]=trades
        daily=rows(folder/(name+"_daily_equity.csv"));daily_cache[name]=daily
        cash_error=D(100)+sum((D(t["net_pnl"]) for t in trades),D(0))-D(a["ending_equity"])
        require(abs(cash_error)<D("1e-18"),"Trade cash identity failed")
        require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=72*HOUR for t in trades),"Holding deadline failed")
        if case=="risk05":
            prior=old["efficiency24_"+cost]
            fields=("ending_equity","net_return","cagr","max_adverse_bound_drawdown","round_trips","fees","funding","annual",
                "win_rate","profit_factor","daily_sharpe_zero_risk_free","calmar","liquidation_proxy_events","execution_ambiguities","halt_reasons")
            require(all(a[k]==prior[k] for k in fields),"Control financial reproduction failed")
            require(trades==rows(baseline_folder/("efficiency24_"+cost+"_trades.csv")),"Control trade sequence changed")
            require(daily==rows(baseline_folder/("efficiency24_"+cost+"_daily_equity.csv")),"Control daily path changed")
            controls.append({"account":name,"saved_baseline":"efficiency24_"+cost,"exact_financial_trade_daily_reproduction":True})
        with (folder/(name+"_gate_events.jsonl")).open(encoding="utf-8") as stream:gate_events=[json.loads(line) for line in stream]
        for e in gate_events:
            if e["gate"]=="closed_4h_ema20_ema50":
                ctx=e["context"]
                require(ctx is None or ctx["available_us"]<=e["signal_available_us"],"Future direction context")
                require(e["scheduled_entry_us"]==e["signal_available_us"]+MINUTE,"Entry delay changed")
            else:require(e["gate"]=="efficiency24" and e["minimum"]=="0.30","Strategy filter changed")
        with (folder/(name+"_sizing.jsonl")).open(encoding="utf-8") as stream:sizing=[json.loads(line) for line in stream]
        rejected=sum(e["reserve_error"] is not None for e in sizing)
        require(rejected==a["sizing_diagnostics"].get("reserve_rejected",0),"Rejection counts differ")
        missing=sum("diagnostic_unavailable" in e["diagnostic"] for e in sizing)
        winners=sorted((float(t["net_pnl"]) for t in trades if D(t["net_pnl"])>0),reverse=True)
        flagged=[t for t in trades if t["funding_timing_ambiguous"]=="True"]
        symbolpnl={s:float(sum((D(t["net_pnl"]) for t in trades if t["symbol"]==s),D(0))) for s in {t["symbol"] for t in trades}}
        details.append({"account":name,"metrics":a,"cash_identity_error":str(cash_error),"holding_deadline_respected":True,
            "sizing_attempts":len(sizing),"reserve_rejections":rejected,"reserve_rejection_fraction":rejected/len(sizing) if sizing else None,
            "unavailable_sizing_diagnostics":missing,"utilization":utilization(trades,start,end),
            "by_symbol":dict(Counter(t["symbol"] for t in trades)),"net_pnl_by_symbol":symbolpnl,
            "top5_share_of_positive_trade_pnl":sum(winners[:5])/sum(winners) if winners else None,
            "net_pnl_less_largest_winner":float(a["ending_equity"])-100-(winners[0] if winners else 0),
            "gross_pnl_at_execution_prices":float(sum((D(t["gross_pnl"]) for t in trades),D(0))),
            "last_exit_utc":datetime.fromtimestamp(max(int(t["exit_time_us"]) for t in trades)/1_000_000,timezone.utc).isoformat() if trades else None,
            "funding_timing_flagged_trades":flagged})
    comparisons=[]
    for cost in ("base","stress"):
        low,high=accounts["risk05_"+cost],accounts["risk10_"+cost]
        ld,hd=daily_cache["risk05_"+cost],daily_cache["risk10_"+cost]
        require([r["date"] for r in ld]==[r["date"] for r in hd],"Paired days differ")
        lhalf,hhalf=halfyears(ld),halfyears(hd)
        uncertainty=paired_cagr_diagnostic([float(r["equity"]) for r in ld],[float(r["equity"]) for r in hd],
            block_days=plan["diagnostics"]["bootstrap_block_days"],resamples=plan["diagnostics"]["bootstrap_resamples"],seed=plan["diagnostics"]["bootstrap_seed"])
        ltr={trade_key(t):t for t in trade_cache["risk05_"+cost]};htr={trade_key(t):t for t in trade_cache["risk10_"+cost]}
        require(len(ltr)==len(trade_cache["risk05_"+cost]) and len(htr)==len(trade_cache["risk10_"+cost]),"Duplicate complete trade key")
        common=ltr.keys()&htr.keys();only_low=ltr.keys()-htr.keys();only_high=htr.keys()-ltr.keys()
        common_delta=sum((D(htr[k]["net_pnl"])-D(ltr[k]["net_pnl"]) for k in common),D(0))
        low_only=sum((D(ltr[k]["net_pnl"]) for k in only_low),D(0));high_only=sum((D(htr[k]["net_pnl"]) for k in only_high),D(0))
        require(abs(D(high["ending_equity"])-D(low["ending_equity"])-(common_delta+high_only-low_only))<D("1e-18"),"Trade grouping accounting failed")
        comparisons.append({"cost":cost,"cagr_increase":high["cagr"]-low["cagr"],"drawdown_increase":high["max_adverse_bound_drawdown"]-low["max_adverse_bound_drawdown"],
            "ending_equity_increase":float(D(high["ending_equity"])-D(low["ending_equity"])),
            "calmar_change":high["calmar"]-low["calmar"] if high["calmar"] is not None and low["calmar"] is not None else None,
            "halfyears":[{"segment":k,"risk05_return":lhalf[k],"risk10_return":hhalf[k]} for k in lhalf],
            "uncertainty":uncertainty,"trade_grouping":{"matched_complete_trades":len(common),"only_risk05":len(only_low),"only_risk10":len(only_high),
                "matched_net_pnl_delta":str(common_delta),"only_risk05_net_pnl":str(low_only),"only_risk10_net_pnl":str(high_only),
                "accounting_partition_not_causal_or_counterfactual_replay":True}})
    decision="已完成固定策略风险预算权衡模拟；默认单笔风险仍为0.5%，不自动采用1%。"
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    pct=lambda x:f"{100*x:+.2f}%"
    lines=["# 第八轮单笔风险与资金使用效率 R008-v1","",decision,"",
        f"运行 `{report['run_id']}`，四组回放 {seconds:.1f} 秒。效率过滤策略完全固定，只有单笔风险上限0.5%/1%不同；每组100USDT，BTC/ETH共享资金，2023—2025连续开发期，2022年11—12月热身，2026未读取。", "",
        "## 风险收益直接比较", "", "| 单笔风险 | 成本 | 复合年化 | 三年累计收益 | 回撤 | Calmar | 期末USDT | 交易数 |","|---|---|---:|---:|---:|---:|---:|---:|"]
    for name,a in accounts.items():
        risk="0.5%" if name.startswith("risk05") else "1%"
        lines.append(f"| {risk} | {name.rsplit('_',1)[1]} | {pct(a['cagr'])} | {pct(a['net_return'])} | {100*a['max_adverse_bound_drawdown']:.2f}% | {a['calmar']:.3f} | {float(a['ending_equity']):.4f} | {a['round_trips']} |")
    lines += ["", "回撤是既定分钟不利标记价路径的观测值，不是所有真实路径或未来损失上限。增加单笔风险属于风险敞口权衡，不是新因子或信号能力提升。", ""]
    for c in comparisons:
        lines.append(f"- {c['cost']}：1%相对0.5%年化变化{100*c['cagr_increase']:+.2f}个百分点；回撤变化{100*c['drawdown_increase']:+.2f}个百分点；期末现金变化{c['ending_equity_increase']:+.4f}USDT；Calmar变化{c['calmar_change']:+.3f}。")
    lines += ["", "## 账户总风险并未翻倍", "",
        "单笔1%仍受账户全部已开及待成交止损风险合计1%的约束；不能同时给BTC和ETH各分配完整1%。20%峰值回撤永久停机、3%日亏损暂停、50USDT本金停止线、20%自由现金预留、总敞口基准1倍和最高10倍均保持不变。实际到止损的损失仍受费用、跳价等影响，不是严格亏损保证。原配置文件和默认0.5%均未改，差异只写入本轮账户解析配置。", "",
        "原48小时突破、小时EMA200、完整四小时EMA20/50方向、24小时效率ER≥0.30、小时EMA50退出、最初2ATR止损、72小时期限及60秒延迟完全复用R004；没有改入场/退出、扫描风险档位或降低手续费。", "",
        "## 仓位、在场时间与拒单", "", "| 账户 | 平均入场名义USDT | 时均名义敞口代理USDT | 有持仓时间占比 | 平均持仓小时 | 预留失败／尝试 | 拒单率 |","|---|---:|---:|---:|---:|---:|---:|"]
    for d in details:
        u=d["utilization"]
        rate=f"{100*d['reserve_rejection_fraction']:.2f}%" if d["reserve_rejection_fraction"] is not None else "无尝试"
        lines.append(f"| {d['account']} | {u['mean_entry_notional_usdt']:.2f} | {u['time_average_entry_notional_proxy_usdt']:.2f} | {100*u['account_time_in_market_fraction']:.2f}% | {u['mean_holding_hours']:.2f} | {d['reserve_rejections']}/{d['sizing_attempts']} | {rate} |")
    lines += ["", "时均敞口代理=逐笔入场数量×入场价×持仓秒数之和÷整个开发期秒数，不是保证金占用，也不是真实逐分钟标记价敞口。有持仓时间占比按账户至少一笔持仓的时间并集合并，重叠时不重复计算。空仓并不代表应该强制交易。", "",
        "拒单率仅为预留失败/(成功+失败)。不同风险档位的持仓占用、资金路径、币种成交和可尝试机会会改变，不能把比率差简单归因于单个过滤器；订单失败标志可重叠，不能相加当独立次数。", "",
        "## 年度和分期", "", "| 账户 | 2023 | 2024 | 2025 | 代理强平 | 执行歧义 | 停机原因 |","|---|---:|---:|---:|---:|---:|---|"]
    for name,a in accounts.items():
        lines.append(f"| {name} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {a['liquidation_proxy_events']} | {a['execution_ambiguities']} | {json.dumps(a['halt_reasons'])} |")
    lines += ["", "| 成本 | 半年 | 0.5%风险收益 | 1%风险收益 |","|---|---|---:|---:|"]
    for c in comparisons:
        for h in c["halfyears"]:lines.append(f"| {c['cost']} | {h['segment']} | {pct(h['risk05_return'])} | {pct(h['risk10_return'])} |")
    lines += ["", "如触发永久保护，之后现金不再开仓；后续零收益不是持续交易下的稳定性。20%是保护触发值，路径和费用可使观测回撤超过它，不截断损失。资金费与止损同分钟时，既定回放先资金费再止损，分钟OHLC无法确认真实先后；上表执行歧义不能忽略。", "",
        "## 条件不确定性与成交路径", ""]
    for c in comparisons:
        u=c["uncertainty"];t=c["trade_grouping"]
        lines.append(f"- {c['cost']}：配对14天区块重采样2000次（种子20261015），1%相对0.5%年化差的条件95%分位区间[{100*u['cagr_improvement_lower_95']:+.2f}, {100*u['cagr_improvement_upper_95']:+.2f}]个百分点。完整进出时点与原因相同的交易{t['matched_complete_trades']}笔，净盈亏差{t['matched_net_pnl_delta']}USDT；仅0.5%档{t['only_risk05']}笔/净{t['only_risk05_net_pnl']}，仅1%档{t['only_risk10']}笔/净{t['only_risk10_net_pnl']}USDT。")
    lines += ["", "交易按币种、方向、入场时间、出场时间、退出原因配对；这一分组只做净收益差的账面恒等分解，不是风险扩大与新增机会的因果贡献，也没有重新回放反事实。重采样没有修正多年多轮选择偏差、账户动态风控或改变风险带来的路径依赖，不能当作独立验证或未来盈利概率。", "",
        "## 集中度与逐次拒单", ""]
    for d in details:
        a=d["metrics"];share=d["top5_share_of_positive_trade_pnl"]
        share_text=f"{100*share:.2f}%" if share is not None else "无盈利单"
        lines.append(f"- {d['account']}：分币交易{json.dumps(d['by_symbol'])}；分币净盈亏{json.dumps(d['net_pnl_by_symbol'])}；前五笔占全部盈利交易净收益总和{share_text}；账面扣最大盈利单后净盈亏{d['net_pnl_less_largest_winner']:+.4f}USDT（未重放资金路径）。实际成交价口径毛盈亏{d['gross_pnl_at_execution_prices']:+.4f}；手续费{float(a['fees']):.4f}，资金费净额{float(a['funding']):+.4f}USDT。拒单{json.dumps(a['sizing_diagnostics'])}，诊断缺失{d['unavailable_sizing_diagnostics']}条；资金费时点标记交易{len(d['funding_timing_flagged_trades'])}笔；最后退出UTC {d['last_exit_utc']}。")
    lines += ["", "## 复现、试验次数与边界", "",
        "0.5%两组与保存的R004效率账户逐笔交易、每日权益、年化、回撤、费用、资金费、年度结果及风险事件完全一致，验证旧策略复现。四项新增检查通过：仅单笔风险变化、总风险仍为1%、最低订单行为、原策略两种成本的决策与现金一致。四个账户另核对全部解析配置、逐笔资金守恒、72小时期限、过滤条件和延迟。", "",
        "本轮四个账户是两组复现对照＋两组新增1%风险模拟；累计有效账户执行34次、不同候选×成本配置32个，另有首轮8个作废调试账户。对照重跑不当作新策略或新增独立证据。2023—2025已经反复观察，不搜索更多风险档位或按结果自动采用1%。", "",
        "只读既有行情、标记价和资金费，不重新下载、不扫描原始校验和；2026仍锁定。基础单边0.06%手续费+0.02%不利价格调整，压力0.06%+0.06%，另计资金费和步长取整。历史最小订单和保证金档位尚未验证，沿用当前快照作用历史、固定2.5%维持保证金等代理。没有连接实盘，正式选模资格仍false。", ""]
    text="\n".join(lines)
    evidence={"run_id":report["run_id"],"research_design":"R008-v1","runtime_seconds":seconds,"decision":decision,
        "details":details,"comparisons":comparisons,"controls":controls,"new_checks_passed":4,
        "new_account_executions":4,"new_distinct_candidate_cost_configurations":2,"reproduction_accounts":2,
        "valid_account_executions_total":34,"distinct_valid_candidate_cost_configurations_total":32,"superseded_debug_accounts_total":8,
        "default_per_trade_risk_fraction":"0.005","default_risk_changed":False,"formal_selection_eligible":False,
        "independent_validation":False,"selection_bias_corrected":False,"holdout_read":False,"background_tasks_remaining":0}
    preserve(folder/"comparison.md",text.encode());preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    for file in (Path(__file__),ROOT/"src/perpquant/uncertainty.py"):preserve(folder/"source"/file.name,file.read_bytes())
    (ROOT/"docs/第八轮风险预算对比.md").write_text(text,encoding="utf-8")
    pointer_path=ROOT/"artifacts/backtests/latest.json"
    if pointer_path.exists():
        body=pointer_path.read_bytes();preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R008-v1","scope":"development_risk_budget_sensitivity",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"strategy_reference":report["baseline_run"]+":efficiency24",
        "default_per_trade_risk_fraction":"0.005","default_risk_changed":False,"holdout_read":False,"formal_selection_eligible":False}
    pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:evidence[k] for k in ("run_id","runtime_seconds","decision","controls","comparisons")},ensure_ascii=False))

if __name__=="__main__":main(sys.argv[1])
