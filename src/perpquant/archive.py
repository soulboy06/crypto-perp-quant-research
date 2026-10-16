"""Version-preserving public UM archive samples, guarded before network access."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.request import Request, urlopen
from uuid import uuid4
import io
import zipfile
from .config import load, require

BASE = "https://data.binance.vision/data/futures/um"


@dataclass(frozen=True)
class ArchiveSpec:
    symbol: str
    kind: str
    period: str

    def interval(self):
        pattern = r"\d{4}-\d{2}" if self.kind == "fundingRate" else r"\d{4}-\d{2}-\d{2}"
        require(re.fullmatch(pattern, self.period) is not None, "Invalid archive period")
        start = datetime.fromisoformat(self.period + ("-01" if self.kind == "fundingRate" else ""))
        start = start.replace(tzinfo=timezone.utc)
        if self.kind == "fundingRate":
            end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        else:
            from datetime import timedelta
            end = start + timedelta(days=1)
        return start, end

    def url(self):
        if self.kind == "fundingRate":
            return f"{BASE}/monthly/{self.kind}/{self.symbol}/{self.symbol}-fundingRate-{self.period}.zip"
        return f"{BASE}/daily/{self.kind}/{self.symbol}/1m/{self.symbol}-1m-{self.period}.zip"


def allowed_development(config, spec):
    require(spec.symbol in config["market"]["symbols"], "Symbol outside frozen universe")
    require(spec.kind in ("klines", "markPriceKlines", "fundingRate"), "Unsupported archive")
    start, end = spec.interval()
    warmup = datetime.fromisoformat(config["data"]["warmup_start"].replace("Z", "+00:00"))
    cutoff = datetime.fromisoformat(config["data"]["development_end"].replace("Z", "+00:00"))
    require(warmup <= start < end <= cutoff, "Archive outside allowed development partition")


def get_bytes(url, limit):
    request = Request(url, headers={"User-Agent": "perpquant-research/0.2"})
    with urlopen(request, timeout=30) as response:
        require(response.status == 200, "Non-200 archive response")
        body = response.read(limit + 1)
        require(len(body) <= limit, "Archive download size limit exceeded")
        return body


def verify_checksum(body, checksum, expected_name):
    lines = checksum.decode("ascii").strip().splitlines()
    require(len(lines) == 1, "Expected one official checksum record")
    match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?([^\s]+)", lines[0])
    require(match is not None and match[2] == expected_name, "Checksum filename mismatch")
    actual = hashlib.sha256(body).hexdigest()
    require(actual == match[1].lower(), "Official checksum mismatch")
    return actual


def preserve(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        require(path.read_bytes() == body, "Refusing to replace preserved bytes")
        return
    with path.open("xb") as stream:
        stream.write(body)


def csv_bytes(body, expected_member):
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        members = archive.infolist()
        require(len(members) == 1 and members[0].filename == expected_member,
                "Unexpected ZIP members; no extraction performed")
        require(members[0].file_size <= 20_000_000, "Expanded CSV exceeds size bound")
        require(not members[0].is_dir(), "Expected CSV file")
        return archive.read(members[0])


def download_one(root, config, spec):
    # This check precedes both checksum and ZIP requests.
    allowed_development(config, spec)
    result = {**asdict(spec), "url": spec.url(), "checksum_url": spec.url() + ".CHECKSUM",
              "started_at_utc": datetime.now(timezone.utc).isoformat()}
    try:
        checksum = get_bytes(result["checksum_url"], 4096)
        body = get_bytes(result["url"], 5_000_000)
        digest = hashlib.sha256(body).hexdigest()
        folder = root / "data/raw" / spec.kind / spec.symbol / spec.period / digest
        path = folder / spec.url().rsplit("/", 1)[1]
        preserve(path, body)
        # Preserve a differing CHECKSUM version separately instead of overwriting.
        checksum_hash = hashlib.sha256(checksum).hexdigest()
        checksum_path = folder / (checksum_hash + ".CHECKSUM")
        preserve(checksum_path, checksum)
        result.update(sha256=digest, bytes=len(body), path=str(path.relative_to(root)).replace("\\", "/"),
                      checksum_path=str(checksum_path.relative_to(root)).replace("\\", "/"))
        verify_checksum(body, checksum, path.name)
        member = path.stem + ".csv"
        csv_body = csv_bytes(body, member)
        result.update(checksum_verified=True, csv_member=member,
                      csv_sha256=hashlib.sha256(csv_body).hexdigest(),
                      preview_lines=csv_body.decode("utf-8-sig").splitlines()[:3])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    result["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
    return result


def sample_specs():
    return [ArchiveSpec(symbol, kind, "2023-01" if kind == "fundingRate" else "2023-01-01")
            for symbol in ("BTCUSDT", "ETHUSDT")
            for kind in ("klines", "markPriceKlines", "fundingRate")]


def snapshot_source(root):
    body = Path(__file__).read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    preserve(root / "artifacts/data/source_snapshots" / digest / "archive.py", body)
    return digest


def download_sample(root):
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    source_digest = snapshot_source(root)
    specs = sample_specs()
    for spec in specs:
        allowed_development(config, spec)
    run_id = "SAMPLE-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda spec: download_one(root, config, spec), specs))
    report = {"run_id": run_id, "scope": "development_sample_only",
              "config_sha256": hashlib.sha256((root / "configs/first_round.toml").read_bytes()).hexdigest(),
              "script_sha256": source_digest,
              "holdout_history_requested": False, "complete_development_coverage": False,
              "results": results}
    manifest = root / "artifacts/data/manifests" / (run_id + ".json")
    preserve(manifest, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest, report


def download_funding_sample(root):
    """Bounded Jan-2023 public REST cross-check, preserving the raw response."""
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    source_digest = snapshot_source(root)
    results = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        spec = ArchiveSpec(symbol, "fundingRate", "2023-01")
        allowed_development(config, spec)
        start, end = spec.interval()
        url = ("https://fapi.binance.com/fapi/v1/fundingRate?symbol=" + symbol
               + f"&startTime={int(start.timestamp()*1000)}&endTime={int(end.timestamp()*1000)-1}&limit=1000")
        result = {"symbol": symbol, "kind": "fundingRate", "period": spec.period, "url": url}
        try:
            body = get_bytes(url, 2_000_000)
            payload = json.loads(body)
            require(isinstance(payload, list) and 0 < len(payload) < 1000,
                    "Funding response empty, non-list, or pagination required")
            require(all(row.get("symbol") == symbol for row in payload), "Wrong funding symbol")
            digest = hashlib.sha256(body).hexdigest()
            path = root / "data/raw/funding_api" / symbol / spec.period / (digest + ".json")
            preserve(path, body)
            result.update(path=str(path.relative_to(root)).replace("\\", "/"), sha256=digest,
                          rows=len(payload), example=payload[0])
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        results.append(result)
    run_id = "FUNDING-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    report = {"run_id": run_id, "scope": "development_sample_only",
              "holdout_history_requested": False, "complete_development_coverage": False,
              "script_sha256": source_digest, "results": results}
    manifest = root / "artifacts/data/manifests" / (run_id + ".json")
    preserve(manifest, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest, report


def download_boundary_sample(root):
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    source_digest = snapshot_source(root)
    specs = [ArchiveSpec(symbol, "markPriceKlines", "2022-12-31") for symbol in ("BTCUSDT", "ETHUSDT")]
    for spec in specs:
        allowed_development(config, spec)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda spec: download_one(root, config, spec), specs))
    run_id = "BOUNDARY-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    report = {"run_id": run_id, "scope": "development_sample_only", "purpose": "causal_funding_mark_boundary",
              "holdout_history_requested": False, "complete_development_coverage": False,
              "script_sha256": source_digest, "results": results}
    manifest = root / "artifacts/data/manifests" / (run_id + ".json")
    preserve(manifest, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return manifest, report
