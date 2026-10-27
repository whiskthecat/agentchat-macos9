#!/usr/bin/env python3
"""Binary import framing/negotiation, real relay writer, recording sinks only.

Never sends P: or replies to a tool question. All listeners are loopback and
ephemeral; all native operations are recording fakes.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from mac_binary import TRANSPORT, write_body
from remote_mac import CHUNK, MacBroker, MacError, MacTools
from test_file_import import RecordingMac

TOKEN = "AC~123456781234"
NONCE = "0123456789abcdef0123456789abcdef"


class WireMac:
    """Run the real broker/importer against a fake native staging filesystem."""
    def __init__(self, old_client=False, old_relay=False):
        self.alive, self.connection_id = True, "original"
        self.broker, self.native = MacBroker(), RecordingMac()
        self.lines, self.frames, self.abort_count = [], [], 0
        self.corrupt = False
        self.on_body = None
        self.mac = MacTools(self.broker, self)
        if old_relay: self.send_binary = None
        self.old_client = old_client

    def import_bytes(self, path, data, fork="data", create_only=False):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"; source.write_bytes(data)
            return self.mac.import_file({"host_path": str(source), "destination": path, "fork": fork, "create_only": create_only})

    def reply(self, nonce, result):
        self.broker.reply(self, self.connection_id, "E:" + nonce + "\t0\t" +
                          base64.b64encode(json.dumps(result).encode()).decode())

    def send(self, line):
        assert line.startswith("C:RPC\t")
        self.lines.append(line)
        nonce, op, path, offset, count, data, fork, create, crc = line[6:].split("\t")
        params = dict(path=base64.b64decode(path).decode(), offset=int(offset), count=int(count),
                      data=base64.b64decode(data), fork=fork, create_only=create == "1", expected_crc=crc)
        if op != "write_stream_begin": params["path"] = "owned-stage"
        try:
            result = self.native.request(self, self.connection_id, op, **params)
        except MacError:
            result = {"mac_error": -48 if op == "write_stream_begin" else -34}
        if op == "write_stream_begin" and "token" in result:
            result["token"] = TOKEN
            if not self.old_client and params["data"] == TRANSPORT.encode(): result["transport"] = TRANSPORT
        self.reply(nonce, result)

    def send_binary(self, connection, token, nonce, count, chunks):
        assert connection == self.connection_id
        output = io.BytesIO()
        write_body(output, token, nonce, count, chunks)
        header, raw = output.getvalue().split(b"\n", 1)
        self.frames.append((header, raw))
        if self.corrupt: raw = bytes([raw[0] ^ 1]) + raw[1:]
        stage = self.native.stage
        stage["received"] = len(raw); stage["crc"] = zlib.crc32(raw); stage["digest"].update(raw)
        if self.on_body: self.on_body()
        self.reply(nonce, {"received": len(raw)})

    def abort_binary(self, connection):
        assert connection == self.connection_id
        self.abort_count += 1; self.alive = False; self.native.stage = None


class BinaryImportTests(unittest.TestCase):
    def test_exact_frame_and_bounds(self):
        raw = bytes(range(256)) * 256 + b"\nC:QUIT\nP:not a prompt\n\0\r"
        out = io.BytesIO(); write_body(out, TOKEN, NONCE, len(raw), [raw[:65536], raw[65536:]])
        self.assertEqual(out.getvalue(), f"B:{TRANSPORT}\t{TOKEN}\t{len(raw)}\t{NONCE}\n".encode() + raw)
        for count, chunks in ((4, [b"abc"]), (2, [b"abc"]), (1, [b""]), (65537, [b"x" * 65537])):
            with self.subTest(count=count), self.assertRaises(ValueError):
                write_body(io.BytesIO(), TOKEN, NONCE, count, chunks)
        with self.assertRaises(OSError):
            write_body(SimpleNamespace(write=lambda raw: len(raw) - 1), TOKEN, NONCE, 1, [b"x"])

    def test_old_client_and_old_relay_keep_3072_and_new_pair_uses_raw(self):
        raw = bytes(range(256)) * 600
        for old_client, old_relay in ((True, False), (False, True), (False, False)):
            with self.subTest(old_client=old_client, old_relay=old_relay):
                session = WireMac(old_client, old_relay)
                result = session.import_bytes("Disk:Fixture", raw, create_only=True)
                binary = not old_client and not old_relay
                self.assertEqual(result["transport"], TRANSPORT if binary else "base64-3072")
                self.assertEqual(len(session.frames), int(binary))
                chunks = [p for op, p in session.native.calls if op == "write_chunk"]
                self.assertEqual(len(chunks), 0 if binary else (len(raw) + CHUNK - 1) // CHUNK)
                self.assertTrue(all(len(p["data"]) <= 3072 for p in chunks))
                self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(result["crc32"], "%08x" % zlib.crc32(raw))

    def test_create_only_replacement_both_forks_metadata_crc_failure(self):
        session = WireMac(); original = {"data": "keep", "resource": "old", "finder": ("APPL", "TEST")}
        session.native.files["Disk:Fixture"] = original.copy()
        with self.assertRaises(MacError): session.import_bytes("Disk:Fixture", b"new", create_only=True)
        self.assertFalse(session.frames)
        session.import_bytes("Disk:Fixture", b"new", fork="resource")
        self.assertEqual(session.native.files["Disk:Fixture"]["data"], "keep")
        self.assertEqual(session.native.files["Disk:Fixture"]["finder"], ("APPL", "TEST"))
        saved = session.native.files["Disk:Fixture"].copy(); session.corrupt = True
        with self.assertRaises(MacError): session.import_bytes("Disk:Fixture", b"corrupt")
        self.assertEqual(session.native.files["Disk:Fixture"], saved)
        self.assertIsNone(session.native.stage)

    def test_binary_source_mutation_cancels_before_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "source"; path.write_bytes(b"stable" * 20000)
            session = WireMac(); session.on_body = lambda: path.write_bytes(b"changed")
            with self.assertRaisesRegex(MacError, "Host source changed"):
                session.mac.import_file({"host_path": str(path), "destination": "Disk:Fixture"})
            self.assertIsNone(session.native.stage); self.assertFalse(session.native.files)
            self.assertNotIn("write_commit", [op for op, _ in session.native.calls])

    def test_missing_final_ack_closes_connection_and_never_replays(self):
        broker = MacBroker(); aborted = []
        session = SimpleNamespace(alive=True, connection_id="original", send_binary=lambda *args: None,
                                  abort_binary=aborted.append)
        with mock.patch("remote_mac.queue.Queue") as queues:
            queues.return_value.get.side_effect = queue.Empty
            with self.assertRaisesRegex(MacError, "timed out"):
                broker.binary(session, "original", TOKEN, 1, iter([b"x"]))
        self.assertEqual(aborted, ["original"]); self.assertFalse(broker.pending)
        session.connection_id = "replacement"
        with self.assertRaises(MacError): broker.binary(session, "original", TOKEN, 1, iter([b"x"]))


class RelayWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        with mock.patch.dict(os.environ, AGENT_TEST_MODE="1", AGENT_STATE_FILE=cls.temp.name + "/state",
                             OPENCODE_ENABLED="0", CODEX_ENABLED="0", IMAC_TOOLS_ENABLED="0"):
            spec = importlib.util.spec_from_file_location("binary_relay", Path(__file__).with_name("agentd.py"))
            cls.relay = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.relay)
        cls.relay.log = lambda *args: None
        cls.server = cls.relay.Server(("127.0.0.1", 0), cls.relay.ChatHandler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.relay.CODEX.close(); cls.temp.cleanup()

    def connect(self):
        sock = socket.create_connection(self.server.server_address, timeout=3)
        stream = sock.makefile("rb")
        self.assertEqual(stream.readline(), b"H:alive\n"); self.assertEqual(stream.readline(), b"S:connected\n")
        session = self.relay.REG["127.0.0.1"]
        self.addCleanup(stream.close); self.addCleanup(sock.close)
        return sock, stream, session

    def test_01_heartbeat_and_reply_wait_behind_exact_body(self):
        sock, stream, session = self.connect()
        entered, release, queued = threading.Event(), threading.Event(), threading.Event()
        errors = []; body = bytes(range(256)) * 1024
        def chunks():
            entered.set(); self.assertTrue(release.wait(3))
            yield body[:65536]; yield body[65536:131072]; yield body[131072:196608]; yield body[196608:]
        def transfer():
            try: self.relay.MAC.binary(session, session.connection_id, TOKEN, len(body), chunks())
            except Exception as exc: errors.append(exc)
        sender = threading.Thread(target=transfer); sender.start(); self.assertTrue(entered.wait(3))
        def traffic():
            queued.set(); session.send("H:alive"); session.send("T:recording reply")
        other = threading.Thread(target=traffic); other.start(); self.assertTrue(queued.wait(3)); release.set()
        header = stream.readline().decode().strip().split("\t")
        self.assertEqual(header[:3], ["B:" + TRANSPORT, TOKEN, str(len(body))])
        self.assertEqual(stream.read(len(body)), body)
        self.assertEqual(stream.readline(), b"H:alive\n"); self.assertEqual(stream.readline(), b"T:recording reply\n")
        sock.sendall(("E:" + header[3] + "\t0\t" + base64.b64encode(json.dumps({"received": len(body)}).encode()).decode() + "\n").encode())
        sender.join(3); other.join(3); self.assertFalse(sender.is_alive()); self.assertFalse(errors)
        sock.shutdown(socket.SHUT_RDWR)
        for _ in range(100):
            if not session.alive: break
            time.sleep(.01)

    def test_02_truncated_source_closes_before_queued_control_line(self):
        _, stream, session = self.connect()
        def short():
            yield b"abc"
            raise OSError("recording source failure")
        with self.assertRaises(OSError): session.send_binary(session.connection_id, TOKEN, NONCE, 10, short())
        session.send("C:QUIT")
        self.assertTrue(stream.readline().startswith(b"B:")); self.assertEqual(stream.read(), b"abc")
        self.assertFalse(session.alive)

    def test_03_peer_disconnect_mid_body_wakes_broker_without_replay(self):
        sock, stream, session = self.connect(); entered, release = threading.Event(), threading.Event(); errors = []
        with self.assertRaisesRegex(RuntimeError, "connection changed"):
            session.send_binary("stale", TOKEN, NONCE, 1, [b"x"])
        session.abort_binary("stale")
        self.assertTrue(session.alive)
        def chunks():
            entered.set(); release.wait(3)
            yield b"x" * 65536
        def transfer():
            try: self.relay.MAC.binary(session, session.connection_id, TOKEN, 65536, chunks())
            except Exception as exc: errors.append(exc)
        sender = threading.Thread(target=transfer); sender.start(); self.assertTrue(entered.wait(3))
        self.assertTrue(stream.readline().startswith(b"B:")); sock.shutdown(socket.SHUT_RDWR); release.set()
        sender.join(3); self.assertFalse(sender.is_alive()); self.assertEqual(len(errors), 1)
        self.assertFalse(self.relay.MAC.pending)


if __name__ == "__main__": unittest.main()
