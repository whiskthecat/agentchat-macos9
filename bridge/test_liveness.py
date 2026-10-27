#!/usr/bin/env python3
"""Recording-only admission and real Linux TCP blackhole checks. No P: or RPC.

Uses production handlers/listeners in a private namespace with per-socket BPF
packet drops, so healthy idle TCP is distinguished from a vanished endpoint.
"""
import importlib.util
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools/inspect"))
from relay_harness import blackhole


class LivenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        os.environ.update(AGENT_TEST_MODE="1", AGENT_STATE_FILE=cls.scratch.name + "/state.sqlite3",
                          OPENCODE_ENABLED="0", IMAC_TOOLS_ENABLED="0")
        spec = importlib.util.spec_from_file_location("liveness_relay", Path(__file__).with_name("agentd.py"))
        cls.relay = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.relay)
        cls.active = {}
        cls.lock = threading.Lock()
        cls.servers = []
        for kind in ("chat", "admin"):
            base = getattr(cls.relay, kind.title() + "Handler")
            def observed(parent, lane):
                class Handler(parent):
                    def handle(self):
                        with cls.lock:
                            cls.active[(lane, self.client_address[0])] = self
                        try:
                            super().handle()
                        finally:
                            with cls.lock:
                                if cls.active.get((lane, self.client_address[0])) is self:
                                    del cls.active[(lane, self.client_address[0])]
                return Handler
            server = cls.relay.Server(("127.0.0.1", 0), observed(base, kind))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            setattr(cls, kind + "_port", server.server_address[1])
            cls.servers.append(server)

    @classmethod
    def tearDownClass(cls):
        for server in cls.servers:
            server.shutdown(); server.server_close()
        cls.relay.CODEX.close()
        cls.scratch.cleanup()

    def wait_for(self, predicate, seconds=3):
        started = time.monotonic()
        while not predicate():
            if time.monotonic() - started > seconds:
                self.fail("Condition did not become true within %s seconds" % seconds)
            time.sleep(.05)
        return time.monotonic() - started

    def connect(self, kind, ip):
        connection = socket.socket()
        connection.settimeout(3)
        connection.bind((ip, 0))
        connection.connect(("127.0.0.1", getattr(self, kind + "_port")))
        stream = connection.makefile("rb")
        self.addCleanup(connection.close); self.addCleanup(stream.close)
        greeting = stream.readline()
        if kind == "chat" and greeting == b"H:alive\n":
            greeting = stream.readline()
        return connection, stream, greeting

    def test_01_quiet_connections_survive_multiple_failure_deadlines(self):
        chat, _, greeting = self.connect("chat", "127.0.0.2")
        admin, _, welcome = self.connect("admin", "127.0.0.4")
        self.assertEqual(greeting, b"S:connected\n")
        self.assertTrue(welcome.startswith(b"agentd admin console"))
        time.sleep(65)  # No app traffic; the kernel still answers TCP probes.
        self.assertTrue(self.relay.REG["127.0.0.2"].alive)
        with self.lock:
            sockets = [self.active[("chat", "127.0.0.2")].request,
                       self.active[("admin", "127.0.0.4")].request]
        for connection in sockets:
            self.assertEqual(connection.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
            self.assertEqual(connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 15)
            self.assertEqual(connection.getsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT), 30000)
        self.assertIsNone(self.relay.REG["127.0.0.2"].sid)
        chat.shutdown(socket.SHUT_RDWR); admin.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: not self.relay.REG["127.0.0.2"].alive)

    def test_02_duplicate_and_different_peers_cannot_evict_healthy_mac(self):
        first, _, _ = self.connect("chat", "127.0.0.5")
        _, _, duplicate = self.connect("chat", "127.0.0.5")
        self.assertIn(b"already connected", duplicate)
        second, _, _ = self.connect("chat", "127.0.0.6")
        _, _, full = self.connect("chat", "127.0.0.7")
        self.assertIn(b"2 sessions max", full)
        self.assertTrue(self.relay.REG["127.0.0.5"].alive)
        self.assertTrue(self.relay.REG["127.0.0.6"].alive)
        second.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: not self.relay.REG["127.0.0.6"].alive)
        self.assertIs(self.relay.CURRENT["session"], self.relay.REG["127.0.0.5"])
        first.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: not self.relay.REG["127.0.0.5"].alive)

    def test_03_vanished_chat_releases_slot_and_resumes_identity(self):
        connection, _, _ = self.connect("chat", "127.0.0.8")
        session = self.relay.REG["127.0.0.8"]
        old_id = session.connection_id
        with self.lock:
            blackhole(self.active[("chat", "127.0.0.8")].request)
        elapsed = self.wait_for(lambda: not session.alive, 55)
        print("chat blackhole released in %.3fs" % elapsed, flush=True)
        again, _, greeting = self.connect("chat", "127.0.0.8")
        self.assertEqual(greeting, b"S:connected\n")
        resumed = self.relay.REG["127.0.0.8"]
        self.assertNotEqual(resumed.connection_id, old_id)
        again.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: not resumed.alive)

    def test_04_vanished_admin_reader_is_reaped(self):
        _, _, _ = self.connect("admin", "127.0.0.9")
        with self.lock:
            blackhole(self.active[("admin", "127.0.0.9")].request)
        elapsed = self.wait_for(lambda: ("admin", "127.0.0.9") not in self.active, 40)
        print("admin blackhole released in %.3fs" % elapsed, flush=True)

    def test_05_resumed_slot_is_reserved_atomically_and_obeys_limit(self):
        relay = self.relay
        session = relay.Session("race", lambda _: None)
        session.alive = False; session.disconnected_at = time.monotonic()
        with relay.REG_LOCK:
            previous = relay.REG.copy(); relay.REG.clear(); relay.REG["race"] = session
        try:
            barrier = threading.Barrier(8)
            answers = []
            def attempt():
                barrier.wait(); answers.append(relay.acquire("race"))
            threads = [threading.Thread(target=attempt) for _ in range(8)]
            for thread in threads: thread.start()
            for thread in threads: thread.join()
            self.assertEqual(sum(s is session for s in answers), 1)
            session.alive = False; session.disconnected_at = time.monotonic()
            self.assertIsNotNone(relay.acquire("other1"))
            self.assertIsNotNone(relay.acquire("other2"))
            self.assertIsNone(relay.acquire("race"))
        finally:
            with relay.REG_LOCK:
                relay.REG.clear(); relay.REG.update(previous)

    def test_06_send_failure_wakes_the_blocked_reader(self):
        connection, _, _ = self.connect("chat", "127.0.0.10")
        with self.lock:
            handler = self.active[("chat", "127.0.0.10")]
        original = handler.wfile
        class BrokenWriter:
            closed = False
            def write(self, data): raise BrokenPipeError("recording write failure")
            def flush(self): pass
            def close(self): original.close()
        handler.wfile = BrokenWriter()
        self.relay.REG["127.0.0.10"].send("S:test")
        self.wait_for(lambda: not self.relay.REG["127.0.0.10"].alive)

    def test_07_detach_releases_slot_during_provider_wait_and_fences_cleanup(self):
        entered = threading.Event(); release = threading.Event()
        connection, _, _ = self.connect("chat", "127.0.0.11")
        old = self.relay.REG["127.0.0.11"]
        def slow_models():
            entered.set(); release.wait(5)
        old.models = slow_models
        connection.sendall(b"F:\n")
        self.assertTrue(entered.wait(2))
        with self.lock:
            handler = self.active[("chat", "127.0.0.11")]
        original = handler.wfile
        class BrokenWriter:
            closed = False
            def write(self, data): raise BrokenPipeError("recording provider-wait disconnect")
            def flush(self): pass
            def close(self): original.close()
        handler.wfile = BrokenWriter()
        old.send("S:test")
        self.assertFalse(old.alive)
        again, _, greeting = self.connect("chat", "127.0.0.11")
        new = self.relay.REG["127.0.0.11"]
        self.assertIsNot(new, old)
        self.assertEqual(greeting, b"S:connected\n")
        release.set()
        self.wait_for(lambda: not old.handler_active)
        self.assertTrue(new.alive)
        self.assertIs(self.relay.CURRENT["session"], new)
        again.shutdown(socket.SHUT_RDWR)
        self.wait_for(lambda: not new.alive)

    def test_08_disconnect_abort_deadline_reaches_both_recording_backends(self):
        session = self.relay.Session("abort", lambda _: None)
        session.sid = "ses_fixture"; session.model = "recording/native"; session.inflight = True
        with patch.object(self.relay, "api") as api:
            session.abort("recording disconnect", timeout=5)
            api.assert_called_once_with("POST", "/session/ses_fixture/abort", {}, timeout=5)
        self.assertFalse(session.inflight)
        session.model = "codex/gpt-6.1-sol"; session.inflight = True
        session.codex_thread = "recording-thread"; session.codex_turn = "recording-turn"
        with patch.object(self.relay.CODEX, "call") as call:
            session.abort("recording disconnect", timeout=5)
            call.assert_called_once_with("turn/interrupt", {"threadId": "recording-thread", "turnId": "recording-turn"}, timeout=5)
        self.assertFalse(session.inflight)


if __name__ == "__main__":
    unittest.main(verbosity=2)
