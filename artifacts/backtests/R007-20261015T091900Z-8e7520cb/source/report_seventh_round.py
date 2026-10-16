"""Report both fixed R007 mechanisms and all four cost accounts."""
from collections import Counter
import csv
from datetime import datetime,timezone
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import tomllib
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.config import require
from perpquant.engine import sha,HOUR,MINUTE
from perpquant.quality import under
from perpquant.uncertainty import paired_cagr_diagnostic

LABELS={"slow4h":"四小时入场","compression_breakout":"收缩后扩张突破"}

def rows(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def halves(records):
    result={};previous=100.
    for row in records:
        key=row["date"][:4]+("H1" if row["date"][5:7]<="06" else "H2")
        if key not in result:result[key]={"start":previous}
        previous=float(row["equity"]);result[key]["end"]=previous
    return {k:v["end"]/v["start"]-1 for k,v in result.items()}

def audit_context(case,event):
    t=event["signal_available_us"];ctx=event["entry_context"]
    require(event["scheduled_entry_us"]==t+MINUTE,"Entry delay changed")
    if case=="slow4h":require(t%(4*HOUR)==0 and ctx["available_us"]==t,"Incomplete four-hour signal")
    else:
        require(ctx["prior_recent_mean_tr"]>0 and ctx["prior_background_mean_tr"]>0,"Nonpositive contraction history")
        require(abs(ctx["compression_ratio"]-ctx["prior_recent_mean_tr"]/ctx["prior_background_mean_tr"])<1e-12,"Compression ratio mismatch")
        require(abs(ctx["expansion_ratio"]-ctx["current_tr"]/ctx["prior_recent_mean_tr"])<1e-12,"Expansion ratio mismatch")
        require(ctx["compression_ratio"]<=.75 and ctx["expansion_ratio"]>=1.5,"Compression hypothesis not satisfied")
    close,ema=event["close"],event["ema200"]
    require(close>ctx["prior_high"] and close>ema if event["side"]=="long" else close<ctx["prior_low"] and close<ema,"Invalid directional breakout")

def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    plan=tomllib.loads((folder/"source/seventh_round.toml").read_text(encoding="utf-8"))
    require(report["status"]=="completed" and report["research_design_id"]=="R007-v1" and report["strategy_accounts"]==4
        and not report["holdout_read"] and not report["parameter_search"],"Wrong run")
    require(sha((folder/"source/seventh_round.toml").read_bytes())==report["variant_plan_sha256"],"Plan changed")
    old={};paths={}
    for key in ("baseline","reference","flow_filter"):
        path=ROOT/"artifacts/backtests"/report[key+"_run"];paths[key]=path
        require(sha((path/"summary.json").read_bytes())==report[key+"_summary_sha256"],"Saved comparison changed")
        saved=json.loads((path/"summary.json").read_text(encoding="utf-8"))
        require(not saved["holdout_read"] and saved["data_manifest_sha256"]==report["data_manifest_sha256"],"Comparison scope differs")
        old[key]={a["account"]:a for a in saved["accounts"]}
    new={a["account"]:a for a in report["accounts"]}
    require(set(new)=={case+"_"+cost for case in plan["candidate_cases"] for cost in plan["cost_scenarios"]},"Extra/missing trial")
    spec=plan["diagnostics"];comparisons=[];gates={}
    for case in plan["candidate_cases"]:
        failed=[]
        for cost in plan["cost_scenarios"]:
            name=case+"_"+cost;a=new[name];ref=old["reference"][plan["reference_case"]+"_"+cost]
            trades=rows(folder/(name+"_trades.csv"))
            error=D(100)+sum((D(t["net_pnl"]) for t in trades),D(0))-D(a["ending_equity"])
            require(abs(error)<D("1e-18"),"Cash identity failed")
            require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=72*HOUR for t in trades),"Holding deadline failed")
            require(all(int(t["entry_time_us"])>=int(t["entry_signal_available_us"])+MINUTE for t in trades),"Early entry")
            if case=="slow4h":require(all(int(t["entry_signal_available_us"])%(4*HOUR)==0 for t in trades),"Trade from partial four-hour signal")
            daily=rows(folder/(name+"_daily_equity.csv"))
            rdaily=rows(paths["reference"]/(plan["reference_case"]+"_"+cost+"_daily_equity.csv"))
            require([r["date"] for r in daily]==[r["date"] for r in rdaily],"Unaligned daily records")
            h,rh=halves(daily),halves(rdaily)
            require(len(h)==6 and h.keys()==rh.keys(),"Halfyear mismatch")
            halfrows=[{"segment":k,"return":h[k],"reference_return":rh[k],"improvement":h[k]-rh[k]} for k in h]
            improved=sum(v["improvement"]>0 for v in halfrows)
            uncertainty=paired_cagr_diagnostic([float(r["equity"]) for r in rdaily],[float(r["equity"]) for r in daily],
                block_days=spec["bootstrap_block_days"],resamples=spec["bootstrap_resamples"],seed=spec["bootstrap_seed"])
            if a["cagr"]-ref["cagr"]<float(spec["minimum_cagr_improvement_percentage_points_each_cost"])/100:failed.append(cost+": 年化提升不足1个百分点")
            if a["max_adverse_bound_drawdown"]>min(float(spec["absolute_maximum_drawdown"]),ref["max_adverse_bound_drawdown"]+float(spec["maximum_drawdown_increase_percentage_points_each_cost"])/100):failed.append(cost+": 回撤超过门槛")
            if a["calmar"] is None or a["calmar"]<ref["calmar"]:failed.append(cost+": 收益回撤比下降")
            if improved<spec["minimum_improved_halfyears_each_cost"]:failed.append(cost+": 改善半年不足4/6")
            if uncertainty["cagr_improvement_lower_95"]<=0:failed.append(cost+": 重采样未支持稳定改善")
            annual=[v["return"] for v in a["annual"].values()]
            if a["net_return"]<=0 or sum(v>0 for v in annual)<2 or min(annual)<-.1:failed.append(cost+": 未过原有年度收益门槛")
            if not trades or a["liquidation_proxy_events"] or a["execution_ambiguities"] or a["halt_reasons"]:failed.append(cost+": 交易或风险条件未过")
            with (folder/(name+"_gate_events.jsonl")).open(encoding="utf-8") as stream:events=[json.loads(line) for line in stream]
            own=[e for e in events if e["gate"]==case]
            for event in own:audit_context(case,event)
            require(len(own)==sum(a["entry_origins"].get(k,0) for k in ("candidates_inside_breakout","candidates_outside_breakout")),"Candidate counts mismatch")
            origins={}
            for overlap,label in ((False,"outside_breakout"),(True,"inside_breakout")):
                group=[t for t in trades if t["legacy_breakout_overlap"]==str(overlap)]
                require(len(group)==a["entry_origins"].get("filled_"+label,0),"Filled origins differ")
                origins[label]={"trades":len(group),"net_pnl":float(sum((D(t["net_pnl"]) for t in group),D(0)))}
            require(sum(g["trades"] for g in origins.values())==len(trades),"Unclassified trades")
            with (folder/(name+"_sizing.jsonl")).open(encoding="utf-8") as stream:sizing=[json.loads(line) for line in stream]
            missing=sum("diagnostic_unavailable" in e["diagnostic"] for e in sizing)
            winners=sorted((float(t["net_pnl"]) for t in trades if D(t["net_pnl"])>0),reverse=True)
            symbolpnl={s:float(sum((D(t["net_pnl"]) for t in trades if t["symbol"]==s),D(0))) for s in {t["symbol"] for t in trades}}
            comparisons.append({"account":name,"metrics":a,"improvement_vs_reference":a["cagr"]-ref["cagr"],
                "improved_halfyears":improved,"halfyears":halfrows,"uncertainty_vs_reference":uncertainty,
                "cash_identity_error":str(error),"holding_deadline_respected":True,"signal_timing_audited":True,
                "audited_candidates":len(own),"new_opportunities":origins,"unavailable_sizing_diagnostics":missing,
                "by_symbol":dict(Counter(t["symbol"] for t in trades)),"net_pnl_by_symbol":symbolpnl,
                "top5_share_of_positive_trade_pnl":sum(winners[:5])/sum(winners) if winners else None,
                "net_pnl_less_largest_winner":float(a["ending_equity"])-100-(winners[0] if winners else 0),
                "gross_pnl_at_execution_prices":float(sum((D(t["gross_pnl"]) for t in trades),D(0))),
                "last_exit_utc":datetime.fromtimestamp(max(int(t["exit_time_us"]) for t in trades)/1_000_000,timezone.utc).isoformat() if trades else None})
        gates[case]={"prespecified_development_gate_passed":not failed,"failed_conditions":failed,"formal_selection_eligible":False}
    qualified=[k for k,g in gates.items() if g["prespecified_development_gate_passed"]]
    decision="两项均未通过预设改进门槛，不替换研究基准。" if not qualified else "通过开发期改进门槛："+"、".join(LABELS[k] for k in qualified)+"；仍须独立验证，不连接实盘。"
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    pct=lambda x:f"{100*x:+.2f}%"
    lines=["# 第七轮四小时入场与收缩突破 R007-v1","",decision,"",
        f"运行 `{report['run_id']}`，四组共 {seconds:.1f} 秒。每组100USDT、BTC和ETH共享资金，2023—2025连续开发期，2022年11—12月热身；2026未读取。", "",
        "## 扣手续费、滑点、资金费后的结果", "", "| 版本 | 基础年化 | 压力年化 | 基础回撤 | 压力回撤 | 基础／压力交易数 |","|---|---:|---:|---:|---:|---:|"]
    pairs=[("R002原版",old["baseline"],"trend"),("R004效率过滤",old["reference"],plan["reference_case"]),("R005买卖压力过滤",old["flow_filter"],plan["flow_filter_case"])]+[(LABELS[k],new,k) for k in plan["candidate_cases"]]
    for label,group,prefix in pairs:
        b,s=group[prefix+"_base"],group[prefix+"_stress"]
        lines.append(f"| {label} | {pct(b['cagr'])} | {pct(s['cagr'])} | {100*b['max_adverse_bound_drawdown']:.2f}% | {100*s['max_adverse_bound_drawdown']:.2f}% | {b['round_trips']}／{s['round_trips']} |")
    lines += ["", "回撤为指定分钟不利标记价路径的观测值，不是所有真实路径或未来损失的上限。两种成本会改变成交与资金路径，不能把年化接近解释为成本无影响。", "",
        "## 两个固定方案的准确含义", "",
        "四小时入场：UTC 00/04/08/12/16/20收盘后，完整四小时收盘价突破此前12根完整四小时线的高/低点，并在原小时EMA200的相应一侧，才产生候选；仍通过原四小时EMA20/50方向过滤。通道不含当前四小时线。12根代表48小时历史，但与原每小时48根的取样边界不同。只有入场改为四小时，EMA200、EMA50退出、ATR14、2ATR止损与72小时期限仍保持原小时口径；不是整套系统转换到四小时图。", "",
        "收缩后扩张突破：信号小时之前30根完整小时，最近6根平均真实波幅≤前面不重叠24根平均真实波幅的75%；信号小时真实波幅≥前6根均值的1.5倍；收盘突破前6小时区间并在原EMA200正确一侧。仍通过原完整四小时方向过滤。真实波幅=max(高−低、|高−前收|、|低−前收|)。先算历史窗口再加入信号小时，避免用突破造成的放量波动反过来判定之前是否收缩；零历史波幅拒绝。", "",
        "两版分开跑，没有把四小时与收缩条件叠加，没有使用R004效率或R005买卖压力筛选。6/24、0.75/1.5及12根都只是事先选定的一组研究默认值，没有参数扫描，不声称这些阈值具有已证实的加密货币优势。信号收盘可见后再延迟60秒，费用、单笔/总风险和原退出不变。", "",
        "## 预设门槛", "",
        "相对R004效率版，两种成本都须年化至少增加1个百分点，回撤最多多2个百分点且≤20%，Calmar不下降，六个半年至少四个改善，配对14天循环区块重采样2000次（种子20261015）改善区间下限>0，并通过原年度收益及风险条件。以下原样报告，不随成绩调门槛。", ""]
    for case,g in gates.items():lines.append("- "+LABELS[case]+"："+("通过开发期门槛。" if g["prespecified_development_gate_passed"] else "未通过："+"；".join(g["failed_conditions"])+"。"))
    lines += ["", "| 账户 | 2023 | 2024 | 2025 | 改善半年 | 年化改善区间（百分点） |","|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        a=c["metrics"];u=c["uncertainty_vs_reference"]
        lines.append(f"| {c['account']} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {c['improved_halfyears']}/6 | [{100*u['cagr_improvement_lower_95']:+.2f}, {100*u['cagr_improvement_upper_95']:+.2f}] |")
    lines += ["", "| 账户 | 半年 | 本版收益 | 效率版收益 |","|---|---|---:|---:|"]
    for c in comparisons:
        for h in c["halfyears"]:lines.append(f"| {c['account']} | {h['segment']} | {pct(h['return'])} | {pct(h['reference_return'])} |")
    lines += ["", "重采样只诊断已观察开发数据，没有校正累计多轮选择偏差，也不是未来盈利概率。分期不重置本金、不重新训练。没有训练机器学习、计算PBO或因子IC。", "",
        "## 交易来源与集中度", "", "原突破之外/之内仅指信号价格条件重合，不表示旧账户一定能成交。旧账户和新账户的仓位占用不同，不能把分组净收益当作可直接叠加的收益。", ""]
    for c in comparisons:
        a=c["metrics"];o=c["new_opportunities"];share=c["top5_share_of_positive_trade_pnl"]
        share_text=f"{100*share:.2f}%" if share is not None else "无盈利单"
        lines.append(f"- {c['account']}：期末{float(a['ending_equity']):.4f}USDT；原突破之外{o['outside_breakout']['trades']}笔/净{o['outside_breakout']['net_pnl']:+.4f}，重合{o['inside_breakout']['trades']}笔/净{o['inside_breakout']['net_pnl']:+.4f}USDT。分币交易{json.dumps(c['by_symbol'])}；分币净盈亏{json.dumps(c['net_pnl_by_symbol'])}；前五笔占全部盈利交易净收益之和{share_text}；账面扣最大盈利单后净盈亏{c['net_pnl_less_largest_winner']:+.4f}USDT（未重放后续资金路径）。实际成交价口径扣手续费前毛盈亏{c['gross_pnl_at_execution_prices']:+.4f}，手续费{float(a['fees']):.4f}，资金费净额{float(a['funding']):+.4f}USDT。拒单诊断{json.dumps(a['sizing_diagnostics'])}，缺失{c['unavailable_sizing_diagnostics']}条；保护{json.dumps(a['halt_reasons'])}；最后退出UTC {c['last_exit_utc']}。")
    lines += ["", "如触及永久回撤保护，此后只持现金；年度零收益可能是停机，年化仍按完整三年计算。20%是保护触发阈值，实际观测可因价格及费用略超过，不会截断损失。", "",
        "## 约束过拟合与交付", "",
        "此前有效候选×成本配置26个，本轮4个，累计30个；另有首轮8个作废调试账户和原因。两项各一组参数、两种成本，全部结果保留，不追加阈值/组合/杠杆搜索。2023—2025已经多轮观察，再切分不能创造独立样本，固定方案只降低过拟合风险，不能证明泛化。", "",
        "七项新增关键检查通过：UTC四小时闭合、通道排除当前块、信号不带入下一小时、收缩窗口互不重叠且排除突破小时、多空波幅与跳空、门槛边界、原小时退出/方向过滤/60秒延迟和资金守恒。四个账户另核对现金、72小时持仓、逐笔信号时间、日志条件、实际成交来源和诊断缺失。旧结果直接复用，没有重跑数据验收或扫描原始校验和。", "",
        "仅使用原有分钟OHLC、标记价、实际资金费，不新下载或读取2026，不依赖新增量能表。初始100USDT、单笔风险0.5%、总风险1%、总敞口基准1倍、最高10倍保持不变。真实历史订单过滤/保证金档位仍未核实，沿用原代理及固定2.5%维持保证金假设。正式选模资格仍false，没有连接实盘。配置、代码、信号上下文、逐笔交易、事件和权益均在本次独立目录。", ""]
    text="\n".join(lines)
    evidence={"run_id":report["run_id"],"research_design":"R007-v1","runtime_seconds":seconds,"decision":decision,
        "gates":gates,"comparisons":comparisons,"retained_development_candidates":qualified,"research_baseline_retained":report["baseline_run"],
        "new_accounts":4,"new_checks_passed":7,"valid_candidate_cost_configurations_total":30,"superseded_debug_accounts_total":8,
        "holdout_read":False,"independent_validation":False,"selection_bias_corrected":False,"formal_selection_eligible":False,"background_tasks_remaining":0}
    preserve(folder/"comparison.md",text.encode());preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    for file in (Path(__file__),ROOT/"src/perpquant/uncertainty.py"):preserve(folder/"source"/file.name,file.read_bytes())
    (ROOT/"docs/第七轮入场机制结果.md").write_text(text,encoding="utf-8")
    pointer_path=ROOT/"artifacts/backtests/latest.json"
    if pointer_path.exists():
        body=pointer_path.read_bytes();preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R007-v1","scope":"development_screening_proxy_rules",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"research_baseline_retained":report["baseline_run"],
        "retained_development_candidates":qualified,"holdout_read":False,"formal_selection_eligible":False}
    pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:evidence[k] for k in ("run_id","runtime_seconds","decision","gates")},ensure_ascii=False))

if __name__=="__main__":main(sys.argv[1])
