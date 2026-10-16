"""Strict sample schema/coverage audit; no filling, timestamp rounding, or trading."""
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tomllib
from uuid import uuid4
from .archive import ArchiveSpec, allowed_development, csv_bytes, preserve, verify_checksum
from .config import decimal_value as D, load, require

KLINE_HEADER = ["open_time", "open", "high", "low", "close", "volume", "close_time",
                "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore"]
FUNDING_HEADER = ["calc_time", "funding_interval_hours", "last_funding_rate"]
MINUTE_US = 60_000_000


def utc_us(stamp):
    return int(stamp.timestamp()) * 1_000_000


def rows(body, expected):
    data = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
    require(bool(data), "Empty CSV")
    if data[0] == expected:
        data = data[1:]
    else:
        require(data[0][0].isdigit(), "Unknown CSV header")
    require(data and all(len(row) == len(expected) for row in data), "Invalid column count")
    return data


def timestamp(raw, start_us, end_us):
    require(isinstance(raw, str) and raw.isdigit(), "Non-integer timestamp")
    value = int(raw)
    matches = [(value * 1000, "ms"), (value, "us")]
    matches = [(value, unit) for value, unit in matches if start_us <= value < end_us]
    require(len(matches) == 1, "Timestamp outside expected interval or unit ambiguous")
    return matches[0]


