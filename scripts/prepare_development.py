"""Prepare only the frozen warmup/development data partition."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from perpquant.full_data import prepare
if __name__ == "__main__":
    _, report = prepare(ROOT)
    raise SystemExit(0 if report["complete_archive_coverage"] else 2)
