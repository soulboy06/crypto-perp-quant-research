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
from perpquant.research7 import SlowChannel,CompressionState,SeventhHour,SeventhAccount,entry_signal

class EntryTests(unittest.TestCase):
    def fixture(self,folder,case):
        config=load(ROOT/"configs/first_round.toml")
        plan=tomllib.loads((ROOT/"configs/seventh_round.toml").read_text(encoding="utf-8"))
        rule=InstrumentRules(".01",".01","100","5",(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return SeventhAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},case,"base",Path(folder),plan=plan)

    def data(self):
        q={"open":"100","close":"100","high":"100.1","low":"99.9"}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def feature(self,t,case,side):
        long=side=="long"
        context={"available_us":t,"prior_high":99 if long else 102,"prior_low":98 if long else 101,
            "compression_ratio":.5,"expansion_ratio":2,"prior_recent_mean_tr":1,"prior_background_mean_tr":2,"current_tr":2}
        return {"close":100,"atr":1,"ema50":100,"ema200":99 if long else 101,"mean":100,"trend":None,
            "entry_signals":{"slow4h":side if case=="slow4h" else None,"compression_breakout":side if case=="compression_breakout" else None},
            "entry_contexts":{case:context},"higher_4h":{"available_us":t,"ema20":101 if long else 99,"ema50":100}}

    def close(self,a):a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_slow_channel_waits_full_utc_block_and_excludes_current(self):
        channel=SlowChannel()
        for h in range(1,49):
            value=channel.push(h*HOUR,101,99)
            if h%4:self.assertIsNone(value)
        for h in (49,50,51):self.assertIsNone(channel.push(h*HOUR,110,90))
        value=channel.push(52*HOUR,110,90)
        self.assertEqual(value["prior_high"],101);self.assertEqual(value["prior_low"],99)
        self.assertEqual(value["completed_4h_high"],110)
        self.assertEqual(entry_signal("slow4h",value,103,100),"long")
        with self.assertRaises(ValueError):SlowChannel().push(2*HOUR,101,99)

    def test_hour_features_cannot_carry_4h_signal_into_next_hour(self):
        h=SeventhHour()
        for hour in range(213):
            price="101.5" if hour>=211 else "100"
            high="102" if hour>=211 else "101"
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":high,"low":"99","close":price})
                if hour==211 and minute<59:self.assertFalse(closed)
            if hour==211:
                self.assertEqual(f["entry_signals"]["slow4h"],"long")
                self.assertEqual(f["entry_contexts"]["slow4h"]["available_us"],212*HOUR)
        self.assertIsNone(f["entry_signals"]["slow4h"])
        self.assertIsNone(f["entry_contexts"]["slow4h"])

    def test_compression_uses_nonoverlapping_prior_windows_and_excludes_breakout(self):
        state=CompressionState()
        for _ in range(24):state.push(102,98,100)
        for _ in range(6):state.push(100.5,99.5,100)
        result=state.push(103,99,102)
        self.assertEqual(result["prior_background_mean_tr"],4)
        self.assertEqual(result["prior_recent_mean_tr"],1)
        self.assertEqual(result["compression_ratio"],.25)
        self.assertEqual(result["expansion_ratio"],4)
        self.assertEqual(result["prior_high"],100.5)
        self.assertEqual(entry_signal("compression_breakout",result,102,100),"long")

    def test_compression_short_zero_range_and_tr_gap(self):
        state=CompressionState()
        for _ in range(24):state.push(102,98,100)
        for _ in range(6):state.push(100.5,99.5,100)
        result=state.push(101,97,98)
        self.assertEqual(entry_signal("compression_breakout",result,98,100),"short")
        zero=CompressionState()
        for _ in range(30):zero.push(100,100,100)
        result=zero.push(102,100,102)
        self.assertIsNone(entry_signal("compression_breakout",result,102,100))
        gap=CompressionState();gap.push(101,99,100)
        self.assertEqual(gap.push(121,120,120.5)["current_tr"],21)

    def test_noncompression_or_no_expansion_or_wrong_price_state_rejects(self):
        context={"prior_high":101,"prior_low":99,"compression_ratio":.75,"expansion_ratio":1.5}
        self.assertEqual(entry_signal("compression_breakout",context,102,100),"long")
        for key,value in (("compression_ratio",.76),("expansion_ratio",1.49)):
            changed={**context,key:value}
            self.assertIsNone(entry_signal("compression_breakout",changed,102,100))
        self.assertIsNone(entry_signal("compression_breakout",context,102,103))

    def test_both_mechanisms_and_sides_keep_delay_original_hourly_exit_and_cash(self):
        for case in ("slow4h","compression_breakout"):
            for side in ("long","short"):
                with self.subTest(case=case,side=side),tempfile.TemporaryDirectory() as folder:
                    a=self.fixture(folder,case);t=a.ledger.start+4*HOUR
                    f=self.feature(t,case,side);original=deepcopy(f)
                    a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
                    self.assertEqual(f,original)
                    a.minute(t,self.data(),[],False);self.assertFalse(a.ledger._state["positions"])
                    a.minute(t+MINUTE,self.data(),[],False)
                    self.assertIn("BTCUSDT",a.ledger._state["positions"])
                    # Exit must remain hourly even when entry is sampled every 4h.
                    f["close"]=99 if side=="long" else 101
                    f["entry_signals"]={"slow4h":None,"compression_breakout":None}
                    a.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
                    self.assertEqual(a.queue[t+HOUR+MINUTE][0]["kind"],"exit")
                    a.minute(t+HOUR+MINUTE,self.data(),[],False)
                    self.assertEqual(a.trades[-1]["exit_reason"],"hourly_exit")
                    self.assertEqual(a.ledger.equity,D(100)+D(a.trades[-1]["net_pnl"]))
                    self.assertEqual(a.origin_counts["filled_outside_breakout"],1)
                    self.close(a)

    def test_direction_gate_and_partial_four_hour_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder,"slow4h");t=a.ledger.start+4*HOUR
            f=self.feature(t,"slow4h","long");f["higher_4h"]["ema20"]=99
            a.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
            self.assertEqual(a.queue[t+MINUTE],[])
            f["higher_4h"]["ema20"]=101
            with self.assertRaises(ValueError):a.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
            self.close(a)

if __name__=="__main__":unittest.main()
