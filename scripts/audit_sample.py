import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.quality import audit_sample

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive_manifest")
    parser.add_argument("funding_manifest")
    args = parser.parse_args()
    path, report = audit_sample(ROOT, args.archive_manifest, args.funding_manifest)
    print(json.dumps({"report": str(path), **report}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["sample_schema_and_coverage_passed"] else 1)
