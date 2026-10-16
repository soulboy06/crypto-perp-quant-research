"""Public REST cross-check of the fixed Jan-2023 archive sample."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.archive import download_funding_sample

if __name__ == "__main__":
    manifest, report = download_funding_sample(ROOT)
    print(json.dumps({"manifest": str(manifest), **report}, ensure_ascii=False, indent=2))
    raise SystemExit(1 if any("error" in item for item in report["results"]) else 0)
