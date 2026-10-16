import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.reconcile import reconcile
if __name__=="__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("manifest")
    args=parser.parse_args()
    _,report=reconcile(ROOT,args.manifest)
    raise SystemExit(0 if report["complete_development_coverage"] else 2)
