from pathlib import Path
import argparse
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from perpquant.mark_proxy import prepare
from perpquant.proxy_sensitivity import run_both
from perpquant.funding_rest_recovery import recover

if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--prepare-data', action='store_true')
    p.add_argument('--recover-funding-rest', action='store_true')
    args = p.parse_args()
    try:
        if args.prepare_data:
            _, manifest = prepare(ROOT)
            sys.exit(0 if all(x['complete_with_explicit_proxy'] for x in manifest['scenarios'].values()) else 2)
        if args.recover_funding_rest:
            recover(ROOT)
            sys.exit(0)
        run_both(ROOT)
    except ValueError as exc:
        print('Mark-proxy experiment refused or stopped: ' + str(exc), file=sys.stderr)
        sys.exit(2)
