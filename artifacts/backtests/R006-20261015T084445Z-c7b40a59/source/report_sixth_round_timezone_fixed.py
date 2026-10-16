"""Report the single R006 hypothesis, including opportunities outside old breakout."""
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

def rows(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def halves(records):
    result={};previous=100.
    for r in records:
        key=r["date"][:4]+("H1" if r["date"][5:7]<="06" else "H2")
        if key not in result:result[key]={"start":previous}
        previous=float(r["equity"]);result[key]["end"]=previous
    return {k:v["end"]/v["start"]-1 for k,v in result.items()}

def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    plan=tomllib.loads((folder/"source/sixth_round.toml").read_text(encoding="utf-8"))
    require(report["status"]=="completed" and report["research_design_id"]=="R006-v1" and report["strategy_accounts"]==2
        and not report["holdout_read"] and not report["parameter_search"],"Wrong mechanism run")
    require(sha((folder/"source/sixth_round.toml").read_bytes())==report["variant_plan_sha256"],"Frozen plan changed")
    paths={k:ROOT/"artifacts/backtests"/report[k+"_run"] for k in ("baseline","reference","flow_filter")}
    old={}
    for key,path in paths.items():
        require(sha((path/"summary.json").read_bytes())==report[key+"_summary_sha256"],"Saved comparison changed")
        saved=json.loads((path/"summary.json").read_text(encoding="utf-8"))
        require(not saved["holdout_read"] and saved["data_manifest_sha256"]==report["data_manifest_sha256"],"Corpus differs")
        old[key]={a["account"]:a for a in saved["accounts"]}
    new={a["account"]:a for a in report["accounts"]}
    require(set(new)=={"flow_cross_base","flow_cross_stress"},"Extra or missing trial")
    spec=plan["diagnostics"];failed=[];comparisons=[]
    for cost in ("base","stress"):
        name="flow_cross_"+cost;a=new[name];ref=old["reference"][plan["reference_case"]+"_"+cost]
        trades=rows(folder/(name+"_trades.csv"))
        error=D(100)+sum((D(t["net_pnl"]) for t in trades),D(0))-D(a["ending_equity"])
        require(abs(error)<D("1e-18"),"Cash identity failed")
        require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=72*HOUR for t in trades),"Holding deadline failed")
        require(all(int(t["entry_time_us"])>=int(t["entry_signal_available_us"])+MINUTE for t in trades),"Entry before observable signal")
        daily=rows(folder/(name+"_daily_equity.csv"))
        rdaily=rows(paths["reference"]/(plan["reference_case"]+"_"+cost+"_daily_equity.csv"))
        require([r["date"] for r in daily]==[r["date"] for r in rdaily],"Daily mismatch")
        h,rh=halves(daily),halves(rdaily)
        require(len(h)==6 and h.keys()==rh.keys(),"Halfyear mismatch")
        halfrows=[{"segment":k,"return":h[k],"reference_return":rh[k],"improvement":h[k]-rh[k]} for k in h]
        improved=sum(r["improvement"]>0 for r in halfrows)
        uncertainty=paired_cagr_diagnostic([float(r["equity"]) for r in rdaily],[float(r["equity"]) for r in daily],
            block_days=spec["bootstrap_block_days"],resamples=spec["bootstrap_resamples"],seed=spec["bootstrap_seed"])
        if a["cagr"]-ref["cagr"]<float(spec["minimum_cagr_improvement_percentage_points_each_cost"])/100:failed.append(cost+": 年化提升不足1个百分点")
        if a["max_adverse_bound_drawdown"]>min(float(spec["absolute_maximum_drawdown"]),ref["max_adverse_bound_drawdown"]+float(spec["maximum_drawdown_increase_percentage_points_each_cost"])/100):failed.append(cost+": 回撤超过门槛")
        if a["calmar"] is None or a["calmar"]<ref["calmar"]:failed.append(cost+": 收益回撤比下降")
        if improved<spec["minimum_improved_halfyears_each_cost"]:failed.append(cost+": 改善半年不足4/6")
        if uncertainty["cagr_improvement_lower_95"]<=0:failed.append(cost+": 重采样未支持稳定改善")
        annual=[v["return"] for v in a["annual"].values()]
        if a["net_return"]<=0 or sum(v>0 for v in annual)<2 or min(annual)<-.1:failed.append(cost+": 未过原有年度收益门槛")
        if not trades or a["liquidation_proxy_events"] or a["execution_ambiguities"] or a["halt_reasons"]:failed.append(cost+": 交易或风险保护条件未过")
        with (folder/(name+"_gate_events.jsonl")).open(encoding="utf-8") as stream:gates=[json.loads(line) for line in stream]
        own=[g for g in gates if g["gate"]=="independent_flow_cross"]
        for g in own:
            require(g["previous_share_available_us"]==g["signal_available_us"]-HOUR and g["scheduled_entry_us"]==g["signal_available_us"]+MINUTE,"Invalid flow visibility")
            if g["side"]=="long":valid=g["previous_share"]<.55<=g["current_share"] and g["close"]>g["ema200"]
            else:valid=g["previous_share"]>.45>=g["current_share"] and g["close"]<g["ema200"]
            require(valid,"Signal does not match frozen crossing")
        origins={}
        for overlap,label in ((False,"outside_breakout"),(True,"inside_breakout")):
            group=[t for t in trades if t["legacy_breakout_overlap"]==str(overlap)]
            count=len(group)
            require(count==a["entry_origins"].get("filled_"+label,0),"Filled origin count mismatch")
            origins[label]={"trades":count,"net_pnl":float(sum((D(t["net_pnl"]) for t in group),D(0)))}
        require(sum(o["trades"] for o in origins.values())==len(trades),"Unclassified trade origin")
        with (folder/(name+"_sizing.jsonl")).open(encoding="utf-8") as stream:sizing=[json.loads(line) for line in stream]
        missing=sum("diagnostic_unavailable" in e["diagnostic"] for e in sizing)
        winners=sorted((float(t["net_pnl"]) for t in trades if D(t["net_pnl"])>0),reverse=True)
        symbolpnl={s:float(sum((D(t["net_pnl"]) for t in trades if t["symbol"]==s),D(0))) for s in {t["symbol"] for t in trades}}
        comparisons.append({"account":name,"metrics":a,"improvement_vs_reference":a["cagr"]-ref["cagr"],
            "improved_halfyears":improved,"halfyears":halfrows,"uncertainty_vs_reference":uncertainty,
            "cash_identity_error":str(error),"holding_deadline_respected":True,"signal_timing_audited":True,
            "audited_signal_candidates":len(own),"new_opportunities":origins,"unavailable_sizing_diagnostics":missing,
            "by_symbol":dict(Counter(t["symbol"] for t in trades)),"net_pnl_by_symbol":symbolpnl,
            "gross_pnl_at_execution_prices":float(sum((D(t["gross_pnl"]) for t in trades),D(0))),
            "last_trade_exit_utc":datetime.fromtimestamp(max(int(t["exit_time_us"]) for t in trades)/1_000_000,timezone.utc).isoformat() if trades else None,
            "top5_share_of_positive_trade_pnl":sum(winners[:5])/sum(winners) if winners else None,
            "net_pnl_less_largest_winner":float(a["ending_equity"])-100-(winners[0] if winners else 0)})
    passed=not failed
    decision="通过运行前固定的开发期改进门槛，可保留为待独立验证候选；仍不能实盘或宣称无过拟合。" if passed else "独立买卖压力入口未通过固定改进门槛，不替换研究基准。"
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    pct=lambda v:f"{100*v:+.2f}%"
    lines=["# 第六轮独立买卖压力机制 R006-v1","",decision,"",
        f"运行 `{report['run_id']}`，基础与压力两组共 {seconds:.1f} 秒。2023—2025 年已观察开发数据，2022年11—12月只热身；每组100USDT、BTC/ETH共享资金。2026保留数据未读取。", "",
        "## 扣费结果比较", "", "| 版本 | 基础年化 | 压力年化 | 基础回撤 | 压力回撤 | 基础／压力交易数 |","|---|---:|---:|---:|---:|---:|"]
    for label,group,prefix in (("R002突破原版",old["baseline"],"trend"),("R004效率过滤",old["reference"],plan["reference_case"]),("R005买卖压力过滤",old["flow_filter"],plan["flow_filter_case"]),("R006独立买卖压力",new,"flow_cross")):
        b,s=group[prefix+"_base"],group[prefix+"_stress"]
        lines.append(f"| {label} | {pct(b['cagr'])} | {pct(s['cagr'])} | {100*b['max_adverse_bound_drawdown']:.2f}% | {100*s['max_adverse_bound_drawdown']:.2f}% | {b['round_trips']}／{s['round_trips']} |")
    lines += ["", "均已计入手续费、不利价格调整、实际资金费和价格步长取整。回撤为既定分钟不利标记价路径的观测值，不是未来损失或所有真实路径上限。两种成本的成交和资金路径会不同，不可据结果接近就断言交易成本没有影响。", "",
        "## 固定的新入口", "",
        "相邻两个完整小时的主动买入USDT成交额占总USDT成交额的比例，从低于55%上穿到≥55%，且收盘价>EMA200时尝试做多；从高于45%下穿到≤45%，且收盘价<EMA200时尝试做空。仍须通过R002最近完整四小时EMA20/EMA50方向条件。只看相邻闭合小时，当前或前一小时无成交时不产生信号，历史重置。持续高于或低于阈值不逐小时重复触发。", "",
        "取消48小时价格突破这个入场要求，保留原EMA50退出、最初2ATR止损、72小时硬期限、账户保护、仓位及成本。信号可见后延迟60秒下单，同币先平后开、不加仓。没有叠加R004效率或R005其他过滤，55%/45%沿用上一轮而未重新搜索。它是一个新的入口机制，不能解释为只改变单个数值的因果对照。", "",
        "## 新机会是否真的存在", "",
        "以下按入场信号是否同时符合旧48小时突破方向分组；只是价格条件重合情况，不代表旧账户当时一定能成交。资金路径不同，不能把两组收益当成添加策略后的组合收益。", ""]
    for c in comparisons:
        o=c["new_opportunities"]
        lines.append(f"- {c['account']}：原突破之外 {o['outside_breakout']['trades']} 笔，净盈亏 {o['outside_breakout']['net_pnl']:+.4f} USDT；与原突破重合 {o['inside_breakout']['trades']} 笔，净盈亏 {o['inside_breakout']['net_pnl']:+.4f} USDT。候选与实际成交统计 {json.dumps(c['metrics']['entry_origins'])}。")
    lines += ["", "## 稳健性门槛", "",
        "相对R004效率过滤版，基础和压力都要求年化至少提高1个百分点、回撤最多多2个百分点且不超过20%、Calmar不下降、六个半年至少四个改善、配对14天循环区块重采样2000次（种子20261015）的改善区间下限>0，并保留原有收益与风险门槛。规则在运行前保存，不随结果更改。", "",
        "未通过条件："+("无。" if passed else "；".join(failed)+"。"), "",
        "| 账户 | 2023 | 2024 | 2025 | 改善半年 | 年化改善区间（百分点） |","|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        a=c["metrics"];u=c["uncertainty_vs_reference"]
        lines.append(f"| {c['account']} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {c['improved_halfyears']}/6 | [{100*u['cagr_improvement_lower_95']:+.2f}, {100*u['cagr_improvement_upper_95']:+.2f}] |")
    lines += ["", "| 账户 | 半年 | 本版收益 | 效率版收益 |","|---|---|---:|---:|"]
    for c in comparisons:
        for h in c["halfyears"]:lines.append(f"| {c['account']} | {h['segment']} | {pct(h['return'])} | {pct(h['reference_return'])} |")
    lines += ["", "分半年不重置账户、不重新训练。重采样区间仅为已知开发数据的条件诊断，没有校正多轮自适应选择，不能当作未来盈利概率。本轮未训练机器学习、未计算PBO或因子IC。", "",
        "## 集中度与执行诊断", ""]
    for c in comparisons:
        a=c["metrics"];share=c["top5_share_of_positive_trade_pnl"]
        share_text=f"{100*share:.2f}%" if share is not None else "无盈利单"
        lines.append(f"- {c['account']}：期末 {float(a['ending_equity']):.4f} USDT；分币交易 {json.dumps(c['by_symbol'])}；分币净盈亏 {json.dumps(c['net_pnl_by_symbol'])}；前五盈利笔占盈利总和 {share_text}；账面扣掉最大盈利单后净盈亏 {c['net_pnl_less_largest_winner']:+.4f} USDT（没有重新回放后续资金路径）。实际成交价口径扣手续费前毛盈亏 {c['gross_pnl_at_execution_prices']:+.4f} USDT；手续费 {float(a['fees']):.4f}，资金费净额 {float(a['funding']):+.4f}。拒单预算／门槛 {json.dumps(a['sizing_diagnostics'])}；诊断缺失 {c['unavailable_sizing_diagnostics']} 条；风险保护 {json.dumps(a['halt_reasons'])}；最后交易退出时间 {c['last_trade_exit_utc']}。")
    lines += ["", "如果账户触及永久回撤保护，之后只持有现金、不再开仓；年化仍按完整2023—2025权益序列计算。后续年度零收益代表停机，不代表策略在继续交易中稳定。20%是触发保护的阈值，分钟路径、价格变化和费用可使观测回撤略超20%，不会截断亏损。", ""]
    lines += ["", "## 避免过拟合的实际约束", "",
        "本轮只有一种新机制×两种成本，没有阈值扫描、追加版本、组合或提高杠杆与风险。全部结果保留；此前24个有效候选×成本配置，本轮2个，累计26个；另有首轮8个作废调试账户及原因。2023—2025已经多轮观察，再切分或重复回测不能把它变成独立样本。固定规则和有限次数只降低风险，不能证明没有过拟合。", "",
        "六项新增关键检查通过，包括阈值跨越、闭合小时、空量重置、无突破可入场、原四小时过滤/退出和资金守恒。两个账户另外核对逐笔净盈亏与期末现金、持仓期限、实际入场延迟、相邻小时可见性、候选数与成交来源。既有数据、风险、账本和费用模型未重做全量检查，旧版本成绩直接复用。", "",
        "沿用R005已验收小时量能表，没有新下载或读取2026。只读来源绑定及小型新增表校验，不再扫描原始数据校验和。真实历史订单过滤和保证金规则仍未核实，固定2.5%维持保证金及其他执行代理继续存在。初始100USDT、单笔风险0.5%、总敞口基准1倍、最大10倍和停止规则保持一致。正式选模资格仍为false，没有连接实盘。", ""]
    text="\n".join(lines)
    evidence={"run_id":report["run_id"],"research_design":"R006-v1","runtime_seconds":seconds,"decision":decision,
        "gate":{"prespecified_development_gate_passed":passed,"failed_conditions":failed,"formal_selection_eligible":False},
        "comparisons":comparisons,"retained_development_candidates":["flow_cross"] if passed else [],
        "research_baseline_retained":report["baseline_run"],"new_accounts":2,"new_checks_passed":6,
        "valid_candidate_cost_configurations_total":26,"superseded_debug_accounts_total":8,
        "holdout_read":False,"independent_validation":False,"selection_bias_corrected":False,
        "formal_selection_eligible":False,"background_tasks_remaining":0}
    preserve(folder/"comparison.md",text.encode())
    preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    for file in (Path(__file__),ROOT/"src/perpquant/uncertainty.py"):preserve(folder/"source"/file.name,file.read_bytes())
    (ROOT/"docs/第六轮独立信号结果.md").write_text(text,encoding="utf-8")
    pointer_path=ROOT/"artifacts/backtests/latest.json"
    if pointer_path.exists():
        body=pointer_path.read_bytes();preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R006-v1","scope":"development_screening_proxy_rules",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"research_baseline_retained":report["baseline_run"],
        "retained_development_candidates":evidence["retained_development_candidates"],"holdout_read":False,"formal_selection_eligible":False}
    pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:evidence[k] for k in ("run_id","runtime_seconds","decision","gate")},ensure_ascii=False))

if __name__=="__main__":main(sys.argv[1])
