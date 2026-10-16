"""Only R002 gates and higher-timeframe causality; no source revalidation."""
from decimal import Decimal as D
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from perpquant.variants import FourHourContext,VariantHour,VariantAccount,room_budget,load_plan
from perpquant.engine import HOUR,MINUTE
from perpquant.config import load
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier


class VariantTests(unittest.TestCase):
    def test_four_hour_context_cannot_use_partial_or_future_bar(self):
        context=FourHourContext()
        for h in range(1,201):
            context.push(h*HOUR,100+h)
        last=context.latest.copy()
        self.assertEqual(last["available_us"],200*HOUR)
        self.assertGreater(last["ema20"],last["ema50"])
        for h in (201,202,203):
            context.push(h*HOUR,100000)
            self.assertEqual(context.latest,last)
        context.push(204*HOUR,100000)
        self.assertEqual(context.latest["available_us"],204*HOUR)

    def test_space_gate_counts_friction_and_is_directional(self):
        thin=room_budget("long","100","100.1","98",".0006",".001",".001","1")
        self.assertFalse(thin["accepted"])
        self.assertLess(thin["net_room_per_unit"],0)
        for side,target,stop in (("long","105","98"),("short","95","102")):
            self.assertTrue(room_budget(side,"100",target,stop,".0006",".001",".001","1")["accepted"])
        self.assertTrue(room_budget("long","100","102","98","0","0","0","1")["accepted"])
        self.assertFalse(room_budget("long","100","101.99","98","0","0","0","1")["accepted"])

    def fixture(self,folder,strategy):
        config=load(ROOT/"configs/first_round.toml")
        plan,_=load_plan(ROOT)
        rule=InstrumentRules(".01",".01","100","5",(Tier("0","1000000",".025","0","10"),),
                             0,2_000_000_000_000_000,"SYNTHETIC_TEST_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        account=VariantAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},strategy,"base",Path(folder),plan=plan)
        return account

    def test_trend_gate_filters_entries_but_keeps_original_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,"trend")
            t=a.ledger.start+HOUR
            f={"close":100,"atr":1,"ema50":100,"mean":100,"trend":"long",
               "higher_4h":{"available_us":a.ledger.start,"ema20":99,"ema50":100}}
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE],[])
            f["higher_4h"]["ema20"]=101
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE][0]["side"],"long")
            q={"open":"100","high":"100.1","low":"99.9","close":"100"}
            data={s:{"trade":q,"mark":q} for s in a.rules}
            a.minute(t+MINUTE,data,[],False)
            a.minute(t+2*MINUTE,data,[],True)
            self.assertEqual(a.trades[-1]["exit_reason"],"end_of_partition")
            a.gate_log.close();a.event_stream.close()

    def test_mean_target_is_signal_mean_and_entry_keeps_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,"mean_reversion")
            t=a.ledger.start+HOUR
            f={"close":100,"atr":1,"ema50":100,"mean":100.2,"mean_reversion":"long"}
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            action=a.queue[t+MINUTE][0]
            self.assertEqual(D(action["target_mean"]),D("100.2"))
            q={"open":"100","high":"100.1","low":"99.9","close":"100"}
            data={s:{"trade":q,"mark":q} for s in a.rules}
            with self.assertRaises(ValueError):
                a.entry("BTCUSDT",t,action,data)
            a.minute(t+MINUTE,data,[],False)
            self.assertFalse(a.ledger._state["positions"])
            self.assertEqual(a.gate_counts["net_room_below_stop_risk"],1)
            a.gate_log.close();a.event_stream.close()


if __name__=="__main__":
    unittest.main()
