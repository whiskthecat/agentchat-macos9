"""Process Manager subtraction, bounds and observer policy; no guest/inference."""
import unittest
import base64,json
from types import SimpleNamespace
from unittest.mock import patch
from remote_mac import MacBroker,MacError,MacTools,TOOLS,process_usage_delta

def snap(t,used=10,**extra):
    p=dict(name='Finder',high=0,low=123,launch_ticks=20,active_ticks=used,sample_ticks=t)
    return dict(sample_start_ticks=t,sample_end_ticks=t,ticks_per_second=60,
                processes=[p],truncated=False,**extra)
class UsageTests(unittest.TestCase):
    def test_native_time_and_signed_residual(self):
        r=process_usage_delta(snap(100),snap(160,25))
        self.assertEqual((r['elapsed_ticks'],r['measured_active_ticks'],r['unaccounted_ticks']),(60,15,45))
        self.assertEqual(r['processes'][0]['active_percent'],25)
        self.assertIsNone(r['interrupt_ticks'])
        r=process_usage_delta(snap(100),snap(160,75))
        self.assertEqual(r['unaccounted_ticks'],-5)
        self.assertGreater(r['processes'][0]['active_percent'],100)
    def test_unsigned_clock_and_counter_wrap(self):
        a=snap(0xfffffff0,0xfffffff8);b=snap(44,8)
        r=process_usage_delta(a,b)
        self.assertEqual(r['elapsed_ticks'],60)
        self.assertEqual(r['measured_active_ticks'],16)
    def test_process_churn_and_psn_reuse(self):
        a=snap(100);b=snap(160,25);b['processes'][0]['launch_ticks']=130
        r=process_usage_delta(a,b)
        self.assertEqual({p['status'] for p in r['processes']},{'started','exited'})
        self.assertIsNone(r['unaccounted_ticks']);self.assertFalse(r['accounting_complete'])
    def test_truncation_and_discontinuity_are_unknown(self):
        a=snap(100);b=snap(160,25);b['truncated']=True
        self.assertIsNone(process_usage_delta(a,b)['unaccounted_ticks'])
        r=process_usage_delta(snap(100),snap(160,1))
        self.assertEqual(r['processes'][0]['status'],'counter_discontinuity')
        self.assertIsNone(r['unaccounted_ticks'])
    def test_old_client_and_zero_clock_fail(self):
        with self.assertRaises(MacError):process_usage_delta({'processes':[]},snap(100))
        with self.assertRaises(MacError):process_usage_delta(snap(100),snap(100))
    def test_tool_bounds_and_exactly_two_scans(self):
        t=MacTools(None,None);calls=[]
        def rpc(op,**kw):
            calls.append(op);return snap(100 if len(calls)==1 else 220,10 if len(calls)==1 else 40)
        t.rpc=rpc
        with patch('remote_mac.time.sleep') as sleep:
            r=t.call('imac_get_process_usage',{'seconds':2})['structuredContent']
        sleep.assert_called_once_with(2);self.assertEqual(calls,['processes','processes'])
        self.assertEqual(r['elapsed_seconds'],2)
        for seconds in (0,61,True,1.5):
            with self.assertRaises(ValueError):t.call('imac_get_process_usage',{'seconds':seconds})
        self.assertEqual(len(calls),2)
        self.assertTrue(next(t for t in TOOLS if t['name']=='imac_get_process_usage')['annotations']['readOnlyHint'])
    def test_reconnect_during_interval_never_reads_replacement(self):
        broker=MacBroker();session=SimpleNamespace(alive=True,connection_id='first');sent=[]
        def send(line):
            sent.append(line);nonce=line.split('\t')[1]
            broker.reply(session,'first','E:'+nonce+'\t0\t'+base64.b64encode(json.dumps(snap(100)).encode()).decode())
        session.send=send;tools=MacTools(broker,session)
        def reconnect(_):session.connection_id='replacement'
        with patch('remote_mac.time.sleep',side_effect=reconnect):
            with self.assertRaises(MacError):tools.call('imac_get_process_usage',{})
        self.assertEqual(len(sent),1);self.assertFalse(broker.pending)
if __name__=='__main__':unittest.main()
