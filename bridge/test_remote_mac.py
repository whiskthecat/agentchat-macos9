#!/usr/bin/env python3
"""Focused RPC correlation and MCP protocol tests. No model or guest required."""
import base64
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from remote_mac import MacBroker


class RemoteTests(unittest.TestCase):
    def test_nonce_connection_and_response_integrity(self):
        broker = MacBroker()
        session = SimpleNamespace(alive=True, connection_id="new")
        sent = threading.Event(); wire = []
        session.send = lambda line: (wire.append(line), sent.set())
        result = []
        worker = threading.Thread(target=lambda: result.append(broker.request(session, "new", "state")))
        worker.start(); self.assertTrue(sent.wait(2))
        nonce = wire[0].split("\t")[1]
        encoded = base64.b64encode(b'{"version":"56"}').decode()
        response = "E:" + nonce + "\t0\t" + encoded
        self.assertFalse(broker.reply(session, "old", response))
        self.assertFalse(broker.reply(SimpleNamespace(), "new", response))
        self.assertFalse(broker.reply(session, "new", "E:unknown\t0\t" + encoded))
        self.assertFalse(broker.reply(session, "new", "E:" + nonce + "\t0\tbad"))
        self.assertTrue(broker.reply(session, "new", response)); worker.join(2)
        self.assertEqual(result, [{"version": "56"}])
        self.assertEqual(broker.pending, {})
        self.assertTrue(wire[0].startswith("C:RPC\t"))

    def test_disconnect_wakes_waiter_and_stale_connection_fails(self):
        broker = MacBroker(); sent = threading.Event(); errors = []
        session = SimpleNamespace(alive=True, connection_id="original", send=lambda line: sent.set())
        def request():
            try: broker.request(session, "original", "stat", path="Disk:File")
            except RuntimeError as e: errors.append(str(e))
        worker = threading.Thread(target=request); worker.start(); self.assertTrue(sent.wait(2))
        broker.disconnected(session, "original"); worker.join(2)
        self.assertEqual(len(errors), 1); self.assertEqual(broker.pending, {})
        session.connection_id = "replacement"
        with self.assertRaises(RuntimeError): broker.request(session, "original", "state")

    def test_invalid_requests_never_reach_client(self):
        broker = MacBroker(); sent = []
        session = SimpleNamespace(alive=True, connection_id="c", send=sent.append)
        for values in ({"op": "P:bad"}, {"op": "state", "offset": -1}, {"op": "stat", "path": "x" * 256},
                       {"op": "read", "fork": "bogus"}, {"op": "write_begin", "expected_crc": "x\nC:QUIT"}):
            with self.assertRaises(ValueError): broker.request(session, "c", **values)
        self.assertFalse(sent)

    def test_stdio_mcp_discovery_without_guest(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "offline-test", "version": "1"}, "capabilities": {}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"}]
        proc = subprocess.run([sys.executable, str(Path(__file__).with_name("mac_mcp.py")), "--admin-port", "1"],
                              input="\n".join(json.dumps(r) for r in requests)+"\n", text=True, capture_output=True, timeout=5, check=True)
        responses = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(responses), 3)
        self.assertEqual(responses[0]["result"]["protocolVersion"], "2025-06-18")
        tools = responses[1]["result"]["tools"]
        self.assertEqual(len(tools), 42)
        self.assertIn("imac_get_process_usage", {t["name"] for t in tools})
        self.assertIn("imac_restore_file_info", {t["name"] for t in tools})
        self.assertEqual(len({t["name"] for t in tools}), len(tools))
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))
        self.assertFalse(any("send_prompt" in t["name"] for t in tools))


if __name__ == "__main__": unittest.main()
