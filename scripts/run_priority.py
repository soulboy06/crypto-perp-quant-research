"""Run the one preregistered R012 family; no parameter-search CLI."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.priority_run import run

if __name__ == '__main__':
    try:
        run(ROOT)
    except ValueError as exc:
        print('R012 refused: ' + str(exc), file=sys.stderr)
        sys.exit(2)
