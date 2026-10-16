from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
import sys,tempfile,tomllib,unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from perpquant.config import load
from perpquant.engine import MINUTE,HOUR
from perpquant.ledger import InstrumentRules
from perpquant.margin import Tier
from perpquant.research4 import FourthAccount
from perpquant.universe_data import SYMBOLS,load_plan,load_coverage,parse_variable_funding
from perpquant.universe_engine import UniverseAccount,UniverseLedger

class UniverseTests(unittest.TestCase):
    def fixture(self,folder,symbols=SYMBOLS,cost='base'):
        plan,base,config=load_plan(ROOT)
        config['market']['symbols']=symbols[:]
        rule=InstrumentRules('.01','.01','100','5',(Tier('0','1000000','.025','0','10'),),0,2_000_000_000_000_000,'SYNTHETIC_ONLY')
        rules={s:rule for s in symbols}
        return UniverseAccount(config,rules,{s:D('.01') for s in rules},{s:D('.0125') for s in rules},cost,Path(folder),plan=plan,name='test')

    def close(self,a):
        for f in ['event_stream','gate_log','sizing_log']:getattr(a,f).close()

    def data(self,symbols):
        row={'open':'100','close':'100','high':'100.1','low':'99.9'}
        return {s:{'trade':row.copy(),'mark':row.copy()} for s in symbols}

    def test_only_universe_changes_original_config_unmodified(self):
        _,base,expanded=load_plan(ROOT)
        original=deepcopy(base)
        expanded['market']['symbols']=SYMBOLS[:2]
        self.assertEqual(expanded,base)
        self.assertEqual(load(ROOT/'configs/first_round.toml'),original)

    def test_new_symbols_share_one_cash_and_risk_budget(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder)
            t=a.ledger.start+MINUTE
            for s in SYMBOLS:
                a.entry(s,t,{'side':'long','stop':'98','signal_available_us':t-MINUTE},self.data(SYMBOLS))
            state=a.ledger._state
            gross,risk,pending,pendingrisk=a.ledger._risk_values(state)
            self.close(a)
            self.assertIn('BTCUSDT',state['positions'])
            self.assertIn('ETHUSDT',state['positions'])
            self.assertLess(len(state['positions']),len(SYMBOLS))
            self.assertLessEqual(risk+pendingrisk,a.ledger.equity*D('.01'))
            self.assertLessEqual(gross+pending,a.ledger.equity)
            self.assertEqual(a.ledger.equity,D('99.96639496')) # .42 quantity, 100.02 entry, 100 mark, .0006 entry fee.

    def test_new_symbol_closed_hour_gate_and_60sec_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder)
            t=a.ledger.start+HOUR
            feature={'close':100,'atr':1,'ema50':100,'trend':'long','efficiency24':.4,'higher_4h':{'available_us':t,'ema20':101,'ema50':100}}
            features={s:None for s in SYMBOLS};features['LTCUSDT']=feature
            a.decisions(t,features)
            self.assertNotIn(t,a.queue)
            self.assertEqual(a.queue[t+MINUTE][0]['symbol'],'LTCUSDT')
            a.minute(t+MINUTE,self.data(SYMBOLS),[],False)
            self.assertIn('LTCUSDT',a.ledger._state['positions'])
            feature.update(close=99,efficiency24=0)
            a.decisions(t+HOUR,features)
            self.assertEqual(a.queue[t+HOUR+MINUTE][0]['kind'],'exit')
            self.close(a)

    def test_two_symbol_decisions_and_cash_match_original_both_costs(self):
        for cost in ['base','stress']:
            with self.subTest(cost=cost),tempfile.TemporaryDirectory() as folder:
                a=self.fixture(folder,SYMBOLS[:2],cost)
                oldplan=tomllib.loads((ROOT/'configs/fourth_round.toml').read_text(encoding='utf-8'))
                b=FourthAccount(a.config,a.rules,a.ticks,a.liquidation_fees,'efficiency24',cost,Path(folder),plan=oldplan)
                t=a.ledger.start+HOUR
                f={'close':100,'atr':1,'ema50':100,'trend':'long','efficiency24':.4,'higher_4h':{'available_us':t,'ema20':101,'ema50':100}}
                for x in [a,b]:x.decisions(t,{'BTCUSDT':f,'ETHUSDT':None});x.minute(t+MINUTE,self.data(SYMBOLS[:2]),[],False)
                self.assertEqual(a.ledger._state['positions'],b.ledger._state['positions'])
                self.assertEqual(a.gate_counts,b.gate_counts)
                f.update(close=99,efficiency24=.1)
                for x in [a,b]:x.decisions(t+HOUR,{'BTCUSDT':f,'ETHUSDT':None});x.minute(t+HOUR+MINUTE,self.data(SYMBOLS[:2]),[],False)
                self.assertEqual(a.trades,b.trades)
                self.assertEqual(a.ledger.equity,b.ledger.equity)
                self.close(a);self.close(b)

    def test_funding_transition_accepts_observed_previous_interval_not_missing_events(self):
        # Eight hours followed by four hours; report interval changes at the event.
        start=1672531200000000
        body=b'calc_time,funding_interval_hours,last_funding_rate\n1672531200000,8,0.0001\n1672560000000,4,0.0002\n1672574400000,4,0.0001\n'
        parsed,unit,changes=parse_variable_funding(body,start,start+16*HOUR)
        self.assertEqual(len(parsed),3);self.assertEqual(unit,'ms');self.assertEqual(len(changes),1)
        with self.assertRaises(ValueError):parse_variable_funding(body.replace(b'1672574400000',b'1672578000000'),start,start+16*HOUR)

    def test_common_window_and_universe_extension_keep_original_protection_checks(self):
        coverage=load_coverage(ROOT)
        self.assertEqual(coverage['common_complete_end_exclusive'],'2024-07-01T00:00:00Z')
        self.assertFalse(coverage['full_2023_2025_result_claim_permitted'])
        with tempfile.TemporaryDirectory() as folder:
            a=self.fixture(folder)
            broken=deepcopy(a.config)
            broken['market']['auto_add_margin']=True
            rules=a.rules
            self.close(a)
            with self.assertRaisesRegex(ValueError,'Auto-add margin'):
                UniverseLedger(broken,rules,funding_policy='available_first_credit_cash_research_v1')

if __name__=='__main__':unittest.main()
