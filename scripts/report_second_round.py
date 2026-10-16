"""Compare only the four prespecified R002 accounts against saved R001 results."""
from collections import Counter
import csv
from datetime import datetime
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.config import require
from perpquant.quality import under


def sha(body):
    return hashlib.sha256(body).hexdigest()


def numerical_gate(accounts,strategy):
    base,stress=accounts[strategy+"_base"],accounts[strategy+"_stress"]
    reasons=[]
    if base["net_return"]<=0:reasons.append("基础成本后收益不为正")
    if stress["net_return"]<=0:reasons.append("压力成本后收益不为正")
    annual=[v["return"] for v in base["annual"].values()]
    if sum(v>0 for v in annual)<2:reasons.append("基础场景三个年度中正收益不足两个")
    if min(annual)<-.1:reasons.append("基础场景最差年度低于 -10%")
    for a in (base,stress):
        if a["max_adverse_bound_drawdown"]>.2:reasons.append(a["account"]+" 压力路径回撤超过 20%")
        if not a["round_trips"]:reasons.append(a["account"]+" 没有可评价的实际交易")
        if a["liquidation_proxy_events"]:reasons.append(a["account"]+" 出现代理强平")
        if a["execution_ambiguities"]:reasons.append(a["account"]+" 出现未解决的执行歧义")
        if "capital_stop" in a["halt_reasons"]:reasons.append(a["account"]+" 触及本金停止线")
    return {"numerical_screen_passed":not reasons,"failed_conditions":reasons,"formal_selection_eligible":False}


