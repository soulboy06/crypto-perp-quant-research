"""Extract only newly needed hourly volume fields from existing audited ZIPs."""
import csv
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import zipfile
from datetime import datetime,timezone
from uuid import uuid4
from .archive import preserve
from .config import require
from .quality import under

HOUR=3_600_000_000
MINUTE=60_000_000
sha=lambda body:hashlib.sha256(body).hexdigest()

def volume_values(row):
    quote,buy=Decimal(row["quote_volume"]),Decimal(row["taker_buy_quote_volume"])
    require(quote.is_finite() and buy.is_finite() and quote>=0 and 0<=buy<=quote,"Invalid new volume fields")
    return quote,buy

def prepare(root):
    root=Path(root)
    plan_bytes=(root/"configs/fifth_round.toml").read_bytes()
    pointer=json.loads((root/"artifacts/data_manifest.json").read_text(encoding="utf-8"))
    manifest_path=under(root,pointer["full_manifest"],"artifacts/data/full_runs")
    require(sha(manifest_path.read_bytes())==pointer["full_manifest_sha256"],"Source metadata changed")
    manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest["complete_development_coverage"] and not manifest["holdout_history_requested"],"Wrong source scope")
    records=[r for r in manifest["results"] if r["kind"]=="klines"]
    require(len(records)==76,"Expected 38 months by two symbols")
    ident="FACTORS-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    folder=root/"artifacts/factors"/ident
    folder.mkdir(parents=True,exist_ok=False)
    preserve(folder/"source/fifth_round.toml",plan_bytes)
    preserve(folder/"source/factor_data.py",Path(__file__).read_bytes())
    all_records=[]
    sources=[]
    for symbol in ("BTCUSDT","ETHUSDT"):
        output=[]
        previous=None
        for rec in sorted((r for r in records if r["symbol"]==symbol),key=lambda r:r["period"]):
            require("2022-11"<=rec["period"]<="2025-12" and rec["audit_passed"] and rec["checksum_verified"],"Unaudited/out-of-scope raw source")
            raw=under(root,rec["path"],"data/raw/klines")
            count=0;hour=None;minutes=0;quote=buy=Decimal(0)
            multiplier={"ms":1000,"us":1}[rec["timestamp_unit"]]
            with zipfile.ZipFile(raw) as z:
                require(len(z.namelist())==1,"Unexpected raw ZIP")
                with z.open(z.namelist()[0]) as stream:
                    for row in csv.DictReader(io.TextIOWrapper(stream,encoding="utf-8-sig",newline="")):
                        t=int(row["open_time"])*multiplier
                        require(t%MINUTE==0 and (previous is None or t==previous+MINUTE),"New volume timing gap")
                        require(1_667_260_800_000_000<=t<1_767_225_600_000_000,"Attempt to read beyond permitted corpus")
                        if hour is None:
                            require(t%HOUR==0,"Incomplete source hour")
                            hour=t
                        q,b=volume_values(row)
                        quote+=q;buy+=b;minutes+=1;count+=1;previous=t
                        if minutes==60:
                            output.append({"open_time_us":hour,"available_time_us":t+MINUTE,
                                "quote_volume":str(quote),"taker_buy_quote_volume":str(buy)})
                            hour=None;minutes=0;quote=buy=Decimal(0)
            require(count==rec["rows"] and minutes==0,"Volume source incomplete")
            sources.append({k:rec[k] for k in ("symbol","period","path","sha256","timestamp_unit","rows")})
        buffer=io.StringIO(newline="")
        writer=csv.DictWriter(buffer,fieldnames=list(output[0]),lineterminator="\n")
        writer.writeheader();writer.writerows(output)
        body=buffer.getvalue().encode()
        path=folder/(symbol+"_hourly_volume.csv")
        preserve(path,body)
        all_records.append({"symbol":symbol,"path":path.relative_to(root).as_posix(),"sha256":sha(body),"hours":len(output),
            "first_available_us":output[0]["available_time_us"],"last_available_us":output[-1]["available_time_us"]})
        print(json.dumps({"volume_extracted":symbol,"hours":len(output)}),flush=True)
    report={"preparation_id":ident,"status":"completed","plan_sha256":sha(plan_bytes),
        "source_manifest_sha256":pointer["full_manifest_sha256"],"source_manifest":pointer["full_manifest"],
        "sources":sources,"outputs":all_records,"new_volume_fields_validated":True,"raw_checksums_reused_not_rescanned":True,
        "holdout_read":False,"knowledge":"each_hour_fields_available_only_after_hour_close"}
    body=json.dumps(report,ensure_ascii=False,indent=2).encode()
    preserve(folder/"manifest.json",body)
    out={"manifest":(folder/"manifest.json").relative_to(root).as_posix(),"manifest_sha256":sha(body)}
    pointer_path=root/"artifacts/factors/latest.json"
    if pointer_path.exists():preserve(root/"artifacts/factors/pointer_history"/(sha(pointer_path.read_bytes())+".json"),pointer_path.read_bytes())
    pointer_path.write_text(json.dumps(out,indent=2),encoding="utf-8")
    return folder