def candles(body, start_us, end_us, mark=False):
    data = rows(body, KLINE_HEADER)
    require((end_us - start_us) % MINUTE_US == 0, "Non-minute interval")
    require(len(data) == (end_us - start_us) // MINUTE_US, "Missing or extra minute bars")
    result, units = [], set()
    for index, row in enumerate(data):
        opening, unit = timestamp(row[0], start_us, end_us)
        units.add(unit)
        require(opening == start_us + index * MINUTE_US, "Duplicate, gap, or unordered minute")
        closing, close_unit = timestamp(row[6], start_us, end_us)
        require(close_unit == unit and closing == opening + MINUTE_US - (1000 if unit == "ms" else 1),
                "Invalid minute close timestamp")
        op, high, low, close = [D(value) for value in row[1:5]]
        require(0 < low <= min(op, close) <= max(op, close) <= high, "Invalid OHLC prices")
        if not mark:
            volume, quote, taker, taker_quote = [D(row[i]) for i in (5, 7, 9, 10)]
            require(0 <= taker <= volume and 0 <= taker_quote <= quote, "Invalid trade volumes")
            require(row[8].isdigit(), "Invalid trade count")
        result.append({"open_time_us": opening, "available_time_us": opening + MINUTE_US,
                       "open": op, "high": high, "low": low, "close": close})
    require(len(units) == 1, "Mixed timestamp units in one archive")
    return result, units.pop()


def funding(body, start_us, end_us):
    data = rows(body, FUNDING_HEADER)
    result, units = [], set()
    for row in data:
        event, unit = timestamp(row[0], start_us, end_us)
        units.add(unit)
        require(row[1].isdigit() and 1 <= int(row[1]) <= 24, "Invalid funding interval")
        hours, rate = int(row[1]), D(row[2])
        require(abs(rate) < 1, "Invalid funding rate")
        if result:
            require(event > result[-1]["event_time_us"], "Duplicate or unordered funding event")
            expected = hours * 3_600_000_000
            require(abs(event - result[-1]["event_time_us"] - expected) <= MINUTE_US,
                    "Funding schedule gap or unexpected interval; investigate, do not fill")
        result.append({"event_time_us": event, "interval_hours": hours, "rate": rate})
    require(len(units) == 1, "Mixed funding timestamp units")
    require(result[0]["event_time_us"] - start_us <= MINUTE_US, "Funding start boundary missing")
    require(abs(end_us - result[-1]["event_time_us"] - result[-1]["interval_hours"] * 3_600_000_000)
            <= MINUTE_US, "Funding end boundary missing")
    return result, units.pop()


def crosscheck_funding(archive_rows, payload, symbol, start_us, end_us):
    api = {}
    for row in payload:
        require(row["symbol"] == symbol, "Unexpected REST funding symbol")
        event, _ = timestamp(str(row["fundingTime"]), start_us, end_us)
        require(event not in api, "Duplicate REST funding event")
        raw_mark = row.get("markPrice")
        mark = None if raw_mark in (None, "") else D(raw_mark)
        require(mark is None or mark > 0, "Invalid settlement mark price")
        api[event] = {"rate": D(row["fundingRate"]), "mark": mark}
    require(set(api) == {row["event_time_us"] for row in archive_rows}, "Archive/REST event times differ")
    require(all(api[row["event_time_us"]]["rate"] == row["rate"] for row in archive_rows),
            "Archive/REST funding rates differ")
    return {"matching_events": len(api), "missing_exact_settlement_marks": sum(
        row["mark"] is None for row in api.values())}


def under(root, relative, expected_parent):
    target = (root / relative).resolve()
    require(target.is_relative_to((root / expected_parent).resolve()), "Input path outside expected partition")
    return target


def causal_mark_proxy(event_time_us, closed_bars, maximum_age_us=MINUTE_US):
    eligible = [row for row in closed_bars if row["available_time_us"] <= event_time_us]
    require(bool(eligible), "No closed mark-price candle before funding event")
    latest = max(eligible, key=lambda row: row["available_time_us"])
    require(sum(row["available_time_us"] == latest["available_time_us"] for row in eligible) == 1,
            "Duplicate eligible mark price")
    age = event_time_us - latest["available_time_us"]
    require(0 <= age <= maximum_age_us and latest["close"] > 0, "Mark-price proxy stale or invalid")
    return {"proxy_mark": latest["close"], "source_available_us": latest["available_time_us"],
            "age_us": age, "exact_settlement_mark": False}


def audit_funding_proxy(root, archive_manifest, boundary_manifest):
    """Day-level valuation availability, not a claim of precise funding charges."""
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    policy_path = root / "configs/data_policy.toml"
    policy = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    require(policy["parent_design"] == config["design_id"], "Data policy binding differs")
    require(policy["missing_settlement_mark"] == "last_closed_mark_1m"
            and policy["allow_future_candle_close"] is False and policy["allow_zero_price_fill"] is False
            and policy["allow_missing_mark_skip"] is False and policy["price_proxy_is_exact"] is False,
            "Unsupported data policy")
    require(type(policy["maximum_proxy_age_seconds"]) is int and
            0 < policy["maximum_proxy_age_seconds"] <= 60, "Invalid mark-price age")
    report_paths = [under(root, name, "artifacts/data/manifests") for name in (archive_manifest, boundary_manifest)]
    entries = []
    for path in report_paths:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        require(manifest["scope"] == "development_sample_only", "Wrong manifest scope")
        entries.extend(manifest["results"])
    mark_bars, events, seen = {}, {}, set()
    for item in entries:
        if item["kind"] not in ("markPriceKlines", "fundingRate"):
            continue
        spec = ArchiveSpec(item["symbol"], item["kind"], item["period"])
        allowed_development(config, spec)
        require((spec.symbol, spec.kind, spec.period) not in seen, "Duplicate sample partition")
        seen.add((spec.symbol, spec.kind, spec.period))
        start, end = map(utc_us, spec.interval())
        parent = f"data/raw/{spec.kind}/{spec.symbol}/{spec.period}"
        path = under(root, item["path"], parent)
        checksum_path = under(root, item["checksum_path"], parent)
        body = path.read_bytes()
        digest = verify_checksum(body, checksum_path.read_bytes(), spec.url().rsplit("/", 1)[1])
        require(digest == item["sha256"], "Partition hash differs")
        csv_body = csv_bytes(body, spec.url().rsplit("/", 1)[1][:-4] + ".csv")
        if spec.kind == "markPriceKlines":
            parsed, _ = candles(csv_body, start, end, mark=True)
            mark_bars.setdefault(spec.symbol, []).extend(parsed)
        else:
            events[spec.symbol], _ = funding(csv_body, start, end)
    start, end = map(utc_us, ArchiveSpec("BTCUSDT", "klines", "2023-01-01").interval())
    result = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        require(symbol in mark_bars and symbol in events, "Required symbol data missing")
        for event in events[symbol]:
            if start <= event["event_time_us"] < end:
                result.append({"symbol": symbol, "event_time_us": event["event_time_us"],
                               **causal_mark_proxy(event["event_time_us"], mark_bars[symbol],
                                                   policy["maximum_proxy_age_seconds"] * 1_000_000)})
    require(len(result) == 6, "Unexpected sample-day funding count; investigate schedule")
    report = {"run_id": "VALUATION-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8],
              "scope": "funding_proxy_availability_for_2023_01_01_only", "events_checked": len(result),
              "policy_id": policy["policy_id"], "policy_sha256": hashlib.sha256(policy_path.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "manifest_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in report_paths},
              "holdout_history_read": False, "exact_settlement_prices_recovered": False,
              "full_development_coverage": False, "events": result}
    path = root / "artifacts/data/quality" / (report["run_id"] + ".json")
    preserve(path, json.dumps(report, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    return path, report


def audit_sample(root, archive_manifest, funding_manifest):
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    archive_path = under(root, archive_manifest, "artifacts/data/manifests")
    api_path = under(root, funding_manifest, "artifacts/data/manifests")
    archive_report = json.loads(archive_path.read_text(encoding="utf-8"))
    api_report = json.loads(api_path.read_text(encoding="utf-8"))
    require(archive_report["scope"] == api_report["scope"] == "development_sample_only", "Wrong manifest scope")
    require(archive_report["config_sha256"] == hashlib.sha256(
        (root / "configs/first_round.toml").read_bytes()).hexdigest(), "Archive configuration binding differs")
    expected = {(symbol, kind) for symbol in ("BTCUSDT", "ETHUSDT")
                for kind in ("klines", "markPriceKlines", "fundingRate")}
    require(len(archive_report["results"]) == 6 and
            {(item["symbol"], item["kind"]) for item in archive_report["results"]} == expected,
            "Sample manifest must contain exactly the six fixed archives")
    require(len(api_report["results"]) == 2 and
            {item["symbol"] for item in api_report["results"]} == {"BTCUSDT", "ETHUSDT"},
            "Expected both REST funding sources")
    api_items = {item["symbol"]: item for item in api_report["results"]}
    summaries, times = [], {}
    for item in archive_report["results"]:
        spec = ArchiveSpec(item["symbol"], item["kind"], item["period"])
        allowed_development(config, spec)
        start, end = map(utc_us, spec.interval())
        summary = {"symbol": spec.symbol, "kind": spec.kind, "period": spec.period}
        try:
            parent = f"data/raw/{spec.kind}/{spec.symbol}/{spec.period}"
            path = under(root, item["path"], parent)
            require(path.name == spec.url().rsplit("/", 1)[1], "Unexpected archive filename")
            checksum_path = under(root, item["checksum_path"], parent)
            body = path.read_bytes()
            digest = verify_checksum(body, checksum_path.read_bytes(), path.name)
            require(digest == item["sha256"], "Archive manifest hash differs")
            csv_body = csv_bytes(body, path.stem + ".csv")
            require(hashlib.sha256(csv_body).hexdigest() == item["csv_sha256"], "CSV hash differs")
            if spec.kind == "fundingRate":
                parsed, unit = funding(csv_body, start, end)
                api_item = api_items[spec.symbol]
                require(api_item["period"] == spec.period and "error" not in api_item, "Invalid REST source")
                api_file = under(root, api_item["path"], f"data/raw/funding_api/{spec.symbol}/{spec.period}")
                api_bytes = api_file.read_bytes()
                require(hashlib.sha256(api_bytes).hexdigest() == api_item["sha256"], "REST hash differs")
                summary.update(crosscheck_funding(parsed, json.loads(api_bytes), spec.symbol, start, end))
                summary.update(first_event_us=parsed[0]["event_time_us"], last_event_us=parsed[-1]["event_time_us"],
                               off_second_events=sum(row["event_time_us"] % 1_000_000 != 0 for row in parsed),
                               maximum_subsecond_offset_us=max(row["event_time_us"] % 1_000_000 for row in parsed))
            else:
                parsed, unit = candles(csv_body, start, end, spec.kind == "markPriceKlines")
                times[spec.symbol, spec.kind] = [row["open_time_us"] for row in parsed]
                summary.update(first_open_us=parsed[0]["open_time_us"], last_open_us=parsed[-1]["open_time_us"],
                               last_available_us=parsed[-1]["available_time_us"],
                               volume_semantics="ignored_placeholder" if spec.kind == "markPriceKlines" else "traded_volume")
            summary.update(rows=len(parsed), original_timestamp_unit=unit, schema_and_coverage_passed=True,
                           archive_sha256=digest)
        except Exception as exc:
            summary.update(schema_and_coverage_passed=False, error=f"{type(exc).__name__}: {exc}")
        summaries.append(summary)
    alignment = {symbol: times.get((symbol, "klines")) == times.get((symbol, "markPriceKlines"))
                 and (symbol, "klines") in times for symbol in ("BTCUSDT", "ETHUSDT")}
    passed = all(row["schema_and_coverage_passed"] for row in summaries) and all(alignment.values())
    missing_marks = sum(row.get("missing_exact_settlement_marks", 0) for row in summaries)
    report = {"run_id": "QUALITY-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8],
              "scope": "development_sample_only", "sample_schema_and_coverage_passed": passed,
              "complete_development_coverage": False, "execution_ready": False,
              "missing_exact_funding_settlement_marks": missing_marks,
              "funding_valuation_policy_frozen": False,
              "holdout_history_read": False, "minute_grid_alignment": alignment,
              "archive_manifest": str(archive_path.relative_to(root)).replace("\\", "/"),
              "archive_manifest_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
              "funding_manifest": str(api_path.relative_to(root)).replace("\\", "/"),
              "funding_manifest_sha256": hashlib.sha256(api_path.read_bytes()).hexdigest(),
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "results": summaries}
    path = root / "artifacts/data/quality" / (report["run_id"] + ".json")
    preserve(path, json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    return path, report
