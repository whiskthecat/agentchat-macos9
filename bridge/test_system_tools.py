#!/usr/bin/env python3
"""Read-only query validation/composition. No Mac or inference required."""
import unittest
from remote_mac import MacTools, MacError, TOOLS
class ToolsTests(unittest.TestCase):
    def setUp(self):
        self.tools=MacTools(None,None); self.calls=[]
        def rpc(op,**args):
            self.calls.append((op,args))
            if op=='processes':return {'processes':[{'name':'Finder','partition_bytes':123}]}
            return {'source':op}
        self.tools.rpc=rpc
    def test_report_composes_native_facts(self):
        r=self.tools.call('imac_get_system_configuration',{})['structuredContent']
        self.assertEqual(r['hardware']['source'],'hardware')
        self.assertEqual(r['processes'][0]['name'],'Finder')
        self.assertEqual([op for op,_ in self.calls],['system','hardware','processes'])
    def test_selector_exact_spaces_and_all_bits(self):
        for selector in ('vm  ','ram ','0x00000000','0xFFFFFFFF'):
            self.tools.call('imac_system_probe',{'kind':'gestalt','selector':selector})
            self.assertEqual(self.calls[-1],('system_probe',{'path':selector,'fork':'gestalt'}))
    def test_reject_before_native_call(self):
        for args in ({'kind':'gestalt','selector':'vm'}, {'kind':'gestalt','selector':'ab\tc'},
                     {'kind':'gestalt','selector':'0xZZZZZZZZ'}, {'kind':'registry','path':'foo','property':'p\tx'},
                     {'kind':'registry','path':'','property':'name'}, {'kind':'setter','selector':'vm  '}):
            with self.assertRaises((MacError,ValueError)):self.tools.call('imac_system_probe',args)
        self.assertFalse(self.calls)
    def test_folder_inventory_and_read_only_schemas(self):
        self.tools.call('imac_list_system_items',{'folder':'disabled_extensions','start':25,'limit':8})
        self.assertEqual(self.calls,[('system_items',{'path':'extD','offset':25,'count':8})])
        for name in ('get_system_configuration','list_system_items','system_probe','get_idle_metrics'):
            t=next(t for t in TOOLS if t['name']=='imac_'+name)
            self.assertTrue(t['annotations']['readOnlyHint'])
    def test_exact_registry_property(self):
        self.tools.call('imac_system_probe',{'kind':'registry','path':'Devices:device-tree:cpus:PowerPC,G3','property':'d-cache-size'})
        self.assertEqual(self.calls[-1][0],'registry_probe')
if __name__=='__main__':unittest.main()
