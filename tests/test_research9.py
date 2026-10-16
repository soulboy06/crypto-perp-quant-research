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
from perpquant.research9 import EMA20State,RetestState,NinthHour,NinthAccount,reclaim_signal

class PullbackTests(unittest.TestCase):
    def fixture(self,folder,case):
        config=load(ROOT/"configs/first_round.toml")
        plan=tomllib.loads((ROOT/"configs/ninth_round.toml").read_text(encoding="utf-8"))
        rule=InstrumentRules(".01",".01","100","5",(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return NinthAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},case,"base",Path(folder),plan=plan)

    def feature(self,t,side="long",trend=True,close=None):
        long=side=="long"
        return {"close":close if close is not None else (102 if long else 98),"ema200":100,
            "trend":side if trend else None,"efficiency24":.4,"prior_high":101,"prior_low":99,
            "higher_4h":{"available_us":t,"ema20":101 if long else 99,"ema50":100}}

    def data(self):
        q={"open":"100","high":"100.1","low":"99.9","close":"100"}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def close(self,a):a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_hourly_ema_seed_reclaim_direction_and_equal_boundaries(self):
        ema=EMA20State()
        for _ in range(19):self.assertIsNone(ema.push(100))
        self.assertEqual(ema.push(100),100)
        self.assertAlmostEqual(ema.push(121),102)
        self.assertEqual(reclaim_signal({"close":101,"ema20":101,"ema200":100},{"close":103,"ema20":102,"ema200":100}),"long")
        self.assertEqual(reclaim_signal({"close":99,"ema20":99,"ema200":100},{"close":97,"ema20":98,"ema200":100}),"short")
        self.assertIsNone(reclaim_signal({"close":99,"ema20":101,"ema200":100},{"close":103,"ema20":102,"ema200":100}))
        self.assertIsNone(reclaim_signal({"close":101,"ema20":101,"ema200":100},{"close":102,"ema20":102,"ema200":100}))

    def test_retest_forbids_same_bar_then_freezes_level_and_efficiency_at_arm(self):
        s=RetestState();t=200*HOUR
        signal,ctx=s.push(t,self.feature(t),103,100)
        self.assertIsNone(signal);self.assertEqual(s.pending["level"],101)
        f=self.feature(t+HOUR,trend=False,close=101.5);f["efficiency24"]=.1
        signal,ctx=s.push(t+HOUR,f,102,100.9)
        self.assertEqual(signal,"long");self.assertEqual(ctx["arm_efficiency"],.4)
        self.assertEqual(ctx["arm_available_us"],t);self.assertEqual(ctx["wait_hours"],1)
        self.assertIsNone(s.pending)
        self.assertIsNone(s.push(t+2*HOUR,self.feature(t+2*HOUR,trend=False,close=101.5),102,100.9)[0])

    def test_pending_same_direction_does_not_refresh_and_sixth_hour_inclusive(self):
        for wait,expected in ((6,"long"),(7,None)):
            s=RetestState();t=200*HOUR;s.push(t,self.feature(t),103,102)
            # A repeated breakout does not move the original level or expiration.
            repeated=self.feature(t+HOUR,close=104);repeated["prior_high"]=103
            s.push(t+HOUR,repeated,105,102)
            self.assertEqual(s.pending["arm_available_us"],t)
            self.assertEqual(s.pending["level"],101)
            self.assertEqual(s.push(t+wait*HOUR,self.feature(t+wait*HOUR,trend=False,close=101.5),102,100.9)[0],expected)

    def test_short_retest_and_pending_cancel_when_trend_lost(self):
        s=RetestState();t=200*HOUR;s.push(t,self.feature(t,"short"),100,97)
        signal,ctx=s.push(t+HOUR,self.feature(t+HOUR,"short",False,98.5),99.1,98)
        self.assertEqual(signal,"short");self.assertEqual(ctx["level"],99)
        s=RetestState();s.push(t,self.feature(t),103,102)
        lost=self.feature(t+HOUR,trend=False);lost["higher_4h"]["ema20"]=99
        self.assertIsNone(s.push(t+HOUR,lost,103,100)[0]);self.assertIsNone(s.pending)

    def test_closed_hour_only_updates_ema_and_previous_reclaim_context(self):
        h=NinthHour()
        for hour in range(201):
            close=90 if hour<180 else 104 if hour==199 else 110
            high=91 if hour<180 else 111
            low=89 if hour<180 else 103 if hour==199 else 109
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":str(close),"high":str(high),"low":str(low),"close":str(close)})
        self.assertEqual(f["entry_signals"]["ema20_reclaim"],"long")
        self.assertEqual(f["entry_contexts"]["ema20_reclaim"]["previous"]["available_us"],200*HOUR)
        old_ema,old_previous=h.ema.value,deepcopy(h.previous)
        closed,_=h.push({"open":"110","high":"10000","low":"1","close":"10000"})
        self.assertFalse(closed);self.assertEqual(h.ema.value,old_ema);self.assertEqual(h.previous,old_previous)

    def test_both_entries_use_next_minute_adverse_open_and_keep_original_exit(self):
        for case in ("ema20_reclaim","breakout_retest"):
            for side in ("long","short"):
                with self.subTest(case=case,side=side),tempfile.TemporaryDirectory() as folder:
                    a=self.fixture(folder,case);t=a.ledger.start+4*HOUR;long=side=="long"
                    previous={"available_us":t-HOUR,"close":99.5 if long else 100.5,"ema20":100,"ema200":99 if long else 101}
                    current={"available_us":t,"close":100,"ema20":99.9 if long else 100.1,"ema200":99 if long else 101}
                    ctx={"previous":previous,"current":current} if case=="ema20_reclaim" else {
                        "side":side,"arm_available_us":t-HOUR,"expiry_us":t+5*HOUR,"confirmation_available_us":t,
                        "level":99.5 if long else 100.5,"arm_close":102 if long else 98,"wait_hours":1}
                    f={"close":100,"atr":1,"ema50":100,"ema200":99 if long else 101,"mean":100,"trend":None,
                        "entry_signals":{case:side},"entry_contexts":{case:ctx},
                        "higher_4h":{"available_us":t,"ema20":101 if long else 99,"ema50":100}}
                    saved=deepcopy(f);a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None});self.assertEqual(f,saved)
                    a.minute(t,self.data(),[],False);self.assertFalse(a.ledger._state["positions"])
                    a.minute(t+MINUTE,self.data(),[],False);self.assertIn("BTCUSDT",a.ledger._state["positions"])
                    price=a.ledger._state["positions"]["BTCUSDT"]["entry"]
                    self.assertEqual(price,D("100.02") if long else D("99.98"))
                    f["close"]=99 if long else 101;f["entry_signals"][case]=None
                    a.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
                    self.assertEqual(a.queue[t+HOUR+MINUTE][0]["kind"],"exit")
                    a.minute(t+HOUR+MINUTE,self.data(),[],False)
                    self.assertEqual(a.ledger.equity,D(100)+D(a.trades[-1]["net_pnl"]))
                    if case=="breakout_retest":self.assertEqual(a.trades[-1]["retest_wait_hours"],1)
                    self.close(a)

if __name__=="__main__":unittest.main()
