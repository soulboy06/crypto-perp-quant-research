from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.universe_data import prepare
if __name__=='__main__':
    path,result=prepare(ROOT)
    print(path)
    raise SystemExit(0 if result['complete_development_coverage'] else 2)
