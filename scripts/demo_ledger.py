"""A hand-computable synthetic journal/restart exercise, never a strategy backtest."""
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.archive import preserve
from perpquant.config import load
from perpquant.ledger import Ledger
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier


def main():
    config=load(ROOT/"configs/first_round.toml")
    rule=InstrumentRules("0.01","0.01","100","5",(Tier("0","1000000","0.005","0","10"),),
                         0,2_000_000_000_000_000,"SYNTHETIC_DEMO_NOT_BINANCE_RULES")
    rules={s:rule for s in config["market"]["symbols"]}
    run_id="LEDGER-DEMO-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid4().hex[:8]
    folder=ROOT/"artifacts/ledger/demos"/run_id
    folder.mkdir(parents=True,exist_ok=False)
    kwargs={"funding_policy":"available_first_credit_cash_research_v1","journal":folder/"journal.sqlite"}
    ledger=Ledger(config,rules,**kwargs)
    t=ledger.start+60_000_000
    n=0
    def apply(kind,**values):
        nonlocal n
        n+=1
        return ledger.apply({"id":f"demo-{n}","type":kind,"time_us":t,**values})
    for symbol,side,stop in (("BTCUSDT","long","98"),("ETHUSDT","short","102")):
        apply("reserve",symbol=symbol,side=side,order_id=symbol,reference_price="100",stop_price=stop,quantity="0.20")
        apply("fill",order_id=symbol,quantity="0.20",price="100",mark_price="100")
    t=ledger.start+8*3_600_000_000
    apply("mark",prices={"BTCUSDT":"102","ETHUSDT":"99"})
    for symbol,mark in (("BTCUSDT","102"),("ETHUSDT","99")):
        apply("funding",symbol=symbol,mark_price=mark,rate="0.0001",exact_mark=True)
    t+=60_000_000
    apply("mark",prices={"BTCUSDT":"102","ETHUSDT":"99"})
    for symbol,price in (("BTCUSDT","102"),("ETHUSDT","99")):
        apply("exit",symbol=symbol,quantity="0.20",price=price,mark_price=price)
    before=ledger.snapshot()
    equity=str(ledger.equity)
    ledger.close()
    restored=Ledger(config,rules,**kwargs)
    replay_matches=restored.snapshot()==before
    restored.close()
    report={"run_id":run_id,"scope":"synthetic_accounting_exercise_only","historical_backtest":False,
            "rules_are_synthetic":True,"rule_fixture":asdict(rule),"initial_equity":"100",
            "hand_expected_final_equity":"100.55182","observed_final_equity":equity,
            "identity_matches":ledger.equity==Decimal("100.55182"),
            "journal_replay_matches":replay_matches,"binding":ledger.binding,"final_state":before}
    path=folder/"evidence.json"
    preserve(path,json.dumps(report,ensure_ascii=False,indent=2,default=str).encode())
    print(json.dumps({"evidence":str(path),"identity_matches":report["identity_matches"],"journal_replay_matches":replay_matches}))
    return 0 if report["identity_matches"] and replay_matches else 2


if __name__=="__main__":
    raise SystemExit(main())
