import unittest
from agent_trace_ledger import analyze

class TraceTests(unittest.TestCase):
    def test_cycle_is_reported_once(self):
        spans=[dict(id=a,parent=b,kind='tool',name=a,start_ms=0,end_ms=1) for a,b in [('a','b'),('b','a')]]
        self.assertEqual(len(analyze({'spans':spans})['graph_findings']),1)
    def test_unknown_price_is_not_zero_claim(self):
        r=analyze({'spans':[dict(id='a',kind='llm',model='unknown',start_ms=0,end_ms=1)]})
        self.assertEqual(r['unpriced_spans'],['a'])
    def test_decimal_cost(self):
        r=analyze({'rates':{'m':{'input':'0.1','output':'0.2'}},'spans':[dict(id='a',kind='llm',model='m',input_tokens=1000000,output_tokens=0,start_ms=0,end_ms=1)]})
        self.assertEqual(r['estimated_cost'],'0.1')
    def test_invalid_interval(self):
        with self.assertRaises(ValueError): analyze({'spans':[dict(id='a',kind='tool',name='x',start_ms=5,end_ms=1)]})
    def test_duplicate(self):
        s=dict(id='a',kind='tool',name='x',start_ms=0,end_ms=1)
        with self.assertRaises(ValueError): analyze({'spans':[s,s]})
