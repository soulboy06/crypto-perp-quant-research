"""Default is the approved extension window; reproduction is explicitly diagnostic."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.phase1_data import prepare
from perpquant.phase1_run import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--prepare-data', action='store_true')
    parser.add_argument('--scope', choices=['extension', 'reproduction'], default='extension')
    args = parser.parse_args()
    try:
        if args.prepare_data:
            _, manifest = prepare(ROOT)
            sys.exit(0 if manifest['complete_extension_coverage'] else 2)
        run(ROOT, args.scope)
    except ValueError as exc:
        print('Phase 1 refused or stopped: ' + str(exc), file=sys.stderr)
        sys.exit(2)
