import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.quality import audit_funding_proxy

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_manifest")
    parser.add_argument("boundary_manifest")
    args = parser.parse_args()
    path, report = audit_funding_proxy(ROOT, args.archive_manifest, args.boundary_manifest)
    print(json.dumps({"report": str(path), **report}, ensure_ascii=False, indent=2, default=str))