def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    require(report["status"]=="completed" and report["research_design_id"]=="R002-v1" and report["strategy_accounts"]==4,"Wrong comparison run")
    require(report["holdout_read"] is False and report["parameter_search"] is False,"Research scope changed")
    baseline_path=ROOT/"artifacts/backtests"/report["baseline_run"]/"summary.json"
    require(sha(baseline_path.read_bytes())==report["baseline_summary_sha256"],"Baseline result changed")
    old=json.loads(baseline_path.read_text(encoding="utf-8"))
    before={a["account"]:a for a in old["accounts"]}
    after={a["account"]:a for a in report["accounts"]}
    require(set(before)==set(after) and len(after)==4,"Account comparison mismatch")
    checks,comparisons=[],[]
    for name,a in after.items():
        with (folder/(name+"_trades.csv")).open(encoding="utf-8-sig",newline="") as f:
            trades=list(csv.DictReader(f))
        net=sum((D(t["net_pnl"]) for t in trades),D(0))
        error=D(100)+net-D(a["ending_equity"])
        require(abs(error)<=D("1e-18"),"Closed trade cash identity failed")
        limit=(72 if name.startswith("trend_") else 24)*3_600_000_000
        require(all(0<=int(t["exit_time_us"])-int(t["entry_time_us"])<=limit for t in trades),"Holding deadline violated")
        checks.append({"account":name,"cash_identity_error":str(error),"holding_deadline_respected":True})
        b=before[name]
        comparisons.append({"account":name,"old_net_return":b["net_return"],"new_net_return":a["net_return"],
            "improvement_percentage_points":100*(a["net_return"]-b["net_return"]),
            "old_pressure_path_drawdown":b["max_adverse_bound_drawdown"],"new_pressure_path_drawdown":a["max_adverse_bound_drawdown"],
            "old_trades":b["round_trips"],"new_trades":a["round_trips"],"old_fees":b["fees"],"new_fees":a["fees"],
            "new_gross_pnl_at_execution_prices":str(sum((D(t["gross_pnl"]) for t in trades),D(0))),
            "new_by_symbol":dict(Counter(t["symbol"] for t in trades)),"filter_counts":a["entry_filter_counts"]})
    gate={s:numerical_gate(after,s) for s in ("trend","mean_reversion")}
    seconds=(datetime.fromisoformat(report["completed_at_utc"])-datetime.fromisoformat(report["started_at_utc"])).total_seconds()
    labels={"trend_base":"趋势／基础成本","trend_stress":"趋势／压力成本","mean_reversion_base":"均值回归／基础成本","mean_reversion_stress":"均值回归／压力成本"}
    pct=lambda value:f"{100*value:+.2f}%"
    lines=["# 第二轮单项改版对比 R002-v1","",f"运行：`{report['run_id']}`；基准：`{report['baseline_run']}`。四组回放耗时 {seconds:.1f} 秒。","",
        "范围仍为 2023—2025 年开发数据，2022 年 11—12 月用于热身。每组初始 100 USDT，连续运行且不按年重置；BTC、ETH 在组内共享资金。总敞口基准上限 1 倍、逐仓设置按 10 倍模拟，用户最高 10 倍上限没有被用作扩大风险的理由。","",
        "## 与原版直接对比","","以下收益均为三年累计扣费收益，包含模拟滑点、手续费和实际资金费，不是年度收益。","",
        "| 账户 | 原版累计收益 | 改版累计收益 | 改善（百分点） | 压力路径回撤：原→新 | 完整交易数：原→新 |",
        "|---|---:|---:|---:|---:|---:|"]
    for c in comparisons:
        lines.append(f"| {labels[c['account']]} | {pct(c['old_net_return'])} | {pct(c['new_net_return'])} | {c['improvement_percentage_points']:+.2f} | {100*c['old_pressure_path_drawdown']:.2f}% → {100*c['new_pressure_path_drawdown']:.2f}% | {c['old_trades']} → {c['new_trades']} |")
    lines += ["","回撤是 EXEC-001-v1 指定的分钟联合不利标记价路径下的观测值，不能理解为所有真实分钟路径的最大回撤保证。","",
        "## 年度与成本","","| 账户 | 2023 | 2024 | 2025 | 复合年化 | 手续费 USDT | 资金费净收入 USDT |","|---|---:|---:|---:|---:|---:|---:|"]
    for name,a in after.items():
        lines.append(f"| {labels[name]} | "+" | ".join(pct(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+f" | {pct(a['cagr'])} | {float(a['fees']):.4f} | {float(a['funding']):+.4f} |")
    lines += ["","## 本轮结论",""]
    for s,label in (("trend","趋势过滤版"),("mean_reversion","均值回归净空间版")):
        g=gate[s]
        lines.append(f"- {label}："+("通过原先的开发期数值门槛，可保留为研究候选。" if g["numerical_screen_passed"] else "仍未通过："+"；".join(g["failed_conditions"])+"。"))
    lines += ["","改善发生在已经看过的开发数据中。过滤条件虽然在本轮运行前固定，仍是参考首轮亏损分析提出的，因此这些结果不构成新的独立样本外证据，也不能证明未来稳定盈利。正式选模资格仍为 false。","",
        "本轮只运行约定的四个账户；没有追加参数扫描、组合两种改动、杠杆敏感性或 2026 保留期评价。正收益候选不会自动触发进一步试验。","",
        "## 运行前固定的两个单项改动","",
        "趋势版：保留原来的 1 小时突破、EMA200、止损和退出条件，只额外要求最近一根已闭合 4 小时线 EMA20 > EMA50 才允许多头，EMA20 < EMA50 才允许空头。4 小时线按 UTC 00/04/08/12/16/20 对齐；EMA 用各自前 N 个已闭合收盘价均值初始化。不读取尚未闭合的 4 小时条目，不满足条件时只禁止入场，不阻止原有退出。20/50 是本轮预先选取的研究默认值，没有搜索最优参数。","",
        "均值回归版：保留原来的 z-score、状态限制、2ATR 止损、动态 SMA20 退出和 24 小时硬期限。只在原有延迟 60 秒后的入场分钟，用当时已知报价，比较扣费后目标空间与止损风险，要求比值至少为 1。目标仅取信号小时的 SMA20，用于入场评估；没有把它改成固定止盈订单。价格回到该均线及计算出的收益都不是保证。","",
        "按单位币数量计算：净目标空间 = 方向×（预计目标退出成交价−预计入场成交价）−入场及目标退出手续费−资金费缓冲；止损风险 = 方向×（预计入场成交价−预计止损退出成交价）＋入场及止损退出手续费＋跳价及资金费缓冲。入场、目标退出和止损退出价格均包含各自成本场景的不利价格调整及价格步长取整。缓冲用于筛选及仓位预算，实际资金费仍逐次全额入账。","",
        "## 过滤效果与小账户限制",""]
    for c in comparisons:
        a=after[c["account"]]
        lines.append(f"- {labels[c['account']]}：过滤统计 {json.dumps(c['filter_counts'],ensure_ascii=False)}；实际分币交易 {json.dumps(c['new_by_symbol'],ensure_ascii=False)}；数量预算或最低订单拒绝 {a['rejections'].get('Entry rejected: budget_or_minimum_order',0)} 次。")
    lines += ["","过滤通过数不等于成交数，之后仍需满足既定风险预算、资金、最小订单和执行条件。过滤会改变持仓占用和后续可交易信号，因此新旧账户不是逐笔配对样本，不能把所有减少的交易都归为原版亏损单。","",
        "## 保持不变与证据边界","",
        "R001-v1、DATA-001-v1、EXEC-001-v1 保持原样。仍按已保存的当前订单过滤规则作用于历史数据；维持保证金采用固定 2.5% 的明示压力假设，强平收费取既有快照，资金费扣款顺序仍是研究约定。上述口径不等于历史交易所规则，也不保证在所有方面都更保守。","",
        "已有数据验收未重跑。本轮只运行四项新增过滤器／时间可见性检查，再核对四个新账户的初始资金加逐笔净盈亏等于期末现金，以及实际交易没有超过 24／72 小时持仓期限。原版成绩直接复用有效归档，未重新回测基准。","",
        "本目录 `source/second_round.toml` 保存运行前方案，`source/` 保存实际代码及其他配置；各账户 `*_gate_events.jsonl` 记录过滤时点、四小时上下文或净空间计算，`*_variant_metrics.json` 保存过滤与绩效统计。交易、事件、每日／小时权益文件继续保留。","",
        f"完成时间：{report['completed_at_utc']}。",""]
    text="\n".join(lines)
    preserve(folder/"comparison.md",text.encode())
    evidence={"research_design":"R002-v1","run_id":report["run_id"],"runtime_seconds":seconds,"comparisons":comparisons,"gate":gate,
        "cash_and_deadline_checks":checks,"new_filter_checks_passed":4,"new_accounts":4,"baseline_replayed":False,
        "data_checksum_rescan":False,"holdout_read":False,"parameter_search":False,"formal_selection_eligible":False,"stage_background_tasks_remaining":0}
    preserve(folder/"comparison.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    preserve(folder/"source/report_second_round.py",Path(__file__).read_bytes())
    (ROOT/"docs/第二轮对比结果.md").write_text(text,encoding="utf-8")
    old_pointer=ROOT/"artifacts/backtests/latest.json"
    if old_pointer.exists():
        body=old_pointer.read_bytes()
        preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
    pointer={"run_id":report["run_id"],"research_design_id":"R002-v1","scope":"development_screening_proxy_rules",
        "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),"report":(folder/"comparison.md").relative_to(ROOT).as_posix(),
        "summary_sha256":sha((folder/"summary.json").read_bytes()),"holdout_read":False,"formal_selection_eligible":False}
    old_pointer.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"report":str(folder/"comparison.md"),"runtime_seconds":seconds,"gate":gate,"comparisons":comparisons},ensure_ascii=False))


if __name__=="__main__":
    main(sys.argv[1])
