"""All four factor hypotheses compared with R002 and saved R004 ER reference."""
from collections import Counter
import csv
from datetime import datetime
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

LABELS={"volume_confirm":"放量确认","taker_pressure":"主动买卖压力","close_location":"收盘位置","funding_crowding":"资金费拥挤度"}

def rows(path):
    with path.open(encoding="utf-8-sig",newline="") as f:return list(csv.DictReader(f))

def halves(records):
    result={};previous=100.
    for r in records:
        k=r["date"][:4]+("H1" if r["date"][5:7]<="06" else "H2")
        if k not in result:result[k]={"start":previous}
        previous=float(r["equity"]);result[k]["end"]=previous
    return {k:v["end"]/v["start"]-1 for k,v in result.items()}

def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    plan=tomllib.loads((folder/"source/fifth_round.toml").read_text(encoding="utf-8"))
    require(report["status"]=="completed" and report["research_design_id"]=="R005-v1" and report["strategy_accounts"]==8 and not report["holdout_read"],"Wrong completed factor run")
    require(sha((folder/"source/fifth_round.toml").read_bytes())==report["variant_plan_sha256"],"Prespecified plan changed")
    paths={"baseline":ROOT/"artifacts/backtests"/report["baseline_run"],"reference":ROOT/"artifacts/backtests"/report["reference_run"]}
    old={}
    for key,path in paths.items():
        require(sha((path/"summary.json").read_bytes())==report[key+"_summary_sha256"],"Saved comparison changed")
        old[key]={a["account"]:a for a in json.loads((path/"summary.json").read_text(encoding="utf-8"))["accounts"]}
    accounts={a["account"]:a for a in report["accounts"]}
    require(set(accounts)=={c+"_"+cost for c in plan["candidate_cases"] for cost in plan["cost_scenarios"]},"Extra or missing trials")
    diagnostics=plan["diagnostics"]
    comparisons=[];gates={}
    for case in plan["candidate_cases"]:
        failed=[]
        for cost in plan["cost_scenarios"]:
            name=case+"_"+cost;a=accounts[name]
            baseline=old["baseline"]["trend_"+cost];reference=old["reference"][plan["reference_case"]+"_"+cost]
            trades=rows(folder/(name+"_trades.csv"))
            error=D(100)+sum((D(t["net_pnl"]) for t in trades),D(0))-D(a["ending_equity"])
            require(abs(error)<D("1e-18"),"Cash identity failed")
            require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=72*HOUR for t in trades),"Holding deadline failed")
            daily=rows(folder/(name+"_daily_equity.csv"))
            rdaily=rows(paths["reference"]/(plan["reference_case"]+"_"+cost+"_daily_equity.csv"))
            require([r["date"] for r in daily]==[r["date"] for r in rdaily],"Daily comparison misaligned")
            h,rh=halves(daily),halves(rdaily)
            require(len(h)==6 and h.keys()==rh.keys(),"Halfyear mismatch")
            halfrows=[{"segment":k,"return":h[k],"reference_return":rh[k],"improvement":h[k]-rh[k]} for k in h]
            improved=sum(r["improvement"]>0 for r in halfrows)
            uncertainty=paired_cagr_diagnostic([float(r["equity"]) for r in rdaily],[float(r["equity"]) for r in daily],
                block_days=diagnostics["bootstrap_block_days"],resamples=diagnostics["bootstrap_resamples"],seed=diagnostics["bootstrap_seed"])
            if a["cagr"]-reference["cagr"]<float(diagnostics["minimum_cagr_improvement_percentage_points_each_cost"])/100:failed.append(cost+": 相对上轮过滤版年化提升不足1个百分点")
            if a["max_adverse_bound_drawdown"]>min(float(diagnostics["absolute_maximum_drawdown"]),reference["max_adverse_bound_drawdown"]+float(diagnostics["maximum_drawdown_increase_percentage_points_each_cost"])/100):failed.append(cost+": 回撤超过门槛")
            if a["calmar"] is None or a["calmar"]<reference["calmar"]:failed.append(cost+": 收益回撤比未保持")
            if improved<diagnostics["minimum_improved_halfyears_each_cost"]:failed.append(cost+": 改善半年不足4/6")
            if uncertainty["cagr_improvement_lower_95"]<=0:failed.append(cost+": 重采样未支持稳定改善")
            annual=[v["return"] for v in a["annual"].values()]
            if a["net_return"]<=0 or sum(v>0 for v in annual)<2 or min(annual)<-.1:failed.append(cost+": 原有年度收益门槛未过")
            if not trades or a["liquidation_proxy_events"] or a["execution_ambiguities"] or a["halt_reasons"]:failed.append(cost+": 交易或风险条件未过")
            with (folder/(name+"_gate_events.jsonl")).open(encoding="utf-8") as f:
                gated=[json.loads(line) for line in f]
            own=[e for e in gated if e["gate"]==case]
            require(len(own)==a["entry_filter_counts"].get("factor_pass",0)+a["entry_filter_counts"].get("factor_reject",0),"Factor gate event counts differ")
            for e in own:
                require(e["scheduled_entry_us"]==e["signal_available_us"]+MINUTE,"Factor changed original entry delay")
                if case=="funding_crowding" and e["factor_value"] is not None:
                    v=e["factor_value"]
                    require(v["known_time_us"]==v["settlement_time_us"]+MINUTE and v["known_time_us"]<=e["signal_available_us"],"Future funding used")
            with (folder/(name+"_sizing.jsonl")).open(encoding="utf-8") as f:sizing=[json.loads(line) for line in f]
            missing=sum("diagnostic_unavailable" in e["diagnostic"] for e in sizing)
            winners=sorted((float(t["net_pnl"]) for t in trades if D(t["net_pnl"])>0),reverse=True)
            symbolpnl={s:float(sum((D(t["net_pnl"]) for t in trades if t["symbol"]==s),D(0))) for s in {t["symbol"] for t in trades}}
            comparisons.append({"account":name,"metrics":a,"baseline_cagr":baseline["cagr"],"reference_cagr":reference["cagr"],
                "improvement_vs_baseline":a["cagr"]-baseline["cagr"],"improvement_vs_reference":a["cagr"]-reference["cagr"],
                "reference_drawdown":reference["max_adverse_bound_drawdown"],"improved_halfyears":improved,"halfyears":halfrows,
                "uncertainty_vs_reference":uncertainty,"cash_identity_error":str(error),"holding_deadline_respected":True,
                "factor_timing_audited":True,"factor_gate_records":len(own),"unavailable_sizing_diagnostics":missing,
                "by_symbol":dict(Counter(t["symbol"] for t in trades)),"net_pnl_by_symbol":symbolpnl,
                "top5_share_of_positive_trade_pnl":sum(winners[:5])/sum(winners) if winners else None,
                "net_pnl_less_largest_winner":float(a["ending_equity"])-100-(winners[0] if winners else 0),
                "winner_removal_is_bookkeeping_not_replay":True})
        gates[case]={"prespecified_development_gate_passed":not failed,"failed_conditions":failed,"formal_selection_eligible":False}
    qualified=[k for k,g in gates.items() if g["prespecified_development_gate_passed"]]
    point_winners=[case for case in plan["candidate_cases"] if all(accounts[case+"_"+cost]["cagr"]>old["reference"][plan["reference_case"]+"_"+cost]["cagr"] for cost in plan["cost_scenarios"])]
    decision="本轮没有通过完整改进门槛的因子，原研究基准不替换。" if not qualified else "通过开发期门槛的研究候选："+"、".join(LABELS[c] for c in qualified)+"；仍待独立验证，不连接实盘。"
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    pct=lambda x:f"{x*100:+.2f}%"
    lines=["# 第五轮因子研究 R005-v1","",decision,"",
        f"运行 `{report['run_id']}`，八组回放 {seconds:.1f} 秒。2023—2025 年已观察开发期，2022年11—12月仅热身；每组100 USDT、BTC和ETH共享资金。2026未读取。所有收益均为手续费、滑点、实际资金费后。","",
        "## 与两个已保存版本比较","","| 版本 | 基础年化 | 压力年化 | 基础回撤 | 压力回撤 | 基础／压力交易数 |","|---|---:|---:|---:|---:|---:|"]
    for label,group,prefix in (("R002原版",old["baseline"],"trend"),("R004走势效率过滤",old["reference"],plan["reference_case"])):
        b,s=group[prefix+"_base"],group[prefix+"_stress"]
        lines.append(f"| {label} | {pct(b['cagr'])} | {pct(s['cagr'])} | {100*b['max_adverse_bound_drawdown']:.2f}% | {100*s['max_adverse_bound_drawdown']:.2f}% | {b['round_trips']}／{s['round_trips']} |")
    for case in plan["candidate_cases"]:
        b,s=accounts[case+"_base"],accounts[case+"_stress"]
        lines.append(f"| {LABELS[case]} | {pct(b['cagr'])} | {pct(s['cagr'])} | {100*b['max_adverse_bound_drawdown']:.2f}% | {100*s['max_adverse_bound_drawdown']:.2f}% | {b['round_trips']}／{s['round_trips']} |")
    lines += ["","回撤是既定分钟不利标记价路径的观测值，不是所有真实路径或未来损失上限。R004只是已观察的较高成绩研究参照，未通过独立验证。本轮不只要求赢过较弱的原版。","",
        "## 运行前固定的四个单因子", "",
        "1. 放量确认：当前完整小时USDT成交额÷此前24个小时平均成交额≥1；均值不包含当前小时，历史不足或均值为零拒绝。", 
        "2. 主动买卖压力：当前完整小时主动买入USDT成交额÷总USDT成交额，多头≥55%、空头≤45%；零成交额拒绝。主动买入是吃单方向，不是新增资金净流入，也不是盘口挂单不平衡。", 
        "3. 收盘位置：(小时收盘−小时最低)/(小时最高−小时最低)，多头≥75%、空头≤25%；小时无价格区间时拒绝。", 
        "4. 资金费拥挤度：只使用已结算且至少过去60秒的最新费率，按结算间隔换算到8小时。多头用正费率、空头用相反数，方向性费率≤0.01%才允许；超过24小时未更新或缺失拒绝。它既不读取下一次费率，也不保证下次资金费和价格方向。结算后60秒可用是研究假设，真实发布延迟未核实。", "",
        "每次只在R002原有突破、EMA200和已闭合四小时方向条件后加一个入场过滤。没有叠加R004效率因子，没有改退出、仓位预算、72小时上限、费用或风险。只测试每因子一组研究默认值，不能把它们称作已建立的小时级加密货币风险溢价。", "",
        "## 比较门槛与不确定性", "",
        "相对R004效率过滤版，两种成本都须：年化至少增加1个百分点；回撤最多多2个百分点且≤20%；Calmar不下降；六个半年至少四个改善；配对14天循环区块重采样2000次（种子20261015）的年化改善区间下限>0；并保留原有收益和风险条件。预设门槛未因结果改变。", ""]
    for case,g in gates.items():lines.append("- "+LABELS[case]+"："+("通过开发期门槛。" if g["prespecified_development_gate_passed"] else "未通过："+"；".join(g["failed_conditions"])+"。"))
    lines += ["","| 账户 | 2023 | 2024 | 2025 | 胜过效率版的半年 | 年化改善区间（百分点） |","|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        a=c["metrics"];u=c["uncertainty_vs_reference"]
        lines.append(f"| {c['account']} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {c['improved_halfyears']}/6 | [{100*u['cagr_improvement_lower_95']:+.2f}, {100*u['cagr_improvement_upper_95']:+.2f}] |")
    lines += ["","重采样只是已观察开发样本的条件诊断，没有校正多轮研究和人为选择偏差，不能解释为未来盈利概率。分半年不重置本金、不重新训练；本轮未训练机器学习模型，未计算因子IC或PBO。", "",
        "## 集中度、资金与拒单", ""]
    for c in comparisons:
        a=c["metrics"];share=c["top5_share_of_positive_trade_pnl"]
        concentration=f"{100*share:.2f}%" if share is not None else "无盈利单"
        lines.append(f"- {c['account']}：期末 {float(a['ending_equity']):.4f} USDT；交易分布 {json.dumps(c['by_symbol'])}；分币净盈亏 {json.dumps(c['net_pnl_by_symbol'])}；前五笔占全部盈利交易净收益之和 {concentration}；账面扣最大盈利单后净盈亏 {c['net_pnl_less_largest_winner']:+.4f} USDT；手续费 {float(a['fees']):.4f}，资金费净额 {float(a['funding']):+.4f}。拒单预算／订单门槛 {json.dumps(a['sizing_diagnostics'])}；诊断不可用 {c['unavailable_sizing_diagnostics']} 条。")
    lines += ["","扣掉最大盈利单只是账面诊断，没有重新回放后续资金占用。预算限制与最小订单失败标志可能重叠，不能相加当成独立拒单数。", "",
        "## 数据和研究边界", "",
        "量能来自既有已验收官方原始K线ZIP，每币27768个小时，合计55536小时。旧标准化OHLC文件没有量能字段，因此本轮只新增提取并校验USDT成交额和主动买入USDT成交额、逐分钟时间连续性、小时完整性及买入额不超过总额，未重跑原始文件校验和扫描。新表、来源记录及哈希清单见本目录source/factor_manifest.json。原始历史数据和旧规范化文件保持不变。", "",
        "累计已完成有效候选×成本配置24个（此前16、本轮8），另有首轮作废调试8个账户，全部保留。八组结果全部报告，不追加阈值搜索、因子组合、币种、杠杆或保留期评估。六项新增关键测试通过；各账户核对资金守恒、持仓期限、因子日志延迟与资金费可见性。", "",
        "文献只作为研究方向依据：[Risks and Returns of Cryptocurrency](https://www.nber.org/papers/w24877)讨论动量和注意力；[Common Risk Factors in Cryptocurrency](https://www.nber.org/papers/w25882)研究较大横截面中的市场、规模与动量，不能直接推出BTC/ETH小时策略有效。原始字段定义来自[币安公开数据项目](https://github.com/binance/binance-public-data)。多轮选择偏差参考[The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。", "",
        "未持有点时历史持仓量、盘口、清算流和注意力数据的完整档案，本轮不伪造这些因子。真实历史订单和保证金规则仍未核实，继续沿用既有代理、固定2.5%维持保证金假设。基础单边0.06%手续费＋0.02%不利价格调整，压力0.06%＋0.06%，另计资金费和步长取整。100USDT、单笔0.5%风险、总敞口基准1倍保持不变。2026未读取，没有连接实盘，正式选模资格仍为false。", ""]
    text="\n".join(lines)
    evidence={"run_id":report["run_id"],"research_design":"R005-v1","runtime_seconds":seconds,"decision":decision,
        "gates":gates,"comparisons":comparisons,"point_winners_vs_reference_both_costs":point_winners,
        "retained_development_candidates":qualified,"research_baseline_retained":report["baseline_run"],
        "reference_research_case":report["reference_run"]+":"+plan["reference_case"],"new_accounts":8,"new_checks_passed":6,
        "valid_candidate_cost_configurations_total":24,"superseded_debug_accounts_total":8,
        "holdout_read":False,"formal_selection_eligible":False,"independent_validation":False,"selection_bias_corrected":False,"background_tasks_remaining":0}
    preserve(folder/"comparison.md",text.encode())
    preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    for file in (Path(__file__),ROOT/"src/perpquant/uncertainty.py"):preserve(folder/"source"/file.name,file.read_bytes())
    (ROOT/"docs/第五轮因子研究结果.md").write_text(text,encoding="utf-8")
    pointer_path=ROOT/"artifacts/backtests/latest.json"
    if pointer_path.exists():
        body=pointer_path.read_bytes();preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R005-v1","scope":"development_screening_proxy_rules",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"research_baseline_retained":report["baseline_run"],
        "reference_research_case":evidence["reference_research_case"],"retained_development_candidates":qualified,
        "holdout_read":False,"formal_selection_eligible":False}
    pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:evidence[k] for k in ("run_id","runtime_seconds","decision","point_winners_vs_reference_both_costs","gates")},ensure_ascii=False))

if __name__=="__main__":main(sys.argv[1])
