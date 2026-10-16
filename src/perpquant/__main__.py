import argparse
from dataclasses import asdict
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from .config import load, readiness
from .risk import EntryRequest, preview


def main():
    parser = argparse.ArgumentParser(description="Offline perpetual-futures design checks")
    parser.add_argument("command", choices=["check-config", "check-readiness", "size-preview"])
    parser.add_argument("--config", default="configs/first_round.toml")
    parser.add_argument("--leverage-cap", default=None)
    args = parser.parse_args()
    path = Path(args.config).resolve()
    try:
        config = load(path)
        if args.command == "check-config":
            result = {"design_id": config["design_id"], "config_valid": True,
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                      "note": "Configuration invariants only; not a backtest or execution approval"}
        elif args.command == "check-readiness":
            missing = readiness(config, path.parent.parent)
            result = {"ready": not missing, "missing": missing,
                      "note": "Setup inventory only; evidence must be reviewed separately"}
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 2 if missing else 0
        else:
            request = EntryRequest(**{key: Decimal(value) for key, value in {
                "equity": "100", "free_collateral": "100", "reference_price": "100",
                "stop_price": "98", "step_size": "0.01", "min_qty": "0.01",
                "min_notional": "5"}.items()})
            result = {"synthetic_example": True, "inputs": asdict(request),
                      "result": preview(config, request, args.leverage_cap)}
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0
    except (ValueError, KeyError, OSError, TypeError, IndexError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
