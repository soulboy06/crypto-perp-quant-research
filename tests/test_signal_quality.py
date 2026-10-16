from pathlib import Path
import sys,tempfile,unittest
from decimal import Decimal as D
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.engine import HOUR,MINUTE
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.universe_data import SYMBOLS,load_plan
from perpquant.universe_engine import UniverseAccount
from perpquant.signal_quality import ExtensionAccount,extension_assessment,load_signal_plan

class SignalTests(unittest.TestCase):
    def fixture(self,folder,filtered=True,name='candidate'):
        universe,_,config=load_plan(ROOT)
        signal,_,_=load_signal_plan(ROOT)
        rule=InstrumentRules('.01','.01','100','5',(Tier('0','1000000','.025','0','10'),),0,2_000_000_000_000_000,'SYNTHETIC_ONLY')
        rules={s:rule for s in SYMBOLS}
        cls=ExtensionAccount if filtered else UniverseAccount
        kw={'signal_plan':signal} if filtered else {}
        return cls(config,rules,{s:D('.01') for s in rules},{s:D('.0125') for s in rules},'base',Path(folder),plan=universe,name=name,**kw)

    def close(self,a):
        for field in ['event_stream','gate_log','sizing_log']:getattr(a,field).close()

    def feature(self,t,close=102):
        return {'close':close,'mean':100,'atr':1,'ema50':100,'trend':'long','efficiency24':.4,
                'higher_4h':{'available_us':t,'ema20':101,'ema50':100}}

    def data(self):
        row={'open':'102','close':'102','high':'102.1','low':'101.9'}
        return {s:{'trade':row.copy(),'mark':row.copy()} for s in SYMBOLS}

    def test_directional_units_symmetry_inclusive_boundary(self):
        self.assertTrue(extension_assessment({'close':102,'mean':100,'atr':1},'long')['accepted'])
        self.assertTrue(extension_assessment({'close':98,'mean':100,'atr':1},'short')['accepted'])
        self.assertFalse(extension_assessment({'close':102.001,'mean':100,'atr':1},'long')['accepted'])
        self.assertFalse(extension_assessment({'close':97.999,'mean':100,'atr':1},'short')['accepted'])
        a=extension_assessment({'close':102,'mean':100,'atr':1},'long')
        b=extension_assessment({'close':10200,'mean':10000,'atr':100},'long')
        self.assertEqual(a,b)

    def test_invalid_feature_rejects_without_division(self):
        for atr in [0,-1,None,float('nan')]:
            self.assertFalse(extension_assessment({'close':102,'mean':100,'atr':atr},'long')['accepted'])

    def test_overextension_rejects_entry_but_keeps_original_parent_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);b=self.fixture(folder,False,'control')
            t=a.ledger.start+HOUR
            fs={s:None for s in SYMBOLS};fs['XRPUSDT']=self.feature(t,103)
            try:
                for x in [a,b]:x.decisions(t,fs)
                self.assertEqual(a.queue[t+MINUTE],[])
                self.assertEqual(b.queue[t+MINUTE][0]['symbol'],'XRPUSDT')
                self.assertEqual(a.gate_counts['extension_reject'],1)
            finally:self.close(a);self.close(b)

    def test_passed_signal_same_fill_and_exit_unfiltered_even_when_extension_large(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder);b=self.fixture(folder,False,'control')
            t=a.ledger.start+HOUR
            fs={s:None for s in SYMBOLS};fs['XRPUSDT']=self.feature(t,102)
            try:
                for x in [a,b]:
                    x.decisions(t,fs)
                    self.assertNotIn(t,x.queue)
                    x.minute(t+MINUTE,self.data(),[],False)
                self.assertEqual(a.config,b.config)
                self.assertEqual(a.ledger._state['positions'],b.ledger._state['positions'])
                fs['XRPUSDT'].update(close=99,mean=90,ema50=100)
                for x in [a,b]:
                    x.decisions(t+HOUR,fs)
                    self.assertEqual(x.queue[t+HOUR+MINUTE][0]['kind'],'exit')
                    x.minute(t+HOUR+MINUTE,self.data(),[],False)
                self.assertEqual(a.trades,b.trades)
                self.assertEqual(a.ledger.equity,b.ledger.equity)
            finally:self.close(a);self.close(b)

if __name__=='__main__':unittest.main()
