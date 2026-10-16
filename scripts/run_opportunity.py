"""Run the three independently preregistered R013 cases, with no tuning options."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.opportunity_run import run

if __name__ == '__main__':
    try:
        run(ROOT)
    except ValueError as exc:
        print('R013 refused: ' + str(exc), file=sys.stderr)
        sys.exit(2)
