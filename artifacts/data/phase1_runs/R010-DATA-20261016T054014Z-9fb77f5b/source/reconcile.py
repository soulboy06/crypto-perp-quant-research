"""Repair vendor monthly gaps only from checksummed official daily observations."""
from bisect import bisect_right
import csv
from datetime import datetime, timezone, timedelta
import gzip
import io
import json
import tomllib
from pathlib import Path
from uuid import uuid4
from .archive import ArchiveSpec, allowed_development, download_one, preserve, verify_checksum
from .config import decimal_value as D, load, require
from .full_data import MonthlySpec, specs_for, unpack, sha, relative, fetch
from .quality import rows, KLINE_HEADER, candles, utc_us, timestamp, MINUTE_US, under


def compressed_rows(parsed):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(parsed[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(parsed)
    return gzip.compress(buffer.getvalue().encode(), mtime=0)


def repair(root, config, item):
    spec = MonthlySpec(item["symbol"], item["kind"], item["period"])
    allowed_development(config, spec)
    require(spec.kind in ("klines", "markPriceKlines"), "Only observed candle gaps can be repaired")
    start_dt, end_dt = spec.interval()
    start, end = map(utc_us, (start_dt, end_dt))
    name = spec.url().rsplit("/", 1)[1]
    candidates = list((root / "data/raw" / spec.kind / spec.symbol / spec.period).glob("*/"+name))
    require(len(candidates) == 1, "Ambiguous/missing preserved monthly source")
    monthly_path = candidates[0]
    body = monthly_path.read_bytes()
    checks = list(monthly_path.parent.glob("*.CHECKSUM"))
    require(len(checks) == 1, "Ambiguous official checksum")
    verify_checksum(body, checks[0].read_bytes(), name)
    data = rows(unpack(body, name[:-4]+".csv"), KLINE_HEADER)
    merged = {}
    for row in data:
        opening, _ = timestamp(row[0], start, end)
        require(opening % MINUTE_US == 0 and opening not in merged, "Cannot repair duplicate or off-grid observations")
        merged[opening] = row
    missing = sorted(set(range(start, end, MINUTE_US))-set(merged))
    require(bool(missing), "Monthly failure is not a simple missing-observation gap")
    days = sorted({datetime.fromtimestamp(t//1_000_000, timezone.utc).strftime("%Y-%m-%d") for t in missing})
    sources = [{"path":relative(root, monthly_path), "sha256":sha(body),
                "checksum_path":relative(root, checks[0]), "role":"incomplete_monthly_original"}]
    for day in days:
        daily = ArchiveSpec(spec.symbol, spec.kind, day)
        result = download_one(root, config, daily)
        require(not result.get("error") and result.get("checksum_verified"), "Official daily retrieval failed: "+str(result.get("error")))
        daily_body = under(root, result["path"], "data/raw").read_bytes()
        daily_csv = unpack(daily_body, daily.url().rsplit("/",1)[1][:-4]+".csv")
        ds, de = map(utc_us, daily.interval())
        # A daily archive can share the monthly gap. The final merged month must
        # still pass the unmodified strict candle audit; no fill is inferred here.
        overlaps = 0
        for row in rows(daily_csv, KLINE_HEADER):
            opening, _ = timestamp(row[0], ds, de)
            if opening in merged:
                old = merged[opening]
                require([D(v) for v in old[1:5]] == [D(v) for v in row[1:5]], "Daily/monthly OHLC revision conflict")
                overlaps += 1
            else:
                merged[opening] = row
        sources.append({**result, "role":"official_daily_gap_source", "matching_overlap_rows":overlaps})
        print(json.dumps({"repair":spec.period, "symbol":spec.symbol, "daily_source":day, "overlap":overlaps}), flush=True)
        remaining = sorted(t for t in range(ds,de,MINUTE_US) if t not in merged)
        if remaining:
            require(spec.kind == "markPriceKlines", "REST recovery implemented only for mark candles")
            first, last = max(ds,remaining[0]-MINUTE_US), min(de,remaining[-1]+2*MINUTE_US)
            require((last-first)//MINUTE_US < 1500, "Gap recovery requires explicit REST pagination")
            allowed_development(config,daily)
            url = (f"https://fapi.binance.com/fapi/v1/markPriceKlines?symbol={spec.symbol}&interval=1m"
                   f"&startTime={first//1000}&endTime={last//1000-1}&limit=1500")
            api_body = fetch(url,2_000_000)
            payload = json.loads(api_body)
            require(isinstance(payload,list) and 0<len(payload)<1500, "Invalid REST gap response")
            api_path = root/"data/raw/mark_gap_api"/spec.symbol/day/(sha(api_body)+".json")
            preserve(api_path,api_body)
            api_seen, matches, recovered = set(), 0, []
            for api_row in payload:
                require(isinstance(api_row,list) and len(api_row)==12, "Invalid REST mark schema")
                row = [str(v) for v in api_row]
                opening,_ = timestamp(row[0],first,last)
                require(opening not in api_seen, "Duplicate REST mark candle")
                api_seen.add(opening)
                check = io.StringIO(newline="")
                writer = csv.writer(check,lineterminator="\n")
                writer.writerow(KLINE_HEADER)
                writer.writerow(row)
                candles(check.getvalue().encode(),opening,opening+MINUTE_US,mark=True)
                if opening in merged:
                    require([D(v) for v in merged[opening][1:5]] == [D(v) for v in row[1:5]], "REST/archive OHLC revision conflict")
                    matches += 1
                else:
                    merged[opening]=row
                    recovered.append(opening)
            require(matches>0 and set(remaining)<=set(recovered), "REST did not recover all gaps with matching overlap evidence")
            sources.append({"role":"official_rest_gap_source", "url":url, "path":relative(root,api_path),
                            "sha256":sha(api_body), "exchange_checksum_available":False,
                            "matching_overlap_rows":matches,"recovered_open_time_us":recovered})
            print(json.dumps({"repair":spec.period,"symbol":spec.symbol,"rest_recovered_minutes":len(recovered),"matching_overlap_rows":matches}),flush=True)
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(KLINE_HEADER)
    writer.writerows(merged[t] for t in sorted(merged))
    merged_csv = buffer.getvalue().encode()
    parsed, unit = candles(merged_csv, start, end, mark=spec.kind == "markPriceKlines")
    normalized = compressed_rows(parsed)
    path = root / "data/validated" / spec.kind / spec.symbol / spec.period / (sha(normalized)+".csv.gz")
    preserve(path, normalized)
    recipe = {"monthly_original_error":item.get("error"), "missing_minutes":len(missing), "daily_dates":days,
              "source_parts":sources, "merged_csv_sha256":sha(merged_csv), "synthesized_prices":False}
    return {"symbol":spec.symbol, "kind":spec.kind, "period":spec.period, "frequency":"monthly",
            "source_type":"official_monthly_plus_daily", "repair":recipe, "audit_passed":True,
            "rows":len(parsed), "timestamp_unit":unit, "first_time_us":parsed[0]["open_time_us"],
            "last_time_us":parsed[-1]["open_time_us"], "normalized_path":relative(root,path), "normalized_sha256":sha(normalized)}


def normalized(root, item):
    path = under(root, item["normalized_path"], "data/validated")
    body = path.read_bytes()
    require(sha(body) == item["normalized_sha256"], "Normalized content hash differs")
    return list(csv.DictReader(io.StringIO(gzip.decompress(body).decode())))


def reconcile(root, original_manifest):
    root = Path(root).resolve()
    config = load(root / "configs/first_round.toml")
    policy = tomllib.loads((root/"configs/data_policy.toml").read_text(encoding="utf-8"))
    require(policy["parent_design"] == config["design_id"] and policy["missing_settlement_mark"] == "last_closed_mark_1m"
            and policy["maximum_proxy_age_seconds"] == 60 and policy["allow_future_candle_close"] is False
            and policy["price_proxy_is_exact"] is False, "Data policy does not match this valuation implementation")
    original_path = under(root, original_manifest, "artifacts/data/full_runs")
    original = json.loads(original_path.read_text(encoding="utf-8"))
    require(original["scope"] == "warmup_and_development_only" and original["holdout_history_requested"] is False,
            "Wrong parent manifest scope")
    require(original["config_sha256"] == sha((root/"configs/first_round.toml").read_bytes()), "Configuration binding differs")
    expected = {(s.symbol,s.kind,s.period) for s in specs_for(config)}
    require(len(original["results"]) == len(expected) and
            {(r["symbol"],r["kind"],r["period"]) for r in original["results"]} == expected, "Incomplete archive inventory")
    run_id = "RECONCILED-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    directory = root / "artifacts/data/full_runs" / run_id
    directory.mkdir(parents=True, exist_ok=False)
    for name in ("reconcile.py","full_data.py","quality.py","archive.py","config.py"):
        preserve(directory/"source"/name, Path(__file__).with_name(name).read_bytes())
    for name in ("first_round.toml","data_policy.toml"):
        preserve(directory/"source"/name, (root/"configs"/name).read_bytes())
    records, errors = [], []
    for item in original["results"]:
        try:
            records.append(item if item.get("audit_passed") else repair(root, config, item))
        except Exception as exc:
            records.append(item)
            errors.append({"symbol":item["symbol"],"kind":item["kind"],"period":item["period"],"error":str(exc)})
    funding_values = []
    if not errors:
        index = {(r["symbol"],r["kind"],r["period"]):r for r in records}
        development_start = utc_us(datetime.fromisoformat(config["data"]["development_start"].replace("Z","+00:00")))
        for symbol in config["market"]["symbols"]:
            previous_mark, previous_funding = None, None
            for period in sorted({r["period"] for r in records}):
                price_item, mark_item, fund_item = [index[symbol,k,period] for k in ("klines","markPriceKlines","fundingRate")]
                # Hash all normalized partitions, including price series not used in funding valuation.
                prices, marks, funds = [normalized(root, r) for r in (price_item,mark_item,fund_item)]
                require(len(prices) == len(marks) and all(p["open_time_us"] == m["open_time_us"] for p,m in zip(prices,marks)),
                        "Trade/mark candle alignment differs")
                available = [int(row["available_time_us"]) for row in marks]
                if previous_mark is not None:
                    require(int(marks[0]["open_time_us"]) == int(previous_mark["available_time_us"]), "Cross-month candle discontinuity")
                api_path = under(root, fund_item["funding_rest_path"], "data/raw/funding_api")
                api_body = api_path.read_bytes()
                require(sha(api_body) == fund_item["funding_rest_sha256"], "Funding REST hash differs")
                exact = {int(row["fundingTime"])*1000: row.get("markPrice") for row in json.loads(api_body)}
                for row in funds:
                    event, interval = int(row["event_time_us"]), int(row["interval_hours"])
                    if previous_funding is not None:
                        require(abs(event-previous_funding-interval*3_600_000_000) <= MINUTE_US, "Cross-month funding schedule discontinuity")
                    previous_funding = event
                    if event < development_start:
                        continue  # Warmup has no positions; no invented opening-boundary settlement mark.
                    if exact[event] not in (None, ""):
                        price, source, is_exact = str(D(exact[event])), event, True
                        require(D(price)>0, "Invalid exact settlement price")
                    else:
                        position = bisect_right(available,event)-1
                        latest = marks[position] if position>=0 else previous_mark
                        require(latest is not None, "No causal mark at month boundary")
                        price, source, is_exact = latest["close"], int(latest["available_time_us"]), False
                        require(0 <= event-source <= MINUTE_US and D(price)>0, "Funding proxy unavailable/stale")
                    funding_values.append({"symbol":symbol,"event_time_us":event,"rate":row["rate"],"interval_hours":interval,
                                           "settlement_mark":price,"exact_mark":is_exact,"source_available_us":source,"age_us":event-source})
                previous_mark = marks[-1]
    funding_path = None
    if funding_values:
        funding_values.sort(key=lambda r:(r["event_time_us"],r["symbol"]))
        body = compressed_rows(funding_values)
        funding_path = root/"data/validated/funding_valuation"/(sha(body)+".csv.gz")
        preserve(funding_path, body)
    report = {"run_id":run_id,"scope":"warmup_and_development_only","parent_manifest":relative(root,original_path),
              "parent_sha256":sha(original_path.read_bytes()),"config_sha256":original["config_sha256"],
              "data_policy_sha256":sha((root/"configs/data_policy.toml").read_bytes()),"holdout_history_requested":False,
              "original_incomplete_monthly_archives":original["failed_archives"],
              "repaired_archives":sum("repair" in r for r in records),"errors":errors,"results":records,
              "complete_development_coverage":not errors and all(r.get("audit_passed") for r in records),
              "minute_rows":sum(r.get("rows",0) for r in records if r["kind"]!="fundingRate"),
              "funding_rows_including_warmup":sum(r.get("rows",0) for r in records if r["kind"]=="fundingRate"),
              "development_funding_valuations":len(funding_values),"proxy_valuations":sum(not r["exact_mark"] for r in funding_values),
              "maximum_proxy_age_us":max((r["age_us"] for r in funding_values if not r["exact_mark"]),default=0),
              "funding_valuation_path":relative(root,funding_path) if funding_path else None,
              "funding_valuation_sha256":sha(funding_path.read_bytes()) if funding_path else None,
              "historical_margin_rules_verified":False,"historical_order_filters_verified":False,"execution_ready":False}
    report_path = directory/"manifest.json"
    preserve(report_path,json.dumps(report,ensure_ascii=False,indent=2).encode())
    print(json.dumps({"manifest":str(report_path), **{k:report[k] for k in ("complete_development_coverage","repaired_archives","minute_rows","development_funding_valuations","proxy_valuations","maximum_proxy_age_us","errors")}},ensure_ascii=False),flush=True)
    return report_path,report
