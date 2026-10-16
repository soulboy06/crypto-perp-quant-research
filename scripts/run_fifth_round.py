from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.engine import run
folder,report=run(ROOT,variant="R005-v1")
print(folder)
for a in report["accounts"]:print(a["account"],a["cagr"],a["max_adverse_bound_drawdown"],a["round_trips"])
