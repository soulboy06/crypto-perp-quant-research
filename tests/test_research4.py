from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.config import load
from perpquant.engine import Account,HOUR,MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.research4 import FourthAccount,FourthHour,Efficiency,sizing_diagnostic

class FourthTests(unittest.TestCase):
    def fixture(self,folder,case="profit_trail",minimum="5"):
        config=load(ROOT/"configs/first_round.toml")
        plan=tomllib.loads((ROOT/"configs/fourth_round.toml").read_text(encoding="utf-8"))
        rule=InstrumentRules(".01",".01","100",minimum,(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return FourthAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},case,"base",Path(folder),plan=plan)

    def data(self,price="100",high=None,low=None):
        q={"open":price,"close":price,"high":high or str(D(price)+D(".1")),"low":low or str(D(price)-D(".1"))}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def feature(self,t,close=103,er=1):
        return {"close":close,"atr":1,"ema50":100,"mean":100,"trend":"long","efficiency24":er,
                "higher_4h":{"available_us":t,"ema20":101,"ema50":100}}

    def close(self,a):
        a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_efficiency_uses_24_changes_and_zero_path_rejects(self):
        e=Efficiency()
        for i in range(24):self.assertIsNone(e.push(i))
        self.assertEqual(e.push(24),1)
        e=Efficiency()
        for _ in range(25):value=e.push(100)
        self.assertEqual(value,0)
        e=Efficiency()
        for i in range(25):value=e.push(100+i%2)
        self.assertEqual(value,0)
        h=FourthHour()
        for hour in range(201):
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":str(101+hour),"low":"99","close":str(100+hour)})
        self.assertTrue(closed);self.assertEqual(f["efficiency24"],1)
        old=h.efficiency.history.copy()
        h.push({"open":"100","high":"10000","low":"99","close":"10000"})
        self.assertEqual(h.efficiency.history,old)

    def test_efficiency_only_filters_entries_keeps_original_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,"efficiency24")
            t=a.ledger.start+HOUR
            f=self.feature(t,er=.29);before=deepcopy(f)
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(f,before);self.assertEqual(a.queue[t+MINUTE],[])
            f["efficiency24"]=.30
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE][0]["kind"],"entry")
            # Gate must never block a pre-existing position's EMA exit.
            a.entry("BTCUSDT",t,{"side":"long","stop":"98","signal_available_us":t-MINUTE},self.data())
            f.update(close=99,efficiency24=0)
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE][0]["kind"],"exit")
            self.close(a)

    def test_long_trailing_delayed_ratchet_and_current_minute_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);start=a.ledger.start
            a.entry("BTCUSDT",start+MINUTE,{"side":"long","stop":"98","signal_available_us":start},self.data())
            t=start+HOUR
            a.decisions(t,{"BTCUSDT":self.feature(t),"ETHUSDT":None})
            self.assertEqual(a.ledger._state["positions"]["BTCUSDT"]["stop"],98)
            a.minute(t,self.data("103"),[],False)
            self.assertEqual(a.ledger._state["positions"]["BTCUSDT"]["stop"],98)
            a.minute(t+MINUTE,self.data("103",low="100.5"),[],False)
            self.assertFalse(a.ledger._state["positions"])
            self.assertEqual(a.trades[-1]["exit_reason"],"stop")
            self.assertEqual(D(a.trades[-1]["stop_price"]),101)
            self.assertEqual(a.amendment_counts["effective_amendments"],1)
            self.close(a)

    def test_new_stop_gap_exits_at_open_not_better_stop(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);start=a.ledger.start
            a.entry("BTCUSDT",start+MINUTE,{"side":"long","stop":"98","signal_available_us":start},self.data())
            t=start+HOUR
            a.decisions(t,{"BTCUSDT":self.feature(t),"ETHUSDT":None})
            a.minute(t+MINUTE,self.data("100"),[],False)
            self.assertEqual(a.trades[-1]["exit_reason"],"trailing_gap_stop")
            self.assertLess(D(a.trades[-1]["exit_price"]),100)
            self.close(a)

    def test_short_trailing_atomic_idempotent_no_loosen_or_early_update(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);start=a.ledger.start
            a.entry("BTCUSDT",start+MINUTE,{"side":"short","stop":"102","signal_available_us":start},self.data())
            t=start+HOUR
            f=self.feature(t,close=97);f["ema50"]=100
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            a.minute(t+MINUTE,self.data("97"),[],False)
            p=a.ledger._state["positions"]["BTCUSDT"]
            self.assertEqual(p["stop"],99)
            state=a.ledger.snapshot()
            event={"id":"amend-test","type":"tighten_stop","time_us":t+2*MINUTE,"symbol":"BTCUSDT",
                "stop_price":"98.5","previous_stop":"99","tick_size":".01","signal_available_us":t+2*MINUTE,"policy_id":"R004-v1"}
            with self.assertRaises(ValueError):a.ledger.apply(event)
            self.assertEqual(a.ledger.snapshot(),state)
            a.observe(t+2*MINUTE,self.data("97"),"open")
            event["signal_available_us"]=t
            equity,fees=a.ledger.equity,a.ledger._state["fees"]
            a.ledger.apply(event)
            saved=a.ledger.snapshot();a.ledger.apply(event)
            self.assertEqual(a.ledger.snapshot(),saved)
            self.assertEqual(a.ledger.equity,equity);self.assertEqual(a.ledger._state["fees"],fees)
            event.update(id="loosen-test",previous_stop="98.5",stop_price="100")
            with self.assertRaises(ValueError):a.ledger.apply(event)
            self.assertEqual(a.ledger.snapshot(),saved)
            self.close(a)

    def test_rejection_reports_budget_and_filters_without_changing_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,minimum="50")
            d=sizing_diagnostic(a.ledger,"BTCUSDT","long",D("100.02"),D("98"))
            self.assertEqual(d["binding_constraint"],"stop_risk")
            self.assertIn("below_min_notional",d["filter_failures"])
            a.entry("BTCUSDT",a.ledger.start+MINUTE,{"side":"long","stop":"98","signal_available_us":a.ledger.start},self.data())
            self.assertFalse(a.ledger._state["positions"])
            self.assertEqual(a.ledger.equity,100)
            self.assertEqual(a.sizing_counts["rejected_binding:stop_risk"],1)
            self.assertEqual(a.sizing_counts["rejected_filter:below_min_notional"],1)
            self.close(a)

    def test_inactive_extensions_match_original_financial_path(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,"efficiency24")
            b=Account(a.config,a.rules,a.ticks,a.liquidation_fees,"trend","base",Path(folder))
            t=a.ledger.start+MINUTE
            action={"side":"long","stop":"98","signal_available_us":t-MINUTE}
            for account in (a,b):
                account.entry("BTCUSDT",t,action,self.data())
                account.minute(t+MINUTE,self.data("101"),[],True)
            self.assertEqual(a.ledger.equity,b.ledger.equity)
            self.assertEqual(a.trades,b.trades)
            b.event_stream.close();self.close(a)

if __name__=="__main__":unittest.main()
