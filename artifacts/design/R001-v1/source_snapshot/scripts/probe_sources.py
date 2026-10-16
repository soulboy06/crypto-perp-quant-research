"""Public rule snapshot and HEAD-only archive availability probe; no price history download."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.binance.vision/data/futures/um"
RULES = "https://fapi.binance.com/fapi/v1/exchangeInfo"


def fetch(item):
    name, url, method = item
    result = {"name": name, "url": url, "method": method}
    try:
        request = Request(url, method=method, headers={"User-Agent": "perpquant-research/0.1"})
        with urlopen(request, timeout=15) as response:
            result.update(status=response.status, content_length=response.headers.get("Content-Length"))
            if method == "GET":
                body = response.read()
                payload = json.loads(body)
                required = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL"}
                symbols = {entry["symbol"]: entry for entry in payload["symbols"]}
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    entry = symbols[symbol]
                    if (entry["contractType"] != "PERPETUAL" or entry["marginAsset"] != "USDT"
                            or not required.issubset({rule["filterType"] for rule in entry["filters"]})):
                        raise ValueError(f"Unexpected rules for {symbol}")
                destination = ROOT / "artifacts/rules/exchange_info_raw.json"
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(body)
                selected = {"source": url, "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
                            "source_sha256": hashlib.sha256(body).hexdigest(),
                            "scope": "current_snapshot_not_historical",
                            "symbols": {symbol: symbols[symbol] for symbol in ("BTCUSDT", "ETHUSDT")}}
                (destination.parent / "exchange_rules.json").write_text(
                    json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8")
                result["verified_symbols"] = ["BTCUSDT", "ETHUSDT"]
                result["sha256"] = selected["source_sha256"]
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, HTTPError):
            result["status"] = exc.code
    return result


def main():
    requests = [("exchange_info", RULES, "GET")]
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for kind in ("klines", "markPriceKlines"):
            url = f"{BASE}/daily/{kind}/{symbol}/1m/{symbol}-1m-2023-01-01.zip"
            requests.append((f"{symbol}_{kind}", url, "HEAD"))
        url = f"{BASE}/monthly/fundingRate/{symbol}/{symbol}-fundingRate-2023-01.zip"
        requests.append((f"{symbol}_fundingRate", url, "HEAD"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(fetch, requests))
    report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
              "history_records_downloaded": False, "holdout_history_accessed": False,
              "note": "Archive HEAD responses only establish object availability, not integrity or coverage.",
              "results": results}
    destination = ROOT / "artifacts/setup/source_probe.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if any("error" in entry for entry in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
