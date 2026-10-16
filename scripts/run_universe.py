from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.universe_engine import run
from perpquant.universe_report import report
if __name__=='__main__':
    folder,result=run(ROOT)
    report(ROOT,folder)
    print(folder)
    for x in result['accounts']:print(x['account'],x['cagr'],x['max_adverse_bound_drawdown'],x['round_trips'])
