"""Only new causal execution/accounting edges; no repeated source-data scan."""
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.config import load
from perpquant.engine import Account,ReplayLedger,MINUTE,HOUR
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.signals import Indicators


class ExecutionTests(unittest.TestCase):
    def fixture(self,folder):
        config=load(ROOT/"configs/first_round.toml")
        rule=InstrumentRules("0.01","0.01","100","5",(Tier("0","1000000","0.025","0","10"),),
                             0,2_000_000_000_000_000,"SYNTHETIC_TEST_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        account=Account(config,rules,{s:D("0.01") for s in rules},{s:D("0.0125") for s in rules},"trend","base",Path(folder))
        quote={"open":"100","high":"100.1","low":"99.9","close":"100"}
        data={s:{"trade":quote.copy(),"mark":quote.copy()} for s in rules}
        return account,data

    def test_breakout_excludes_current_bar_and_ema_seed_is_sma(self):
        indicators=Indicators()
        for _ in range(200):
            f=indicators.push(100,101,99,100)
        self.assertEqual(f["ema200"],100)
        f=indicators.push(100,103,99,102)
        self.assertEqual(f["prior_high"],101)
        self.assertEqual(f["trend"],"long")
        self.assertAlmostEqual(f["ema200"],100+4/201)

    def test_closed_signal_delay_and_preplanned_terminal_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            account,data=self.fixture(folder)
            available=account.ledger.start+HOUR
            feature={"close":100,"atr":1,"ema50":100,"mean":100,"trend":"long"}
            account.decisions(available,{"BTCUSDT":feature,"ETHUSDT":None})
            self.assertIn(available+MINUTE,account.queue)
            account.minute(available,data,[],False)
            self.assertFalse(account.ledger._state["positions"])
            account.minute(available+MINUTE,data,[],False)
            self.assertIn("BTCUSDT",account.ledger._state["positions"])
            account.minute(available+2*MINUTE,data,[],True)
            self.assertFalse(account.ledger._state["positions"])
            self.assertEqual(account.trades[0]["exit_reason"],"end_of_partition")
            account.event_stream.close()

    def test_fast_observation_matches_atomic_ledger_marks_and_rejects_future(self):
        with tempfile.TemporaryDirectory() as folder:
            account,data=self.fixture(folder)
            t=account.ledger.start
            action={"side":"long","stop":"98"}
            account.entry("BTCUSDT",t,action,data)
            before=account.ledger.snapshot()
            with self.assertRaises(ValueError):
                account.ledger.observe(account.ledger.end,{"BTCUSDT":"100"})
            self.assertEqual(before,account.ledger.snapshot())
            account.ledger.observe(t+MINUTE,{"BTCUSDT":"101"})
            s=account.ledger._state
            expected=D(100)+s["realized_pnl"]+s["funding"]-s["fees"]+sum(p["quantity"]*(p["mark"]-p["entry"]) for p in s["positions"].values())
            self.assertEqual(account.ledger.equity,expected)
            account.event_stream.close()

    def test_stop_and_assumed_liquidation_surcharge_are_accounted(self):
        with tempfile.TemporaryDirectory() as folder:
            account,data=self.fixture(folder)
            t=account.ledger.start
            account.entry("BTCUSDT",t,{"side":"long","stop":"98"},data)
            quantity=account.ledger._state["positions"]["BTCUSDT"]["quantity"]
            price=account.execution_price("BTCUSDT","90",False)
            account.exit("BTCUSDT",t,"90","liquidation_proxy")
            expected_fee=quantity*(D("100.02")*D(".0006")+price*(D(".0006")+D(".0125")))
            self.assertEqual(account.ledger._state["fees"],expected_fee)
            self.assertEqual(account.ledger.equity,D(100)+quantity*(price-D("100.02"))-expected_fee)
            account.event_stream.close()

    def test_existing_risk_reserves_only_unpaid_exit_friction(self):
        with tempfile.TemporaryDirectory() as folder:
            account,data=self.fixture(folder)
            t=account.ledger.start
            account.entry("BTCUSDT",t,{"side":"long","stop":"98"},data)
            p=account.ledger._state["positions"]["BTCUSDT"]
            _,risk,_,_=account.ledger._risk_values(account.ledger._state)
            expected=abs(p["quantity"])*(D("100")-p["stop"]+D("100")*(D(".0008")+D(".001")+D(".001")))
            self.assertEqual(risk,expected)
            account.event_stream.close()

    def test_holding_deadline_exits_at_minute_without_waiting_next_hour(self):
        for strategy,hours in (("trend",72),("mean_reversion",24)):
            with self.subTest(strategy=strategy),tempfile.TemporaryDirectory() as folder:
                account,data=self.fixture(folder)
                account.strategy=strategy
                start=account.ledger.start
                entry=start+MINUTE
                account.entry("BTCUSDT",entry,{"side":"long","stop":"98"},data)
                for day in range(1,hours//24+1):
                    account.minute(start+day*24*HOUR,data,[],False)
                deadline=entry+hours*HOUR
                account.minute(deadline,data,[],False)
                self.assertFalse(account.ledger._state["positions"])
                self.assertEqual(account.trades[-1]["exit_reason"],"time_limit")
                self.assertEqual(account.trades[-1]["exit_time_us"],deadline)
                account.event_stream.close()


if __name__=="__main__":
    unittest.main()
