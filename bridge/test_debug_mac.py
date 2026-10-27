#!/usr/bin/env python3
"""Debugger semantics with synthetic RAM and QMP. No VM or inference needed."""
import base64
import json
import re
import socket
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from remote_mac import MacError, MacTools
from vm_debug import VmDebug


class Monitor:
    def __init__(self, path):
        self.state = "running"; self.commands=[]; self.msr=0xf032
        self.socket=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.socket.bind(path); self.socket.listen(); self.socket.settimeout(.1)
        self.alive=True
        self.thread=threading.Thread(target=self.serve,daemon=True); self.thread.start()

    def serve(self):
        while self.alive:
            try: conn,_=self.socket.accept()
            except socket.timeout: continue
            except OSError: return
            with conn,conn.makefile("rwb") as f:
                f.write(b'{"QMP":{"version":{}}}\n'); f.flush()
                for line in f:
                    request=json.loads(line); command=request['execute']; self.commands.append(command)
                    if command=='query-status': result={'running':self.state=='running','status':self.state}
                    elif command=='stop': self.state='paused'; result={}
                    elif command=='cont': self.state='running'; result={}
                    elif command=='human-monitor-command':
                        text=request['arguments']['command-line']
                        if text=='info registers': result='NIP 12345678 LR 12345000 MSR %08x\r\nGPR00 00000000 00123400 00000002 00000003\r\n' % self.msr
                        else:
                            count,address=re.fullmatch(r'x(?:p)?/(\d+)xb 0x([0-9a-f]+)',text).groups()
                            address=int(address,16); count=int(count)
                            result='\r\n'.join('%08x: %s' % (address+i,' '.join('0x%02x' % ((address+j)%256) for j in range(i,min(i+8,count)))) for i in range(0,count,8))
                            if not self.msr & 0x10 and text.startswith('x/'):
                                result='%08x: Cannot access memory' % address
                    else: result={}
                    # Events must not be confused with command responses.
                    f.write(b'{"event":"SYNTHETIC_EVENT"}\n')
                    f.write(json.dumps({'id':request['id'],'return':result}).encode()+b'\n'); f.flush()

    def close(self):
        self.alive=False; self.socket.close(); self.thread.join(1)


class DebugTests(unittest.TestCase):
    def test_exception_context_is_visible_and_virtual_failure_does_not_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor=Monitor(tmp+'/qmp'); debug=VmDebug(tmp+'/qmp')
            try:
                monitor.msr=0x1000
                debug.call('vm_pause', {'seconds':20})
                self.assertFalse(debug.call('vm_registers', {})['data_address_translation'])
                with self.assertRaisesRegex(RuntimeError, 'translation is disabled'):
                    debug.call('vm_read_memory', {'address':0x1f57ff70, 'count':32})
                self.assertEqual(monitor.state, 'paused')
                self.assertEqual(monitor.commands.count('cont'), 0)
                physical=debug.call('vm_read_memory', {'address':0, 'count':32, 'space':'physical'})
                self.assertEqual(base64.b64decode(physical['content']), bytes(range(32)))
                self.assertEqual(physical['cpu_msr'], '0x00001000')
            finally: debug.close(); monitor.close()

    def test_pause_lease_registers_memory_stale_resume_and_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor=Monitor(tmp+'/qmp'); debug=VmDebug(tmp+'/qmp')
            try:
                with self.assertRaises(RuntimeError): debug.call('vm_read_memory',{'address':0})
                first=debug.call('vm_pause',{'seconds':20})['pause_lease']
                second=debug.call('vm_pause',{'seconds':20})['pause_lease']
                with self.assertRaises(RuntimeError): debug.call('vm_resume',{'pause_lease':first})
                self.assertEqual(monitor.state,'paused')
                self.assertEqual(debug.call('vm_registers',{})['registers']['r1'],'0x00123400')
                data=debug.call('vm_read_memory',{'address':4096,'count':33})
                self.assertEqual(base64.b64decode(data['content']),bytes(range(33)))
                with self.assertRaises(ValueError): debug.call('vm_read_memory',{'address':0xffffffff,'count':2})
                debug.close(); self.assertEqual(monitor.state,'running')
                self.assertEqual(monitor.commands.count('stop'),1)
                self.assertEqual(monitor.commands.count('cont'),1)
            finally: debug.close(); monitor.close()

    def test_lease_auto_resumes_only_owned_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor=Monitor(tmp+'/qmp'); debug=VmDebug(tmp+'/qmp')
            try:
                monitor.state='paused'
                with self.assertRaises(RuntimeError): debug.call('vm_pause',{'seconds':1})
                debug.close(); self.assertEqual(monitor.state,'paused')
                debug.call('vm_resume',{})
                debug.call('vm_pause',{'seconds':1}); time.sleep(1.2)
                self.assertEqual(monitor.state,'running'); self.assertIsNone(debug.lease)
            finally: debug.close(); monitor.close()

    def test_vm_tools_work_without_mac_session_and_stay_disabled_by_default(self):
        from unittest.mock import patch
        with self.assertRaises(RuntimeError): VmDebug('').call('vm_status',{})
        with patch('remote_mac.VM_DEBUG') as vm:
            vm.call.return_value={'status':'paused'}
            result=MacTools(None,None).call('imac_vm_status',{})
            self.assertEqual(result['structuredContent']['status'],'paused')
            vm.call.assert_called_once_with('vm_status',{})

    def test_process_ram_boundaries_search_and_stale_partition(self):
        ram=bytearray(8000); ram[3070:3076]=b'needle'
        meta={'name':'Synthetic Mac app','base_address':0x10000,'partition_bytes':len(ram),'launch_ticks':17}
        class Broker:
            change=False
            def request(self,session,connection,op,**args):
                if op=='process_info': return dict(meta)
                at=args['offset']; data=bytes(ram[at:at+args['count']])
                return {**meta,'data':base64.b64encode(data).decode(),'bytes':len(data),
                        'launch_ticks':18 if self.change and at else 17}
        broker=Broker(); tools=MacTools(broker,SimpleNamespace(connection_id='synthetic'))
        args={'high':0,'low':7,'count':4000}
        found=tools.call('imac_find_process_memory',{**args,'pattern':'needle'})['structuredContent']
        self.assertEqual(found['matches'],[{'offset':3070,'address':0x10000+3070}])
        self.assertEqual(found['next_offset'],3995)
        tail=tools.call('imac_read_process_memory',{**args,'offset':7998})['structuredContent']
        self.assertEqual(tail['bytes'],2); self.assertIsNone(tail['next_offset'])
        for invalid in ({**args,'offset':8000},{**args,'offset':-1},{**args,'count':True},{**args,'count':65537}):
            with self.assertRaises(ValueError): tools.call('imac_read_process_memory',invalid)
        broker.change=True
        with self.assertRaises(MacError): tools.call('imac_read_process_memory',args)


if __name__=='__main__': unittest.main()
