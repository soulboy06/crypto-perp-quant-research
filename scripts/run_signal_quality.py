from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.signal_quality import run
from perpquant.signal_quality_report import report
if __name__=='__main__':
    folder,summary=run(ROOT)
    path,result=report(ROOT,folder)
    print(path)
    for a in summary['accounts']:print(a['account'],a['cagr'],a['max_adverse_bound_drawdown'],a['round_trips'])
