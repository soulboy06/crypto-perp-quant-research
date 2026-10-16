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
from perpquant.engine import HOUR,MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.research4 import FourthAccount
from perpquant.research8 import EighthAccount,resolve_risk

class RiskExperimentTests(unittest.TestCase):
    def setUp(self):self.plan=tomllib.loads((ROOT/"configs/eighth_round.toml").read_text(encoding="utf-8"))

    def fixture(self,folder,case,cost="base",minimum="5"):
        config=load(ROOT/"configs/first_round.toml")
        rule=InstrumentRules(".01",".01","100",minimum,(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return EighthAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},case,cost,Path(folder),plan=self.plan)

    def data(self):
        q={"open":"100","close":"100","high":"100.1","low":"99.9"}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def close(self,a):a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_only_single_trade_risk_changes_control_identical(self):
        original=load(ROOT/"configs/first_round.toml");before=deepcopy(original)
        self.assertEqual(resolve_risk(original,"risk05",self.plan),original)
        higher=resolve_risk(original,"risk10",self.plan)
        expected=deepcopy(original);expected["risk"]["per_trade_risk_fraction"]="0.01"
        self.assertEqual(higher,expected);self.assertEqual(original,before)
        self.assertEqual(higher["risk"]["total_open_risk_fraction"],"0.01")
        self.assertEqual(higher["risk"]["drawdown_halt_fraction"],"0.20")
        self.assertEqual(higher["account"]["stop_equity_usd"],"50")

    def test_higher_trade_risk_does_not_double_shared_account_risk(self):
        for case in ("risk05","risk10"):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder,case);t=a.ledger.start+MINUTE
                action={"side":"long","stop":"98","signal_available_us":t-MINUTE}
                a.entry("BTCUSDT",t,action,self.data())
                self.assertIn("BTCUSDT",a.ledger._state["positions"])
                first=a.ledger._state["positions"]["BTCUSDT"]["quantity"]
                self.assertEqual(first,D(".21") if case=="risk05" else D(".42"))
                a.entry("ETHUSDT",t,action,self.data())
                if case=="risk10":self.assertNotIn("ETHUSDT",a.ledger._state["positions"])
                else:self.assertIn("ETHUSDT",a.ledger._state["positions"])
                gross,risk,pending,pending_risk=a.ledger._risk_values(a.ledger._state)
                self.assertLessEqual(risk+pending_risk,a.ledger.equity*D(".01"))
                self.assertLessEqual(gross+pending,a.ledger.equity)
                self.close(a)

    def test_minimum_order_rejection_can_change_without_signal_change(self):
        for case in ("risk05","risk10"):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder,case,minimum="30");t=a.ledger.start+MINUTE
                a.entry("BTCUSDT",t,{"side":"long","stop":"98","signal_available_us":t-MINUTE},self.data())
                self.assertEqual(bool(a.ledger._state["positions"]),case=="risk10")
                if case=="risk05":self.assertEqual(a.sizing_counts["rejected_filter:below_min_notional"],1)
                self.close(a)

    def test_control_reproduces_original_efficiency_decisions_and_cash_both_costs(self):
        for cost in ("base","stress"):
            with self.subTest(cost=cost),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder,"risk05",cost)
                old_plan=tomllib.loads((ROOT/"configs/fourth_round.toml").read_text(encoding="utf-8"))
                b=FourthAccount(a.config,a.rules,a.ticks,a.liquidation_fees,"efficiency24",cost,Path(folder),plan=old_plan)
                t=a.ledger.start+HOUR
                f={"close":100,"atr":1,"ema50":100,"mean":100,"trend":"long","efficiency24":.4,
                    "higher_4h":{"available_us":t,"ema20":101,"ema50":100}}
                for account in (a,b):
                    account.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
                    account.minute(t+MINUTE,self.data(),[],False)
                self.assertEqual(a.ledger._state["positions"],b.ledger._state["positions"])
                self.assertEqual(a.entry_origins if hasattr(a,"entry_origins") else {},{})
                f.update(close=99,efficiency24=0)
                for account in (a,b):
                    account.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
                    self.assertEqual(account.queue[t+HOUR+MINUTE][0]["kind"],"exit")
                    account.minute(t+HOUR+MINUTE,self.data(),[],False)
                self.assertEqual(a.ledger.equity,b.ledger.equity)
                self.assertEqual(a.trades,b.trades)
                self.assertEqual(a.gate_counts,b.gate_counts)
                self.assertEqual(a.ledger._state["fees"],b.ledger._state["fees"])
                self.close(a);self.close(b)

if __name__=="__main__":unittest.main()
