"""Local evidence integrity and targeted regression checks; no market requests."""
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve, verify_checksum
from perpquant.config import load, readiness, require
from perpquant.quality import under
from perpquant.full_data import sha


def main():
    pointer=json.loads((ROOT/"artifacts/data_manifest.json").read_text(encoding="utf-8"))
    path=under(ROOT,pointer["full_manifest"],"artifacts/data/full_runs")
    require(sha(path.read_bytes())==pointer["full_manifest_sha256"],"Manifest hash differs")
    report=json.loads(path.read_text(encoding="utf-8"))
    require(report["complete_development_coverage"] and report["execution_ready"] is False,"Wrong dataset state")
    require(report["config_sha256"]==sha((ROOT/"configs/first_round.toml").read_bytes()),"Design changed")
    require(report["data_policy_sha256"]==sha((ROOT/"configs/data_policy.toml").read_bytes()),"Data policy changed")
    checked={}
    def check(relative,expected):
        p=under(ROOT,relative,"data")
        body=p.read_bytes()
        require(sha(body)==expected,"Content differs: "+relative)
        checked[relative]=expected
        return p,body
    for item in report["results"]:
        check(item["normalized_path"],item["normalized_sha256"])
        sources=item["repair"]["source_parts"] if "repair" in item else [item]
        for source in sources:
            p,body=check(source["path"],source["sha256"])
            if "checksum_path" in source:
                checksum_path=under(ROOT,source["checksum_path"],"data/raw")
                verify_checksum(body,checksum_path.read_bytes(),p.name)
        if item["kind"]=="fundingRate":
            check(item["funding_rest_path"],item["funding_rest_sha256"])
    check(report["funding_valuation_path"],report["funding_valuation_sha256"])
    loader=unittest.TestLoader()
    suite=unittest.TestSuite([loader.discover(str(ROOT/"tests"),pattern=pattern)
                             for pattern in ("test_ledger.py","test_full_data.py")])
    log=io.StringIO()
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
    config=load(ROOT/"configs/first_round.toml")
    remaining=readiness(config,ROOT)
    evidence={"scope":"local_full_data_integrity_and_new_module_regressions", "network_requests":0,
              "verified_data_files":len(checked),"tests_run":result.testsRun,"tests_passed":result.wasSuccessful(),
              "test_failures":len(result.failures),"test_errors":len(result.errors),
              "full_manifest":pointer["full_manifest"],"full_manifest_sha256":pointer["full_manifest_sha256"],
              "execution_ready":not remaining,"readiness_missing":remaining,
              "holdout_history_read":False,"strategy_backtest_executed":False}
    run="VERIFY-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    directory=ROOT/"artifacts/setup/full_checks"/run
    preserve(directory/"test_output.txt",log.getvalue().encode())
    preserve(directory/"evidence.json",json.dumps(evidence,ensure_ascii=False,indent=2).encode())
    print(json.dumps({"evidence":str(directory/"evidence.json"),**evidence},ensure_ascii=False))
    return 0 if result.wasSuccessful() and remaining else 2


if __name__=="__main__":
    raise SystemExit(main())
