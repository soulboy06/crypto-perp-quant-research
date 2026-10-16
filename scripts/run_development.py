"""Run the four fixed, explicitly labelled development screens."""
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.engine import run
if __name__=="__main__":
    folder,report=run(ROOT)
    print(folder)
    for account in report["accounts"]:
        print(account["account"],account["ending_equity"],account["net_return"],account["round_trips"])
