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
from perpquant.research5 import FactorState
from perpquant.research6 import flow_signal,SixthHour,SixthAccount
from perpquant.variants import VariantHour

class FlowTests(unittest.TestCase):
    def fixture(self,folder):
        config=load(ROOT/"configs/first_round.toml")
        plan=tomllib.loads((ROOT/"configs/sixth_round.toml").read_text(encoding="utf-8"))
        rule=InstrumentRules(".01",".01","100","5",(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return SixthAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},"flow_cross","base",Path(folder),plan=plan)

    def data(self):
        q={"open":"100","close":"100","high":"100.1","low":"99.9"}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def feature(self,t,side):
        long=side=="long"
        return {"close":100,"atr":1,"ema50":100,"ema200":99 if long else 101,"mean":100,"trend":side,
            "previous_flow_share":.5,"flow_share":.6 if long else .4,"previous_flow_available_us":t-HOUR,
            "legacy_breakout_signal":None,"higher_4h":{"available_us":t,"ema20":101 if long else 99,"ema50":100}}

    def close(self,a):a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_cross_is_new_threshold_transition_not_continuous_level(self):
        self.assertEqual(flow_signal(.50,.55,101,100),"long")
        self.assertEqual(flow_signal(.50,.45,99,100),"short")
        self.assertIsNone(flow_signal(.55,.60,101,100))
        self.assertIsNone(flow_signal(.45,.40,99,100))
        self.assertIsNone(flow_signal(None,.60,101,100))
        self.assertIsNone(flow_signal(.5,None,101,100))

    def test_price_state_forbids_opposite_or_equal_ema_and_invalid_share(self):
        self.assertIsNone(flow_signal(.50,.60,99,100))
        self.assertIsNone(flow_signal(.50,.40,101,100))
        self.assertIsNone(flow_signal(.50,.60,100,100))
        with self.assertRaises(ValueError):flow_signal(-.1,.6,101,100)

    def test_hourly_history_closed_only_zero_volume_resets_and_breakout_not_needed(self):
        h=SixthHour.__new__(SixthHour)
        h.parent=VariantHour();h.state=FactorState();h.volume={i*HOUR:(10,5) for i in range(1,205)}
        h.previous_share=h.previous_available=None
        h.volume[201*HOUR]=(10,6)
        h.volume[202*HOUR]=(0,0)
        h.volume[203*HOUR]=(10,6)
        for hour in range(201):
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":"101","low":"99","close":"100.5" if hour==200 else "100"})
        self.assertEqual(f["trend"],"long");self.assertIsNone(f["legacy_breakout_signal"])
        self.assertEqual(f["previous_flow_available_us"],200*HOUR)
        old=h.previous_share
        for hour in (201,202):
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":"101","low":"99","close":"100.5"})
                if hour==201 and minute==0:self.assertEqual(h.previous_share,old)
            self.assertIsNone(f["trend"])
        self.assertIsNone(f["previous_flow_share"])

    def test_both_sides_enter_outside_breakout_only_after_delay_and_preserve_exit(self):
        for side in ("long","short"):
            with self.subTest(side=side),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder);t=a.ledger.start+HOUR
                f=self.feature(t,side);before=deepcopy(f)
                a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
                self.assertEqual(f,before)
                action=a.queue[t+MINUTE][0]
                self.assertFalse(action["legacy_breakout_overlap"])
                a.minute(t,self.data(),[],False)
                self.assertFalse(a.ledger._state["positions"])
                a.minute(t+MINUTE,self.data(),[],False)
                self.assertIn("BTCUSDT",a.ledger._state["positions"])
                a.minute(t+2*MINUTE,self.data(),[],True)
                self.assertFalse(a.ledger._state["positions"])
                self.assertEqual(a.trades[-1]["entry_mechanism"],"independent_flow_cross")
                self.assertFalse(a.trades[-1]["legacy_breakout_overlap"])
                self.assertEqual(a.trades[-1]["entry_signal_available_us"],t)
                self.assertEqual(a.origin_counts["filled_outside_breakout"],1)
                self.assertEqual(a.ledger.equity,D(100)+D(a.trades[-1]["net_pnl"]))
                self.close(a)

    def test_original_4h_gate_blocks_entry_and_never_blocks_existing_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);t=a.ledger.start+HOUR
            f=self.feature(t,"long");f["higher_4h"]["ema20"]=99
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE],[])
            f["higher_4h"]["ema20"]=101
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            a.minute(t+MINUTE,self.data(),[],False)
            f.update(close=99,trend=None,previous_flow_share=None,flow_share=None)
            f["higher_4h"]["ema20"]=99
            a.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+HOUR+MINUTE][0]["kind"],"exit")
            a.minute(t+HOUR+MINUTE,self.data(),[],False)
            self.assertEqual(a.trades[-1]["exit_reason"],"hourly_exit")
            self.close(a)

    def test_rejects_stale_previous_hour_before_scheduling_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);t=a.ledger.start+2*HOUR
            f=self.feature(t,"long");f["previous_flow_available_us"]=t-2*HOUR
            with self.assertRaises(ValueError):a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.close(a)

if __name__=="__main__":unittest.main()
