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
from perpquant.factor_data import volume_values
from perpquant.research5 import FactorState,FifthHour,FifthAccount,factor_gate,CASES
from perpquant.variants import VariantHour

class FactorTests(unittest.TestCase):
    def setUp(self):self.plan=tomllib.loads((ROOT/"configs/fifth_round.toml").read_text(encoding="utf-8"))

    def fixture(self,folder,case):
        config=load(ROOT/"configs/first_round.toml")
        rule=InstrumentRules(".01",".01","100","5",(Tier("0","1000000",".025","0","10"),),0,2_000_000_000_000_000,"SYNTHETIC_ONLY")
        rules={s:rule for s in config["market"]["symbols"]}
        return FifthAccount(config,rules,{s:D(".01") for s in rules},{s:D(".0125") for s in rules},case,"base",Path(folder),plan=self.plan)

    def data(self):
        q={"open":"100","high":"100.1","low":"99.9","close":"100"}
        return {s:{"trade":q.copy(),"mark":q.copy()} for s in ("BTCUSDT","ETHUSDT")}

    def factors(self,t):return {"relative_quote_volume":2,"taker_buy_share":.6,"close_location":.9,
        "settled_funding":{"settlement_time_us":t-HOUR,"known_time_us":t-HOUR+MINUTE,"rate_normalized_8h":.0001}}

    def close(self,a):a.event_stream.close();a.gate_log.close();a.sizing_log.close()

    def test_new_raw_fields_validate_nonnegative_and_buy_not_exceed_total(self):
        self.assertEqual(volume_values({"quote_volume":"10","taker_buy_quote_volume":"6"}),(D(10),D(6)))
        for quote,buy in (("-1","0"),("10","11"),("NaN","0"),("1","Infinity")):
            with self.assertRaises(ValueError):volume_values({"quote_volume":quote,"taker_buy_quote_volume":buy})

    def test_relative_volume_excludes_current_hour_and_zero_denominators(self):
        state=FactorState()
        for i in range(24):r=state.close((i+1)*HOUR,110,90,100,10,6)
        self.assertIsNone(r["relative_quote_volume"])
        r=state.close(25*HOUR,110,90,105,100,60)
        self.assertEqual(r["relative_quote_volume"],10)
        self.assertEqual(r["prior24_mean_quote_volume"],10)
        self.assertEqual(r["taker_buy_share"],.6)
        self.assertEqual(r["close_location"],.75)
        r=state.close(26*HOUR,100,100,100,0,0)
        self.assertIsNone(r["close_location"]);self.assertIsNone(r["taker_buy_share"])

    def test_settled_funding_has_knowledge_delay_normalization_and_no_future(self):
        state=FactorState()
        state.funding({"event_time_us":HOUR,"rate":".0002","interval_hours":"4"})
        r=state.close(HOUR,110,90,100,10,6)
        self.assertIsNone(r["settled_funding"])
        r=state.close(HOUR+MINUTE,110,90,100,10,6)
        self.assertEqual(r["settled_funding"]["rate_normalized_8h"],.0004)
        with self.assertRaises(ValueError):state.funding({"event_time_us":HOUR,"rate":".01","interval_hours":"8"})
        future=self.factors(HOUR)
        future["settled_funding"]["known_time_us"]=2*HOUR
        with self.assertRaises(ValueError):factor_gate("funding_crowding","long",HOUR,future,self.plan)
        stale=self.factors(30*HOUR)
        stale["settled_funding"]["settlement_time_us"]=HOUR
        self.assertFalse(factor_gate("funding_crowding","long",30*HOUR,stale,self.plan)[0])

    def test_directional_thresholds_and_missing_values(self):
        t=30*HOUR;f=self.factors(t)
        for case in CASES:self.assertTrue(factor_gate(case,"long",t,f,self.plan)[0])
        for case,key,value in (("volume_confirm","relative_quote_volume",.99),("taker_pressure","taker_buy_share",.54),("close_location","close_location",.74)):
            other=deepcopy(f);other[key]=value
            self.assertFalse(factor_gate(case,"long",t,other,self.plan)[0])
            other[key]=None;self.assertFalse(factor_gate(case,"long",t,other,self.plan)[0])
        f["taker_buy_share"]=.45;f["close_location"]=.25;f["settled_funding"]["rate_normalized_8h"]=-.0002
        self.assertTrue(factor_gate("taker_pressure","short",t,f,self.plan)[0])
        self.assertTrue(factor_gate("close_location","short",t,f,self.plan)[0])
        self.assertFalse(factor_gate("funding_crowding","short",t,f,self.plan)[0])
        self.assertTrue(factor_gate("funding_crowding","long",t,f,self.plan)[0])

    def test_partial_hour_cannot_change_factors(self):
        h=FifthHour.__new__(FifthHour)
        h.parent=VariantHour();h.state=FactorState();h.volume={i*HOUR:(10,6) for i in range(1,202)}
        for hour in range(201):
            for minute in range(60):
                closed,f=h.push({"available_time_us":(hour*60+minute+1)*MINUTE,"open":"100","high":"101","low":"99","close":"100"})
        self.assertTrue(closed);self.assertEqual(f["factors"]["relative_quote_volume"],1)
        previous=deepcopy(h.state.volumes)
        closed,_=h.push({"open":"100","high":"10000","low":"99","close":"10000"})
        self.assertFalse(closed);self.assertEqual(h.state.volumes,previous)

    def test_each_factor_keeps_entry_delay_financial_path_and_original_exits(self):
        for case in CASES:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder,case)
                b=Account(a.config,a.rules,a.ticks,a.liquidation_fees,"trend","base",Path(folder))
                t=a.ledger.start+HOUR
                f={"close":100,"atr":1,"ema50":100,"mean":100,"trend":"long",
                    "higher_4h":{"available_us":t,"ema20":101,"ema50":100},"factors":self.factors(t)}
                before=deepcopy(f)
                for account in (a,b):
                    account.decisions(t,{"BTCUSDT":f,"ETHUSDT":None})
                    account.minute(t,self.data(),[],False)
                    self.assertFalse(account.ledger._state["positions"])
                    account.minute(t+MINUTE,self.data(),[],False)
                    self.assertIn("BTCUSDT",account.ledger._state["positions"])
                self.assertEqual(f,before)
                f.update(close=99,factors={"relative_quote_volume":None,"taker_buy_share":None,"close_location":None,"settled_funding":None})
                for account in (a,b):
                    account.decisions(t+HOUR,{"BTCUSDT":f,"ETHUSDT":None})
                    self.assertEqual(account.queue[t+HOUR+MINUTE][0]["kind"],"exit")
                    account.minute(t+HOUR+MINUTE,self.data(),[],False)
                self.assertEqual(a.ledger.equity,b.ledger.equity);self.assertEqual(a.trades,b.trades)
                b.event_stream.close();self.close(a)

if __name__=="__main__":unittest.main()
