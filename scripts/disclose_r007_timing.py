"""Append explicit execution limits; preserve original report and financial metrics."""
import csv
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.engine import sha
from perpquant.quality import under
folder=under(ROOT,sys.argv[1],"artifacts/backtests")
report=json.loads((folder/"summary.json").read_text(encoding="utf-8"))
evidence=[]
lines=["## 执行顺序局限补充", "", "本补充不修改任何绩效、交易、门槛或原报告，只明确列出已有指标中的执行歧义。", ""]
for a in report["accounts"]:
    with (folder/(a["account"]+"_trades.csv")).open(encoding="utf-8-sig",newline="") as f:trades=list(csv.DictReader(f))
    flagged=[t for t in trades if t["funding_timing_ambiguous"]=="True"]
    evidence.append({"account":a["account"],"execution_ambiguities":a["execution_ambiguities"],"liquidation_proxy_events":a["liquidation_proxy_events"],"funding_timing_flagged_trades":flagged})
    lines.append(f"- {a['account']}：执行顺序歧义{a['execution_ambiguities']}次，代理强平{a['liquidation_proxy_events']}次，资金费时点标记交易{len(flagged)}笔。")
lines += ["", "四小时版基础和压力场景各有一笔止损与资金费结算落在同一分钟。既定路径按资金费先、止损后入账，分钟OHLC不足以确定真实先后，因此这些收益只是该路径下的近似，不代表精确历史结算。该版未通过执行门槛；其年化提升、分期与重采样条件也未达标，不能通过忽略歧义来宣称改进。收缩突破版没有此类标记，但扣费收益为负。本次不追加回测或改动路径。", ""]
note="\n".join(lines)
preserve(folder/"execution_timing_note.md",note.encode())
preserve(folder/"execution_timing_note.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
full=(folder/"comparison.md").read_text(encoding="utf-8")+"\n"+note
preserve(folder/"comparison_with_execution_note.md",full.encode())
preserve(folder/"source/disclose_r007_timing.py",Path(__file__).read_bytes())
preserve(folder/"source/report_seventh_round_execution_disclosure.py",(ROOT/"scripts/report_seventh_round.py").read_bytes())
(ROOT/"docs/第七轮入场机制结果.md").write_text(full,encoding="utf-8")
path=ROOT/"artifacts/backtests/latest.json"
body=path.read_bytes();preserve(ROOT/"artifacts/backtests/pointer_history"/(sha(body)+".json"),body)
pointer=json.loads(body);pointer["report"]=(folder/"comparison_with_execution_note.md").relative_to(ROOT).as_posix()
pointer["execution_timing_disclosure"]=(folder/"execution_timing_note.json").relative_to(ROOT).as_posix()
path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({e["account"]:e["execution_ambiguities"] for e in evidence}))
