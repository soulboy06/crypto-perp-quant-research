"""Publish a completed screening run; check only the closed-trade cash identity."""
from collections import Counter
import csv
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.quality import under


def main(relative):
    folder=under(ROOT,relative,"artifacts/backtests")
    report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
    assert report["status"]=="completed" and report["holdout_read"] is False
    assert not (folder/"superseded.json").exists(),"Do not publish superseded debug results"
    names={"trend_base":"趋势／基础成本","trend_stress":"趋势／压力成本",
           "mean_reversion_base":"均值回归／基础成本","mean_reversion_stress":"均值回归／压力成本"}
    checks=[]
    breakdown={}
    for account in report["accounts"]:
        name=account["account"]
        with (folder/(name+"_trades.csv")).open(encoding="utf-8-sig",newline="") as f:
            trades=list(csv.DictReader(f))
        net=sum((D(t["net_pnl"]) for t in trades),D(0))
        error=D(100)+net-D(account["ending_equity"])
        assert abs(error)<=D("1e-18"),(name,error)
        limit=(72 if name.startswith("trend_") else 24)*3_600_000_000
        assert all(int(t["exit_time_us"])-int(t["entry_time_us"])<=limit for t in trades),"Holding deadline exceeded"
        checks.append({"account":name,"closed_trade_cash_identity_error":str(error),"holding_deadline_respected":True})
        breakdown[name]={"by_symbol":dict(Counter(t["symbol"] for t in trades)),
                         "by_exit":dict(Counter(t["exit_reason"] for t in trades))}
    metrics={a["account"]:a for a in report["accounts"]}
    gate={}
    for strategy in ("trend","mean_reversion"):
        base,stress=metrics[strategy+"_base"],metrics[strategy+"_stress"]
        annual=[v["return"] for v in base["annual"].values()]
        reasons=[]
        if base["net_return"]<=0:reasons.append("基础成本后净收益不为正")
        if stress["net_return"]<=0:reasons.append("压力成本后净收益不为正")
        if sum(v>0 for v in annual)<2:reasons.append("三个年度中正收益不足两个")
        if min(annual)<-.10:reasons.append("最差年度收益低于 -10%")
        if max(base["max_adverse_bound_drawdown"],stress["max_adverse_bound_drawdown"])>.20:reasons.append("压力路径回撤超过 20%")
        if base["liquidation_proxy_events"] or stress["liquidation_proxy_events"]:reasons.append("存在代理强平")
        if base["execution_ambiguities"] or stress["execution_ambiguities"]:reasons.append("存在分钟内执行时序歧义")
        if "capital_stop" in base["halt_reasons"]+stress["halt_reasons"]:reasons.append("触及本金停止线")
        gate[strategy]={"numerical_screen_passed":not reasons,"failed_conditions":reasons,"formal_selection_eligible":False}
    percent=lambda v:f"{v*100:+.2f}%"
    lines=["# 首轮开发期研究回测结果", "", f"运行编号：`{report['run_id']}`。", "",
           "实际运行区间为 2023—2025 年，2022 年 11—12 月只作指标热身。每组是独立的连续 100 USDT 账户，BTC、ETH 在组内共享资金，年初没有重置本金。账户总敞口基准上限 1 倍，逐仓保证金设置按 10 倍模拟；用户授权的最高 10 倍并不等于本轮用满 10 倍。", "",
           "| 账户 | 期末 USDT | 扣费后收益 | 压力路径回撤 | 完整交易数 | 手续费 USDT | 资金费净收入 USDT |",
           "|---|---:|---:|---:|---:|---:|---:|"]
    for a in report["accounts"]:
        lines.append(f"| {names[a['account']]} | {float(a['ending_equity']):.4f} | {percent(a['net_return'])} | {a['max_adverse_bound_drawdown']*100:.2f}% | {a['round_trips']} | {float(a['fees']):.4f} | {float(a['funding']):+.4f} |")
    lines += ["", "收益已经包含入场与退出的不利价格调整、手续费和实际资金费。成交价按不利方向取价格步长。表中的回撤来自下文指定的分钟压力路径，不是对所有真实分钟路径的最大回撤保证。现金参考账户期末为 100 USDT。", "",
              "## 年度归因", "", "| 账户 | 2023 | 2024 | 2025 |", "|---|---:|---:|---:|"]
    for a in report["accounts"]:
        lines.append("| "+names[a["account"]]+" | "+" | ".join(percent(a["annual"][y]["return"]) for y in ("2023","2024","2025"))+" |")
    lines += ["", "## 筛选结论", ""]
    for strategy,label in (("trend","趋势"),("mean_reversion","均值回归")):
        item=gate[strategy]
        lines.append(f"- {label}："+("数值条件通过，但规则证据未完成，不能进入正式选模。" if item["numerical_screen_passed"] else "未通过首轮数值条件："+"；".join(item["failed_conditions"])+"。"))
    lines += ["", "没有追加杠杆敏感性、搜索参数或打开 2026 保留区间。两个成本账户独立计算仓位和拒单；较高成本可能减少成交，因此压力账户的净收益不保证逐项低于基础账户。", "",
              "## 拒单与交易分布", ""]
    for a in report["accounts"]:
        lines.append(f"- {names[a['account']]}：分币交易 {json.dumps(breakdown[a['account']]['by_symbol'],ensure_ascii=False)}；主要拒单 "+json.dumps(sorted(a["rejections"].items(),key=lambda x:-x[1])[:4],ensure_ascii=False)+"。")
    lines += ["", "BTC 当前快照的最低开仓名义金额为 50 USDT、ETH 为 20 USDT。100 USDT 账户在固定 0.5% 单次风险预算下可能无法交易很多信号；没有为了凑最低订单抬高风险。", "",
              "## 执行代理及限制", "",
              "采用运行前保存的 EXEC-001-v1：历史全部日期套用已保存的当前数量和价格步长、最低开仓金额；维持保证金采用明确假设的固定 2.5%，不是取得的历史交易所档位。代理强平额外费用取已保存的当前交易所快照；资金费扣款顺序仍是未逐日验证的研究约定。该规则情景不保证在所有方面都比历史真实规则更保守，不能称历史精确复现。", "",
              "分钟内先消费该分钟的资金费，再执行订单；若涉及临近结算的退出则标记歧义。普通信号仅使用闭合小时数据，延迟 60 秒在分钟开盘近似成交。分钟持仓风险采用两个币种各自不利标记价同时出现的压力路径，再处理止损，最后恢复闭合分钟价格。该联合路径可能不是实际发生的先后顺序，也未穷举有利峰值先发生再下跌的所有路径。", "",
              "止损与代理强平在同一分钟都可能发生时采用不利退出，并记录本笔止损先发生的局部替代盈亏；未完成整个账户另一条路径的重放，因此不能把局部差异称为完整收益上下界。所有这类结果均不具备正式选模资格。", "",
              "完整开发数据已有验收，本轮不再重复扫描校验和。只新增检查了信号可见时间、执行延迟、期末退出、账目与风险预算；运行后再核对一次每个账户的初始资金加逐笔净盈亏等于期末资金。", "",
              "两遍调试运行均保留并标记作废：SCREEN-20261015T070746Z-273758dc 的持仓风险预算重复预留了已支付的入场摩擦，实际资金没有重复扣费；SCREEN-20261015T071119Z-aee71b34 的持仓期限检查等到下一小时，可能比约定期限多持仓一小时。现已按分钟强制执行 24／72 小时退出，并核对所有实际交易的期限。重放没有改变策略参数、成本值或数据范围，也没有在几遍结果中挑最好的一遍。四个候选配置共执行三遍、累计初始化十二个账户；前两遍均未用于选模。", "",
              "## 文件", "", "本报告对应目录内的 `summary.json`、各账户 `*_metrics.json`、`*_trades.csv`、`*_events.jsonl`、`*_daily_equity.csv` 和 `*_hourly_equity.csv`。`source/` 保存实际执行代码及三份配置。分钟标记价来自已绑定的数据清单，事件日志配合源数据重放，不声称是独立于源数据的 SQLite 全量逐分钟日志。", "",
              f"完成时间：{report['completed_at_utc']}。研究初筛结果不是实盘收益预测。", ""]
    text="\n".join(lines)
    preserve(folder/"report.md",text.encode())
    evidence={"run_id":report["run_id"],"cash_identity_checks":checks,"gate":gate,
              "breakdown":breakdown,"new_execution_checks":6,"data_rehash_repeated":False,
              "debug_runs_superseded":["SCREEN-20261015T070746Z-273758dc","SCREEN-20261015T071119Z-aee71b34"],
              "unique_candidate_accounts":4,"implementation_passes":3,"total_account_initializations":12,"no_parameter_search":True}
    preserve(folder/"publication.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    pointer={"run_id":report["run_id"],"scope":"development_screening_proxy_rules", "summary":(folder/"summary.json").relative_to(ROOT).as_posix(),
             "report":(folder/"report.md").relative_to(ROOT).as_posix(),"summary_sha256":hashlib.sha256((folder/"summary.json").read_bytes()).hexdigest(),
             "holdout_read":False,"formal_selection_eligible":False}
    (ROOT/"artifacts/backtests/latest.json").write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
    (ROOT/"docs/首轮回测结果.md").write_text(text,encoding="utf-8")
    print(json.dumps({"published":str(folder/"report.md"),"gate":gate,"accounts":[{k:a[k] for k in ('account','ending_equity','net_return','round_trips','max_adverse_bound_drawdown','fees','funding','execution_ambiguities','liquidation_proxy_events')} for a in report['accounts']]},ensure_ascii=False))


if __name__=="__main__":
    main(sys.argv[1])
