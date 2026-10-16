from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.universe_report import report
if __name__=='__main__':
    path,details=report(ROOT)
    print(path)
