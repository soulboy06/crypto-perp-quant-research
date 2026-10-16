from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import math
import sys
import tempfile
import tomllib
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
sys.path.insert(0,str(ROOT/"tests"))
import test_execution
from perpquant.config import load
from perpquant.engine import HOUR,MINUTE
from perpquant.research3 import ThirdHour,ThirdAccount,case_config
from perpquant.uncertainty import paired_cagr_diagnostic

class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.plan=tomllib.loads((ROOT/"configs/third_round.toml").read_text(encoding="utf-8"))

    def test_only_prespecified_single_field_changes(self):
        original=load(ROOT/"configs/first_round.toml")
        for case,key,value in (("breakout24","breakout_hours",24),("hold168","max_holding_hours",168)):
            changed=case_config(original,case,self.plan)
            expected=deepcopy(original)
            expected["strategies"]["trend"][key]=value
            self.assertEqual(changed,expected)
            self.assertNotEqual(changed,original)

    def test_channel_excludes_current_and_keeps_48_hour_parent(self):
        h=ThirdHour()
        for hour in range(201):
            close=105 if hour==200 else 100
            high=110 if hour==160 else (106 if hour==200 else 101)
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":str(high),"low":"99","close":str(close)})
        self.assertTrue(closed)
        self.assertEqual(f["prior24_high"],101)
        self.assertEqual(f["prior_high"],110)
        self.assertEqual(f["trend_24"],"long")
        self.assertIsNone(f["trend"])

    def test_extended_holding_deadline_and_feature_isolation(self):
        for case,limit in (("breakout24",72),("hold168",168)):
            with tempfile.TemporaryDirectory() as folder:
                fixture,data=test_execution.ExecutionTests().fixture(folder)
                fixture.event_stream.close()
                account=ThirdAccount(fixture.config,fixture.rules,fixture.ticks,fixture.liquidation_fees,case,"base",Path(folder),plan=self.plan)
                available=account.ledger.start+HOUR
                f={"close":100,"atr":1,"ema50":100,"mean":100,"trend":None,"trend_24":"long",
                   "higher_4h":{"available_us":available,"ema20":101,"ema50":100}}
                features={"BTCUSDT":f,"ETHUSDT":None}
                before=deepcopy(features)
                account.decisions(available,features)
                self.assertEqual(features,before)
                actions=account.queue.get(available+MINUTE,[])
                self.assertEqual(len(actions),1 if case=="breakout24" else 0)
                account.queue.clear()
                start=account.ledger.start
                entry=start+MINUTE
                account.entry("BTCUSDT",entry,{"side":"long","stop":"98","signal_available_us":start},data)
                for day in range(1,limit//24+1):
                    account.minute(start+day*24*HOUR,data,[],False)
                    self.assertIn("BTCUSDT",account.ledger._state["positions"])
                account.minute(entry+limit*HOUR,data,[],False)
                self.assertFalse(account.ledger._state["positions"])
                self.assertEqual(account.trades[-1]["exit_time_us"],entry+limit*HOUR)
                self.assertEqual(account.trades[-1]["exit_reason"],"time_limit")
                account.event_stream.close()
                account.gate_log.close()

    def test_paired_bootstrap_known_constant_paths(self):
        a=[100*math.exp(.0001*(i+1)) for i in range(60)]
        b=[100*math.exp(.0002*(i+1)) for i in range(60)]
        same=paired_cagr_diagnostic(a,a,resamples=100)
        self.assertEqual(same["cagr_improvement_lower_95"],0)
        result=paired_cagr_diagnostic(a,b,resamples=100)
        expected=math.expm1(.0002*365)-math.expm1(.0001*365)
        self.assertAlmostEqual(result["cagr_improvement_lower_95"],expected)
        self.assertAlmostEqual(result["cagr_improvement_upper_95"],expected)

if __name__=="__main__":
    unittest.main()
