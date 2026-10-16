"""Report every prespecified R003 account; never trigger another search."""
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
from perpquant.engine import sha,HOUR
from perpquant.quality import under
from perpquant.uncertainty import paired_cagr_diagnostic

def rows(path):
    with path.open(encoding="utf-8-sig",newline="") as f:
        return list(csv.DictReader(f))

def segments(records):
    result={}
    previous=100.0
    for row in records:
        key=row["date"][:4]+("H1" if row["date"][5:7]<="06" else "H2")
        if key not in result:result[key]={"start":previous}
        previous=float(row["equity"])
        result[key]["end"]=previous
    return {k:v["end"]/v["start"]-1 for k,v in result.items()}

def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    plan=tomllib.loads((folder/"source/third_round.toml").read_text(encoding="utf-8"))
    spec=plan["diagnostics"]
    require(report["status"]=="completed" and report["research_design_id"]=="R003-v1" and not report["holdout_read"],"Wrong run")
    baseline_folder=ROOT/"artifacts/backtests"/report["baseline_run"]
    require(sha((baseline_folder/"summary.json").read_bytes())==report["baseline_summary_sha256"],"Baseline changed")
    baseline=json.loads((baseline_folder/"summary.json").read_text(encoding="utf-8"))
    old={a["account"]:a for a in baseline["accounts"]}
    new={a["account"]:a for a in report["accounts"]}
    require(set(new)=={case+"_"+cost for case in plan["candidate_cases"] for cost in plan["cost_scenarios"]},"Unexpected accounts")
    comparisons=[]
    gates={}
    for case in plan["candidate_cases"]:
        failed=[]
        for cost in plan["cost_scenarios"]:
            name=case+"_"+cost
            a,b=new[name],old["trend_"+cost]
            trades=rows(folder/(name+"_trades.csv"))
            cash_error=D(100)+sum((D(t["net_pnl"]) for t in trades),D(0))-D(a["ending_equity"])
            require(abs(cash_error)<D("1e-18"),"Trade cash identity failed")
            require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=a["holding_limit_hours"]*HOUR for t in trades),"Holding limit failed")
            daily=rows(folder/(name+"_daily_equity.csv"))
            bdaily=rows(baseline_folder/("trend_"+cost+"_daily_equity.csv"))
            require([r["date"] for r in daily]==[r["date"] for r in bdaily],"Misaligned paired days")
            halves,bhalves=segments(daily),segments(bdaily)
            require(len(halves)==spec["halfyear_segments"] and halves.keys()==bhalves.keys(),"Wrong segments")
            paired=[{"segment":k,"baseline_return":bhalves[k],"candidate_return":halves[k],"improvement":halves[k]-bhalves[k]} for k in halves]
            improved=sum(r["improvement"]>0 for r in paired)
            uncertainty=paired_cagr_diagnostic([float(r["equity"]) for r in bdaily],[float(r["equity"]) for r in daily],
                block_days=spec["bootstrap_block_days"],resamples=spec["bootstrap_resamples"],seed=spec["bootstrap_seed"])
            uplift=a["cagr"]-b["cagr"]
            if uplift<float(spec["minimum_cagr_improvement_percentage_points_each_cost"])/100:failed.append(cost+": 年化提升不足 1 个百分点")
            if a["max_adverse_bound_drawdown"]>min(float(spec["absolute_maximum_drawdown"]),b["max_adverse_bound_drawdown"]+float(spec["maximum_drawdown_increase_percentage_points_each_cost"])/100):failed.append(cost+": 回撤增幅超过门槛")
            if spec["require_non_decreasing_calmar_each_cost"] and (a["calmar"] is None or a["calmar"]<b["calmar"]):failed.append(cost+": 收益回撤比下降")
            if improved<spec["minimum_improved_halfyears_each_cost"]:failed.append(cost+": 改善的半年不足 4/6")
            if spec["require_positive_lower_improvement_bound_each_cost"] and uncertainty["cagr_improvement_lower_95"]<=0:failed.append(cost+": 重采样改善区间跨过零")
            annual=[v["return"] for v in a["annual"].values()]
            if a["net_return"]<=0 or sum(v>0 for v in annual)<2 or min(annual)<-.1:failed.append(cost+": 未过原有收益门槛")
            if not trades or a["liquidation_proxy_events"] or a["execution_ambiguities"] or a["halt_reasons"]:failed.append(cost+": 交易或风险保护条件未满足")
            winners=sorted((float(t["net_pnl"]) for t in trades if D(t["net_pnl"])>0),reverse=True)
            symbol_net={s:float(sum((D(t["net_pnl"]) for t in trades if t["symbol"]==s),D(0))) for s in {t["symbol"] for t in trades}}
            comparisons.append({"account":name,"metrics":a,"baseline_cagr":b["cagr"],"cagr_improvement":uplift,
                "baseline_drawdown":b["max_adverse_bound_drawdown"],"improved_halfyears":improved,"halfyears":paired,
                "uncertainty":uncertainty,"cash_identity_error":str(cash_error),"holding_deadline_respected":True,
                "by_symbol":dict(Counter(t["symbol"] for t in trades)),"net_pnl_by_symbol":symbol_net,
                "top5_share_of_positive_trade_pnl":sum(winners[:5])/sum(winners) if winners else None,
                "net_pnl_less_largest_winner":float(a["ending_equity"])-100-(winners[0] if winners else 0),
                "removed_winner_is_accounting_diagnostic_not_replay":True})
        gates[case]={"prespecified_development_gate_passed":not failed,"failed_conditions":failed,"formal_selection_eligible":False}
    retained=[k for k,v in gates.items() if v["prespecified_development_gate_passed"]]
    decision="保留 R002 趋势版为研究基准，不升级策略。" if not retained else "通过开发期改进门槛的研究候选："+"、".join(retained)+"；仍未独立验证，不接实盘。"
    pct=lambda v:f"{v*100:+.2f}%"
    labels={"breakout24":"提前突破（24 小时）","hold168":"延长持仓（最多 7 天）"}
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    lines=["# 第三轮策略优化 R003-v1","",decision,"",
        f"运行 `{report['run_id']}`，基准 `{report['baseline_run']}`；四组回放 {seconds:.1f} 秒。初始每组 100 USDT；2023—2025 连续交易，2022 年 11—12 月仅热身。2026 保留期未读取。","",
        "## 扣除手续费、滑点与资金费后的结果","",
        "| 版本／成本 | 原版年化 | 本版年化 | 年化改善（百分点） | 原版回撤 | 本版回撤 | 期末 USDT | 交易数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        a=c["metrics"]
        case,cost=c["account"].rsplit("_",1)
        lines.append(f"| {labels[case]}／{cost} | {pct(c['baseline_cagr'])} | {pct(a['cagr'])} | {100*c['cagr_improvement']:+.2f} | {100*c['baseline_drawdown']:.2f}% | {100*a['max_adverse_bound_drawdown']:.2f}% | {float(a['ending_equity']):.4f} | {a['round_trips']} |")
    lines += ["","回撤是固定分钟不利标记价路径下的观测值，不是未来最大损失保证。","","## 预设改进门槛与结论", "",
        "两种成本均须满足：年化至少提高 1 个百分点；回撤最多增加 2 个百分点且不超过 20%；Calmar 不下降；六个半年至少四个改善；配对区块重采样的改善区间下限大于零。还须保留原有正收益及风险门槛。这些门槛在本轮运行前保存，不因结果改动。",""]
    for case,g in gates.items():
        lines.append("- "+labels[case]+"："+("通过开发期门槛。" if g["prespecified_development_gate_passed"] else "未通过："+"；".join(g["failed_conditions"])+"。"))
    lines += ["","## 分期与不确定性","","| 账户 | 2023 | 2024 | 2025 | 改善半年数 | 年化改善重采样区间（百分点） |","|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        a=c["metrics"];u=c["uncertainty"]
        lines.append(f"| {c['account']} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {c['improved_halfyears']}/6 | [{100*u['cagr_improvement_lower_95']:+.2f}, {100*u['cagr_improvement_upper_95']:+.2f}] |")
    lines += ["","各半年均沿用原账户资金，不重置本金、不重新训练参数。逐段明细：","","| 账户 | 半年 | 原版收益 | 本版收益 |","|---|---|---:|---:|"]
    for c in comparisons:
        for h in c["halfyears"]:lines.append(f"| {c['account']} | {h['segment']} | {pct(h['baseline_return'])} | {pct(h['candidate_return'])} |")
    lines += ["","重采样使用同一天的原版和改版日对数收益配对，循环抽取 14 天区块，共 2000 次，种子 20261015，报告 2.5%—97.5% 分位。只是对已知开发数据的条件诊断，没有修正此前反复选策略造成的偏差，也不是未来盈利概率或独立验证。未计算 PBO。","",
        "## 盈利集中度","","前五笔盈利占比以所有盈利交易净收益之和为分母。扣掉最大盈利单只做账面敏感性说明，未重新模拟资金占用及后续订单。",""]
    for c in comparisons:
        a=c["metrics"]
        lines.append(f"- {c['account']}：交易币种 {json.dumps(c['by_symbol'])}；分币净盈亏 USDT {json.dumps(c['net_pnl_by_symbol'])}；前五笔占盈利总额 {100*c['top5_share_of_positive_trade_pnl']:.2f}%；账面扣最大盈利单后净盈亏 {c['net_pnl_less_largest_winner']:+.4f} USDT；手续费 {float(a['fees']):.4f}，资金费净额 {float(a['funding']):+.4f}。")
    lines += ["","## 如何约束过拟合","",
        "只测试两个有明确理由的单项改动，分开比较，未组合；沿用 R002 已闭合四小时方向过滤。24 小时版用当前小时之前的 24 根完整小时线高低点，原版 48 小时通道保留；7 天版只把硬持仓期限从 72 延至 168 小时，原有 EMA50 退出和 2ATR 止损仍可提前平仓。资金、风险预算、手续费与滑点均未提高或调低。","",
        "累计已完成有效候选×成本配置 12 个（R001 4、R002 4、R003 4）；另有 R001 两次作废调试共 8 个账户，全部保留并标注原因。本轮没有根据中间成绩追加参数。2023—2025 已多次观察，所以只能降低过拟合风险，无法证明没有过拟合。","",
        "趋势假设的背景参考 [Time Series Momentum](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum)，它不证明本轮加密货币参数有效；多次试验与选择偏差参考 [The Probability of Backtest Overfitting](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。","",
        "## 执行与交付边界","",
        "历史订单过滤规则、保证金档位仍使用既有代理；固定 2.5% 维持保证金压力假设不是已验证历史规则。单边基础成本手续费 0.06%＋不利价格调整 0.02%；压力为 0.06%＋0.06%，另计资金费及步长取整。研究基准总敞口上限仍为 1 倍，用户最高 10 倍仅为上限。","",
        "四项新增逻辑检查通过；四个新账户均核对资金守恒及实际最长持仓期限。没有重复扫描已验收数据校验和、没有重跑原版、没有连接实盘。配置、实际源代码、每账户解析配置、事件、逐笔交易、日／小时权益与统计均在本次独立目录。正式选模资格仍为 false。",""]
    text="\n".join(lines)
    evidence={"run_id":report["run_id"],"research_design":"R003-v1","runtime_seconds":seconds,"decision":decision,
        "gates":gates,"comparisons":comparisons,"retained_development_candidates":retained,
        "research_baseline_retained":report["baseline_run"] if not retained else None,
        "new_checks_passed":4,"new_accounts":4,"valid_candidate_cost_configurations_total":12,
        "superseded_debug_accounts_total":8,"holdout_read":False,"independent_validation":False,
        "selection_bias_corrected":False,"formal_selection_eligible":False,"background_tasks_remaining":0}
    preserve(folder/"comparison.md",text.encode())
    preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    for file in (Path(__file__),ROOT/"src/perpquant/uncertainty.py"):
        preserve(folder/"source"/file.name,file.read_bytes())
    (ROOT/"docs/第三轮优化结果.md").write_text(text,encoding="utf-8")
    pointer_path=ROOT/"artifacts/backtests/latest.json"
    if pointer_path.exists():
        body=pointer_path.read_bytes()
        preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R003-v1","scope":"development_screening_proxy_rules",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"research_baseline_retained":evidence["research_baseline_retained"],
        "retained_development_candidates":retained,"holdout_read":False,"formal_selection_eligible":False}
    pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:evidence[k] for k in ("run_id","runtime_seconds","decision","gates")},ensure_ascii=False))

if __name__=="__main__":
    main(sys.argv[1])
