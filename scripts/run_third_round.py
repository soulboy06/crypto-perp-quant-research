from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.engine import run
if __name__=="__main__":
    folder,report=run(ROOT,variant="R003-v1")
    print(folder)
    for account in report["accounts"]:
        print(account["account"],account["net_return"],account["cagr"],account["max_adverse_bound_drawdown"],account["round_trips"])
