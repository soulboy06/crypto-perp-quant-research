"""Bounded, resumable monthly development data preparation. No holdout requests."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import csv
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError, URLError
from uuid import uuid4
import zipfile
from .archive import BASE, allowed_development, get_bytes, preserve, verify_checksum
from .config import load, require
from .quality import candles, funding, crosscheck_funding, utc_us


@dataclass(frozen=True)
class MonthlySpec:
    symbol: str
    kind: str
    period: str

    def interval(self):
        require(re.fullmatch(r"\d{4}-\d{2}", self.period) is not None, "Invalid monthly period")
        start = datetime.fromisoformat(self.period + "-01").replace(tzinfo=timezone.utc)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        return start, end

    def url(self):
        prefix = f"{BASE}/monthly/{self.kind}/{self.symbol}"
        if self.kind == "fundingRate":
            return f"{prefix}/{self.symbol}-fundingRate-{self.period}.zip"
        return f"{prefix}/1m/{self.symbol}-1m-{self.period}.zip"


def specs_for(config):
    start = datetime.fromisoformat(config["data"]["warmup_start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(config["data"]["development_end"].replace("Z", "+00:00"))
    require(start.day == end.day == 1 and start.hour == end.hour == 0, "Monthly boundaries required")
    result = []
    while start < end:
        for symbol in config["market"]["symbols"]:
            for kind in ("klines", "markPriceKlines", "fundingRate"):
                spec = MonthlySpec(symbol, kind, start.strftime("%Y-%m"))
                allowed_development(config, spec)
                result.append(spec)
        start = start.replace(year=start.year+1, month=1) if start.month == 12 else start.replace(month=start.month+1)
    return result


def fetch(url, limit):
    for attempt in range(3):
        try:
            return get_bytes(url, limit)
        except HTTPError as exc:
            if exc.code not in (408, 429, 500, 502, 503, 504) or attempt == 2:
                raise
        except (URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise
        time.sleep(1 + attempt * 2)


def unpack(body, member):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        items = archive.infolist()
        require(len(items) == 1 and items[0].filename == member and not items[0].is_dir(), "Unexpected ZIP members")
        require(items[0].file_size <= 40_000_000, "CSV exceeds monthly size bound")
        return archive.read(items[0])


def sha(body):
    return hashlib.sha256(body).hexdigest()


def relative(root, path):
    return path.relative_to(root).as_posix()


def prepare_one(root, config, spec):
    allowed_development(config, spec)  # Before every request, including a cache hit.
    record = {**asdict(spec), "frequency": "monthly", "url": spec.url(),
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        name = spec.url().rsplit("/", 1)[1]
        checksum = fetch(spec.url() + ".CHECKSUM", 4096)
        match = re.fullmatch(rb"([a-fA-F0-9]{64})\s+\*?([^\s]+)\s*", checksum)
        require(match is not None and match[2].decode("ascii") == name, "Invalid official checksum record")
        digest = match[1].decode("ascii").lower()
        folder = root / "data/raw" / spec.kind / spec.symbol / spec.period / digest
        path = folder / name
        cached = path.is_file()
        body = path.read_bytes() if cached else fetch(spec.url(), 20_000_000)
        verify_checksum(body, checksum, name)
        preserve(path, body)
        checksum_path = folder / (sha(checksum) + ".CHECKSUM")
        preserve(checksum_path, checksum)
        csv_body = unpack(body, name[:-4] + ".csv")
        start, end = map(utc_us, spec.interval())
        if spec.kind == "fundingRate":
            parsed, unit = funding(csv_body, start, end)
            # A monthly slice is strictly below the public endpoint's pagination limit.
            api_url = (f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={spec.symbol}"
                       f"&startTime={start//1000}&endTime={end//1000-1}&limit=1000")
            api_body = fetch(api_url, 2_000_000)
            payload = json.loads(api_body)
            require(isinstance(payload, list) and 0 < len(payload) < 1000, "Funding REST pagination/response invalid")
            comparison = crosscheck_funding(parsed, payload, spec.symbol, start, end)
            api_path = root / "data/raw/funding_api" / spec.symbol / spec.period / (sha(api_body) + ".json")
            preserve(api_path, api_body)
            record.update(funding_rest=comparison, funding_rest_path=relative(root, api_path),
                          funding_rest_sha256=sha(api_body), funding_rest_url=api_url)
        else:
            parsed, unit = candles(csv_body, start, end, mark=spec.kind == "markPriceKlines")
        # Canonical schema preserves decimal prices as strings, not binary floats.
        text_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(text_buffer, fieldnames=list(parsed[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(parsed)
        normalized = gzip.compress(text_buffer.getvalue().encode("utf-8"), mtime=0)
        normalized_path = (root / "data/validated" / spec.kind / spec.symbol / spec.period /
                           (sha(normalized) + ".csv.gz"))
        preserve(normalized_path, normalized)
        time_key = "event_time_us" if spec.kind == "fundingRate" else "open_time_us"
        record.update(checksum_verified=True, cached_zip=cached, path=relative(root, path), sha256=digest,
                      bytes=len(body), checksum_path=relative(root, checksum_path), csv_sha256=sha(csv_body),
                      rows=len(parsed), timestamp_unit=unit, first_time_us=parsed[0][time_key],
                      last_time_us=parsed[-1][time_key], normalized_path=relative(root, normalized_path),
                      normalized_sha256=sha(normalized), audit_passed=True)
    except Exception as exc:
        record.update(error=f"{type(exc).__name__}: {exc}", audit_passed=False)
    record["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    return record


def prepare(root, workers=4):
    root = Path(root).resolve()
    require(type(workers) is int and 1 <= workers <= 4, "Worker bound is 1..4")
    config_path = root / "configs/first_round.toml"
    config = load(config_path)
    specs = specs_for(config)
    run_id = "DEVELOPMENT-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    directory = root / "artifacts/data/full_runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    source_hashes = {}
    for filename in ("full_data.py", "quality.py", "archive.py", "config.py"):
        body = Path(__file__).with_name(filename).read_bytes()
        preserve(directory / "source" / filename, body)
        source_hashes[filename] = sha(body)
    base = {"run_id": run_id, "scope": "warmup_and_development_only", "expected_archives": len(specs),
            "config_sha256": sha(config_path.read_bytes()), "source_sha256": source_hashes,
            "range_start": config["data"]["warmup_start"], "range_end_exclusive": config["data"]["development_end"],
            "holdout_history_requested": False, "historical_margin_rules_verified": False,
            "strategy_backtest_executed": False}
    preserve(directory / "started.json", json.dumps(base, indent=2).encode())
    results = []
    print(json.dumps({"run_id": run_id, "archives": len(specs)}, ensure_ascii=False), flush=True)
    with (directory / "progress.jsonl").open("x", encoding="utf-8") as journal:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {pool.submit(prepare_one, root, config, spec): spec for spec in specs}
            for future in as_completed(pending):
                record = future.result()
                results.append(record)
                journal.write(json.dumps(record, ensure_ascii=False) + "\n")
                journal.flush()
                if len(results) % 6 == 0 or not record["audit_passed"]:
                    print(json.dumps({"completed": len(results), "total": len(specs),
                                      "failed": sum(not r["audit_passed"] for r in results),
                                      "latest": [record["symbol"], record["kind"], record["period"]],
                                      "error": record.get("error")}, ensure_ascii=False), flush=True)
    results.sort(key=lambda r: (r["period"], r["symbol"], r["kind"]))
    failures = [r for r in results if not r["audit_passed"]]
    report = {**base, "completed_at_utc": datetime.now(timezone.utc).isoformat(), "results": results,
              "complete_archive_coverage": len(results) == len(specs) and not failures,
              "failed_archives": len(failures), "minute_rows": sum(r.get("rows", 0) for r in results if r["kind"] != "fundingRate"),
              "funding_rows": sum(r.get("rows", 0) for r in results if r["kind"] == "fundingRate"),
              "bytes": sum(r.get("bytes", 0) for r in results),
              "missing_exact_settlement_marks": sum(r.get("funding_rest", {}).get("missing_exact_settlement_marks", 0) for r in results),
              "execution_ready": False}
    path = directory / "manifest.json"
    preserve(path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    print(json.dumps({"manifest": str(path), **{k: report[k] for k in ("complete_archive_coverage", "failed_archives", "minute_rows", "funding_rows", "bytes")}}), flush=True)
    return path, report
