"""R011 regime filter run. Default is the extension window; reproduction is diagnostic."""
from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.regime_run import run

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope', choices=['extension', 'reproduction'], default='extension')
    parser.add_argument('--proxy-scenario', choices=['spot', 'ffill'], default='spot',
                        help='Extension only: run both scenarios before interpreting results')
    args = parser.parse_args()
    try:
        run(ROOT, args.scope, proxy_scenario=args.proxy_scenario)
    except ValueError as exc:
        print('R011 refused or stopped: ' + str(exc), file=sys.stderr)
        sys.exit(2)
