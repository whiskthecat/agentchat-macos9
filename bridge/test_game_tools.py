"""New game workflow tools, isolated host fixtures and synthetic QMP only."""
import json
import hashlib
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from mac_archive import appledouble, import_archive, safe_name
from remote_mac import MacTools
from vm_debug import desktop_input


class InputTests(unittest.TestCase):
    def test_input_validation_before_any_events_and_cleanup_on_failure(self):
        events = []
        q = SimpleNamespace(command=lambda name,args: events.extend(args["events"]))
        for args in ({"action":"key","key":"quit"},{"action":"text","text":"é"},
                     {"action":"click","x":800,"y":0},{"action":"text","text":"hello","modifiers":"cmd"}):
            with self.assertRaises(ValueError): desktop_input(q,args)
        self.assertFalse(events)
        def fails(name,args):
            events.extend(args["events"])
            if len(events) == 2: raise RuntimeError("synthetic failure")
        with self.assertRaises(RuntimeError): desktop_input(SimpleNamespace(command=fails),{"action":"key","key":"n","modifiers":"cmd"})
        released = [e["data"]["key"]["data"] for e in events if e["type"] == "key" and not e["data"]["down"]]
        self.assertEqual(set(released),{"meta_l","n"})

    @patch("vm_debug.time.sleep")
    def test_text_shift_and_pointer_end_release(self,_):
        events=[]; q=SimpleNamespace(command=lambda name,args:events.extend(args["events"]))
        desktop_input(q,{"action":"text","text":"A: x"})
        downs=[e["data"]["key"]["data"] for e in events if e["data"].get("down")]
        self.assertEqual(downs,["shift","a","shift","semicolon","spc","x"])
        events.clear(); desktop_input(q,{"action":"drag","x":0,"y":0,"end_x":799,"end_y":599})
        self.assertEqual(events[-1]["data"],{"button":"left","down":False})
        self.assertEqual(events[-3]["data"],{"axis":"x","value":32767})


class ArchiveTests(unittest.TestCase):
    def test_appledouble_raw_fork_and_metadata_bounds(self):
        header=struct.pack(">II16sH",0x51607,0x20000,b"\0"*16,2)
        raw=header+struct.pack(">III",2,50,4)+struct.pack(">III",9,54,10)+b"RSRC"+b"APPLDALK"+b"\x21\x00"
        self.assertEqual(appledouble(raw)[2],b"RSRC")
        self.assertEqual(appledouble(raw)[9],b"APPLDALK\x21\x00")
        with self.assertRaises(ValueError): appledouble(raw[:-1])
        for name in ("/absolute","../escape","dir/../file","folder:bad","a"*32,"a\\b","a\0b"):
            with self.assertRaises(ValueError): safe_name(name)
        self.assertEqual(str(safe_name("Folder/Icon\r")),"Folder/Icon\r")

    def test_import_forks_metadata_and_owned_rollback(self):
        entry={"relative":"Game", "data":b"data", "resource":b"resource", "type":"APPL", "creator":"DALK", "flags":0x2100}
        class Mac:
            def __init__(self,fail=False): self.calls=[]; self.fail=fail
            def rpc(self,op,**args): self.calls.append((op,args)); return {}
            def import_file(self,args):
                data = Path(args["host_path"]).read_bytes()
                self.calls.append(("write",args["destination"],data,args))
                return {"written":len(data), "sha256":hashlib.sha256(data).hexdigest()}
            def call(self,name,args):
                self.calls.append((name,args))
                if self.fail: raise RuntimeError("metadata failed")
        with tempfile.TemporaryDirectory() as t, patch("mac_archive.plan_archive",return_value=([],[entry])):
            archive=Path(t)/"source"; archive.write_bytes(b"archive")
            for fork in ("data", "resource"):
                file=Path(t)/fork; file.write_bytes(entry[fork]); entry[fork]=file
            args={"host_path":str(archive),"destination":"Disk:New game"}
            mac=Mac(); report=import_archive(mac,args)
            self.assertEqual(report["files"][0]["finder_flags"],0x2000)
            writes=[c for c in mac.calls if c[0]=="write"]
            self.assertEqual(writes[0][2],b"data"); self.assertEqual(writes[1][2],b"resource")
            self.assertTrue(writes[0][3]["create_only"]); self.assertEqual(writes[1][3]["fork"],"resource")
            mac=Mac(True)
            with self.assertRaisesRegex(RuntimeError,"metadata failed"): import_archive(mac,args)
            self.assertEqual([c[1]["path"] for c in mac.calls if c[0]=="delete"],["Disk:New game:Game","Disk:New game"])
            mac=Mac()
            with self.assertRaisesRegex(ValueError,"checksum"): import_archive(mac,{**args,"expected_sha256":"incorrect"})
            self.assertFalse(mac.calls)

    def test_finder_metadata_omitted_values_preserved(self):
        mac=MacTools(None,SimpleNamespace(connection_id="fake")); calls=[]
        def rpc(op,**args):
            calls.append((op,args));return {"directory":False,"type":"TEXT","creator":"ttxt"}
        mac.rpc=rpc
        mac.call("imac_set_file_info",{"path":"Disk:File","type":"APPL"})
        self.assertEqual(calls[-1],("set_info",{"path":"Disk:File","data":b"APPLttxt","offset":0,"count":0}))
        with self.assertRaises(ValueError): mac.call("imac_set_file_info",{"path":"Disk:File","creator":"bad"})

    def test_restore_unsigned_catalog_dates_and_exact_flags(self):
        mac=MacTools(None,SimpleNamespace(connection_id="fake")); calls=[]
        args={"path":"Disk:File","finder_flags":0x8100,"created_mac":0xf1234567,"modified_mac":0xffffffff}
        mac.rpc=lambda op,**kw: calls.append((op,kw)) or args
        mac.call("imac_restore_file_info",args)
        self.assertEqual(calls,[("restore_info",{"path":"Disk:File","data":bytes.fromhex("8100f1234567ffffffff")}),
                              ("stat",{"path":"Disk:File"})])
        calls.clear()
        for key,value in [("finder_flags",65536),("created_mac",-1),("modified_mac",2**32),("created_mac",True)]:
            with self.assertRaises(ValueError):mac.call("imac_restore_file_info",{**args,key:value})
        self.assertEqual(calls,[])
        mac.rpc=lambda op,**kw: {} if op=='restore_info' else {**args,"modified_mac":args['modified_mac']-28800}
        with self.assertRaisesRegex(RuntimeError,"did not retain"):mac.call("imac_restore_file_info",args)


if __name__ == "__main__": unittest.main()
