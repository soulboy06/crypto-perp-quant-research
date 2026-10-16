"""Preserve original report; make UTC labels explicit without changing performance."""
import csv
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.quality import under
folder=under(ROOT,sys.argv[1],"artifacts/backtests")
original=json.loads((folder/"comparison.json").read_text(encoding="utf-8"))
text=(folder/"comparison.md").read_text(encoding="utf-8")
for c in original["comparisons"]:
    with (folder/(c["account"]+"_trades.csv")).open(encoding="utf-8-sig",newline="") as stream:trades=list(csv.DictReader(stream))
    fixed=datetime.fromtimestamp(max(int(t["exit_time_us"]) for t in trades)/1_000_000,timezone.utc).isoformat()
    text=text.replace(c["last_trade_exit_utc"],fixed)
    c["last_trade_exit_utc"]=fixed
original["report_timestamp_correction"]="Last trade exit now explicitly uses UTC; original naive local timestamp report retained. No financial result changed."
text+="\n报告记录修正：最后交易退出时间明确转换为 UTC 并附 +00:00；原始无时区显示报告保留。绩效、门槛、交易及所有资金数字未改。\n"
preserve(folder/"comparison_utc_corrected.json",json.dumps(original,ensure_ascii=False,indent=2).encode())
preserve(folder/"comparison_utc_corrected.md",text.encode())
preserve(folder/"source/correct_r006_report_timezone.py",Path(__file__).read_bytes())
preserve(folder/"source/report_sixth_round_timezone_fixed.py",(ROOT/"scripts/report_sixth_round.py").read_bytes())
(ROOT/"docs/第六轮独立信号结果.md").write_text(text,encoding="utf-8")
pointer_path=ROOT/"artifacts/backtests/latest.json"
body=pointer_path.read_bytes()
preserve(ROOT/"artifacts/backtests/pointer_history"/(hashlib.sha256(body).hexdigest()+".json"),body)
pointer=json.loads(body)
pointer["report"]=(folder/"comparison_utc_corrected.md").relative_to(ROOT).as_posix()
pointer["report_timestamp_correction"]=True
pointer_path.write_text(json.dumps(pointer,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps({c["account"]:c["last_trade_exit_utc"] for c in original["comparisons"]}))
