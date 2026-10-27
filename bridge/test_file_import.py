#!/usr/bin/env python3
"""Bounded host-to-Mac imports with a recording native RPC fake; no inference."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import zlib

from remote_mac import CHUNK, HOST_READ_CHUNK, MAX_FILE, MAX_IMPORT_FILE, MacError, MacTools
from mac_mcp import invoke


class RecordingMac:
    """Only the staging RPCs exist here; a prompt cannot reach a backend."""
    def __init__(self):
        self.calls = []
        self.files = {}
        self.stage = None
        self.fail_chunk = False
        self.bad_ack = False
        self.corrupt_chunk = False
        self.on_chunk = None
        self.cleanup_warning = False

    def request(self, session, connection, op, **args):
        self.calls.append((op, args))
        if not session.alive or session.connection_id != connection:
            raise MacError("Macintosh disconnected; stale connection was rejected")
        if op == "write_stream_begin":
            if self.stage: raise MacError("Transaction busy")
            if args["count"] > MAX_IMPORT_FILE: raise MacError("Invalid native size")
            if args["create_only"] and args["path"] in self.files:
                raise MacError("Name already exists (Mac OS error -48)")
            self.stage = dict(args, token="owned-stage", received=0, crc=0, digest=hashlib.sha256())
            return {"token": "owned-stage"}
        if not self.stage or args["path"] != self.stage["token"]:
            raise MacError("Invalid staging token")
        if op == "write_chunk":
            if self.fail_chunk: raise MacError("Disk full")
            raw = args["data"]
            if args["offset"] != self.stage["received"] or len(raw) > CHUNK:
                raise MacError("Invalid chunk")
            if self.corrupt_chunk and raw: raw = bytes([raw[0] ^ 1]) + raw[1:]
            self.stage["received"] += len(raw)
            self.stage["crc"] = zlib.crc32(raw, self.stage["crc"])
            self.stage["digest"].update(raw)
            if self.on_chunk:
                callback, self.on_chunk = self.on_chunk, None
                callback()
            return {"received": self.stage["received"] + int(self.bad_ack)}
        if op == "write_cancel":
            self.stage = None
            return {"cancelled": True}
        if op == "write_commit":
            stage = self.stage
            crc = "%08x" % stage["crc"]
            if stage["received"] != stage["count"] or crc != stage["expected_crc"]:
                raise MacError("Native staged size or CRC did not match")
            previous = self.files.get(stage["path"], {"data": None, "resource": None, "finder": ("TEXT", "ttxt")})
            self.files[stage["path"]] = dict(previous, **{stage["fork"]: (stage["received"], stage["digest"].hexdigest())})
            self.stage = None
            return {"committed": True, "written": stage["received"], "crc32": crc,
                    "cleanup_warning": self.cleanup_warning}
        raise AssertionError("Unexpected operation: " + op)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "source.bin"
        self.source.write_bytes(bytes(range(256)) * 29)
        self.broker = RecordingMac()
        self.session = SimpleNamespace(alive=True, connection_id="initial")
        self.mac = MacTools(self.broker, self.session)
        self.destination = "Disk:Games:G1.dat"

    def call(self, **overrides):
        arguments = {"host_path": str(self.source), "destination": self.destination}
        arguments.update(overrides)
        return self.mac.call("imac_import_file", arguments)["structuredContent"]

    def test_streamed_source_hash_and_native_crc_without_model_binary(self):
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        result = self.call(expected_sha256=expected.upper())
        self.assertEqual(result["sha256"], expected)
        self.assertEqual(result["written"], self.source.stat().st_size)
        self.assertEqual(result["crc32"], "%08x" % zlib.crc32(self.source.read_bytes()))
        self.assertTrue(self.broker.calls[0][1]["create_only"])
        chunks = [args for op, args in self.broker.calls if op == "write_chunk"]
        self.assertEqual([part["offset"] for part in chunks], [0, CHUNK, CHUNK*2])
        self.assertTrue(all(len(part["data"]) <= CHUNK for part in chunks))
        self.assertLess(len(json.dumps(result)), 512)
        self.assertNotIn("content", result)

    def test_file_above_ordinary_write_cap_uses_bounded_memory(self):
        with self.source.open("wb") as source: source.truncate(MAX_FILE + 177)
        reads = []
        fdopen = os.fdopen

        class AuditedSource:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): self.stream.__enter__(); return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def fileno(self): return self.stream.fileno()
            def seek(self, *args): return self.stream.seek(*args)
            def read(self, size):
                reads.append(size)
                if not 0 < size <= HOST_READ_CHUNK: raise AssertionError("Unbounded source read")
                return self.stream.read(size)

        with mock.patch("remote_mac.os.fdopen", side_effect=lambda fd, mode: AuditedSource(fdopen(fd, mode))), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Whole-file read")):
            result = self.call()
        self.assertEqual(result["written"], MAX_FILE + 177)
        self.assertGreater(len(reads), 5000)
        self.assertLessEqual(max(reads), HOST_READ_CHUNK)
        self.assertEqual(self.broker.calls[0][0], "write_stream_begin")
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            self.mac.write(self.destination, bytes(MAX_FILE + 1))

    def test_empty_file_is_verified_and_committed(self):
        self.source.write_bytes(b"")
        result = self.call()
        self.assertEqual(result["written"], 0)
        self.assertEqual(result["crc32"], "00000000")
        self.assertEqual([op for op, _ in self.broker.calls], ["write_stream_begin", "write_commit"])

    def test_hash_mismatch_and_invalid_checksum_never_touch_guest(self):
        for checksum in ("0" * 64, "x" * 64, "short"):
            with self.subTest(checksum=checksum):
                with self.assertRaises(ValueError): self.call(expected_sha256=checksum)
        self.assertEqual(self.broker.calls, [])

    def test_too_large_source_is_rejected_before_guest_mutation(self):
        with self.source.open("wb") as source: source.truncate(MAX_IMPORT_FILE + 1)
        with self.assertRaisesRegex(ValueError, "64 MiB"): self.call()
        self.assertEqual(self.broker.calls, [])

    def test_relative_path_directory_and_fifo_are_rejected_without_blocking(self):
        with self.assertRaises(ValueError): self.call(host_path="relative.bin")
        with self.assertRaises((ValueError, OSError)): self.call(host_path=self.temp.name)
        fifo = Path(self.temp.name) / "fifo"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(ValueError, "regular"): self.call(host_path=str(fifo))
        self.assertEqual(self.broker.calls, [])

    def test_final_symlink_is_rejected_without_guest_mutation(self):
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.source)
        with self.assertRaises(OSError): self.call(host_path=str(link))
        self.assertEqual(self.broker.calls, [])

    def test_create_only_defaults_to_safe_absence_requirement(self):
        original = {"data": "untouched", "resource": "resource", "finder": ("BINA", "ORC2")}
        self.broker.files[self.destination] = original.copy()
        with self.assertRaisesRegex(MacError, "exists"): self.call()
        self.assertIsNone(self.broker.stage)
        self.assertEqual(self.broker.files[self.destination], original)

    def test_explicit_replacement_preserves_other_fork_and_finder_info(self):
        self.broker.files[self.destination] = {"data": "keep", "resource": "old", "finder": ("APPL", "TEST")}
        self.call(create_only=False, fork="resource")
        self.assertEqual(self.broker.files[self.destination]["data"], "keep")
        self.assertEqual(self.broker.files[self.destination]["finder"], ("APPL", "TEST"))
        self.assertEqual(self.broker.files[self.destination]["resource"][0], self.source.stat().st_size)

    def test_owned_stage_cancelled_after_chunk_error_or_bad_ack(self):
        for attribute in ("fail_chunk", "bad_ack", "corrupt_chunk"):
            with self.subTest(attribute=attribute):
                self.broker = RecordingMac(); self.mac = MacTools(self.broker, self.session)
                original = {"data": "old", "resource": "keep", "finder": ("BINA", "ORC2")}
                self.broker.files[self.destination] = original.copy()
                setattr(self.broker, attribute, True)
                with self.assertRaises(MacError): self.call(create_only=False)
                self.assertEqual(self.broker.calls[-1][0], "write_cancel")
                self.assertIsNone(self.broker.stage)
                self.assertEqual(self.broker.files[self.destination], original)
                self.assertNotIn("delete", [op for op, _ in self.broker.calls])

    def test_source_append_truncate_and_same_size_edit_cancel_before_commit(self):
        for change in ("append", "truncate", "overwrite"):
            with self.subTest(change=change):
                self.source.write_bytes(bytes(range(256)) * 29)
                self.broker = RecordingMac(); self.mac = MacTools(self.broker, self.session)
                def mutate():
                    with self.source.open("r+b", buffering=0) as source:
                        if change == "append": source.seek(0, 2); source.write(b"added")
                        elif change == "truncate": source.truncate(0)
                        else: source.seek(CHUNK+10); source.write(b"edited")
                self.broker.on_chunk = mutate
                with self.assertRaisesRegex(MacError, "Host source"): self.call()
                self.assertEqual(self.broker.calls[-1][0], "write_cancel")
                self.assertNotIn("write_commit", [op for op, _ in self.broker.calls])
                self.assertIsNone(self.broker.stage)
                self.assertNotIn(self.destination, self.broker.files)

    def test_host_path_replacement_does_not_replace_open_source_descriptor(self):
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        def replace_path():
            replacement = self.source.with_name("replacement")
            replacement.write_bytes(b"other bytes")
            replacement.replace(self.source)
        self.broker.on_chunk = replace_path
        # Removing a name changes ctime of the open file on Linux. That safely
        # cancels the import instead of silently switching to the replacement.
        with self.assertRaisesRegex(MacError, "Host source"): self.call(expected_sha256=expected)
        self.assertIsNone(self.broker.stage)
        self.assertNotIn(self.destination, self.broker.files)

    def test_new_connection_never_continues_old_import(self):
        self.broker.on_chunk = lambda: setattr(self.session, "connection_id", "replacement")
        with self.assertRaisesRegex(MacError, "stale connection"): self.call()
        self.assertNotIn("write_commit", [op for op, _ in self.broker.calls])
        self.assertNotIn(self.destination, self.broker.files)

    def test_committed_backup_cleanup_warning_is_reported_as_success(self):
        self.broker.cleanup_warning = True
        self.assertTrue(self.call()["cleanup_warning"])
        self.assertIn(self.destination, self.broker.files)

    def test_import_timeout_is_bounded_and_other_tools_keep_existing_timeout(self):
        for name, seconds in (("imac_import_file", 600), ("imac_read_file", 120), ("imac_import_archive", 600)):
            with self.subTest(name=name):
                connection = mock.MagicMock()
                connection.__enter__.return_value = connection
                connection.makefile.return_value.readline.side_effect = [b"agentd admin console\n", b"{}\n"]
                with mock.patch("mac_mcp.socket.create_connection", return_value=connection):
                    self.assertEqual(invoke(1, name, {}), {})
                connection.settimeout.assert_called_once_with(seconds)


if __name__ == "__main__": unittest.main()
