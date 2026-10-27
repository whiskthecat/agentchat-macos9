"""Real local archive extraction and bounded native-import fixtures; no inference."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

from mac_archive import analyze_entries, import_archive, inspect_archive, plan_archive, select_entries
from remote_mac import MacTools


def sidecar(resource, codes=b"APPLTEST", flags=0x2100):
    info = codes + struct.pack(">H", flags) + b"\0" * 22
    return (struct.pack(">II16sH", 0x51607, 0x20000, b"\0" * 16, 2) +
            struct.pack(">III", 9, 50, len(info)) + struct.pack(">III", 2, 82, len(resource)) + info + resource)


class RecordingMac:
    def __init__(self, fail=None): self.calls = []; self.fail = fail
    def rpc(self, op, **args):
        self.calls.append((op, args))
        if op == self.fail: raise RuntimeError("fixture " + op + " failure")
        return {}
    def write(self, path, data, **args):
        self.calls.append(("write", {"path": path, "data": data, **args}))
        if self.fail == "resource" and args.get("fork") == "resource": raise RuntimeError("fixture fork failure")
        return {}
    def call(self, op, args): self.calls.append((op, args)); return {}


class ArchiveWorkflowTests(unittest.TestCase):
    def test_real_zip_inspection_selection_and_exact_literal_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "collection.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Game/App[1]", b"application fixture")
                z.writestr("Game/App1", b"do not select by wildcard")
                for i in range(130): z.writestr("Extras/File %03d" % i, b"extra")
            report = inspect_archive({"host_path": str(archive), "limit": 3})
            self.assertFalse(report["importable"])
            self.assertEqual(report["next_start"], 3)
            self.assertEqual(len(report["entries"]), 3)
            self.assertIn("paths", report["issues"][0])
            self.assertEqual(report["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            output = Path(tmp) / "out"; output.mkdir()
            folders, files = plan_archive(archive, output, ["Game/App[1]"])
            self.assertEqual(folders, [PurePosixPath("Game")])
            self.assertEqual([(f["relative"], f["data"]) for f in files], [("Game/App[1]", b"application fixture")])
            self.assertFalse((output / "Game/App1").exists())
            selected = inspect_archive({"host_path": str(archive), "paths": ["Game"], "limit": 3})
            self.assertTrue(selected["importable"])
            self.assertEqual(selected["logical_entries"], 3)

    def test_real_rsrc_filename_collision_preserves_both_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "project.zip"; output = Path(tmp) / "out"; output.mkdir()
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("Sources/Project", b"project data")
                z.writestr("Sources/Project.rsrc", b"legitimate separate file")
            _, files = plan_archive(archive, output)
            self.assertEqual({f["relative"]: f["data"] for f in files},
                             {"Sources/Project": b"project data", "Sources/Project.rsrc": b"legitimate separate file"})

    def test_dual_forks_count_once_and_finder_metadata_is_preserved(self):
        entries = []
        for i in range(70):
            for fork in (False, True):
                entries.append({"XADFileName": "Folder/File %02d" % i, "XADFileSize": 4,
                                "XADIsResourceFork": fork, "XADIndex": len(entries)})
        listing = {"lsarContents": entries, "lsarFormatName": "fixture"}
        def extract(command, **kwargs):
            output = Path(command[command.index("-o") + 1])
            (output / "Folder").mkdir()
            for i in range(70):
                (output / "Folder" / ("File %02d" % i)).write_bytes(b"DATA")
                (output / "Folder" / ("File %02d.rsrc" % i)).write_bytes(sidecar(b"RSRC"))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as tmp, patch("mac_archive.archive_listing", return_value=listing), patch("mac_archive.subprocess.run", side_effect=extract):
            _, files = plan_archive(Path(tmp) / "archive", Path(tmp))
            self.assertEqual(len(files), 70)
            self.assertEqual(files[0]["data"], b"DATA"); self.assertEqual(files[0]["resource"], b"RSRC")
            self.assertEqual((files[0]["type"], files[0]["creator"], files[0]["flags"]), ("APPL", "TEST", 0x2100))

    def test_native_import_keeps_resource_only_app_and_rolls_back_owned_files(self):
        entry = {"relative": "Game/App", "data": b"", "resource": b"CODE fixture", "type": "APPL", "creator": "TEST", "flags": 0x2100}
        with tempfile.TemporaryDirectory() as tmp, patch("mac_archive.plan_archive", return_value=([PurePosixPath("Game")], [entry])):
            archive = Path(tmp) / "source"; archive.write_bytes(b"archive")
            args = {"host_path": str(archive), "destination": "Disk:New game"}
            mac = RecordingMac(); report = import_archive(mac, args)
            self.assertEqual(report["files"][0]["data_bytes"], 0)
            self.assertEqual(report["files"][0]["resource_sha256"], hashlib.sha256(entry["resource"]).hexdigest())
            writes = [c[1] for c in mac.calls if c[0] == "write"]
            self.assertTrue(writes[0]["create_only"]); self.assertEqual(writes[1]["fork"], "resource")
            mac = RecordingMac("resource")
            with self.assertRaisesRegex(RuntimeError, "fork failure"): import_archive(mac, args)
            self.assertEqual([c[1]["path"] for c in mac.calls if c[0] == "delete"],
                             ["Disk:New game:Game:App", "Disk:New game:Game", "Disk:New game"])
            mac = RecordingMac("mkdir")
            with self.assertRaises(RuntimeError): import_archive(mac, args)
            self.assertFalse(any(c[0] == "delete" for c in mac.calls))

    def test_invalid_selection_types_never_reach_native_tools(self):
        mac = MacTools(None, None)
        for paths in ([], "Game", [123], [None], [""], ["x"] * 33):
            with self.assertRaises(ValueError): mac.call("imac_import_archive", {"host_path": "/tmp/source", "destination": "Disk:New", "paths": paths})

    def test_invalid_names_links_case_collisions_and_duplicate_forks(self):
        for entries in ([{"XADFileName": "../escape"}],
                        [{"XADFileName": "Link", "XADIsLink": True}],
                        [{"XADFileName": "File"}, {"XADFileName": "file"}],
                        [{"XADFileName": "File"}, {"XADFileName": "File"}]):
            with self.assertRaises(ValueError): analyze_entries(select_entries(entries))
        entries = [{"XADFileName": "Game/App"}, {"XADFileName": "../unselected"}]
        folders, files, _, _ = analyze_entries(select_entries(entries, ["Game"]))
        self.assertEqual(len(files), 1)
        with self.assertRaisesRegex(ValueError, "inspect_archive"): select_entries(entries, ["missing"])

    def test_checksum_empty_download_and_path_length_fail_before_guest_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "empty"; archive.write_bytes(b"")
            mac = RecordingMac()
            args = {"host_path": str(archive), "destination": "Disk:New"}
            with self.assertRaisesRegex(ValueError, "empty"): import_archive(mac, args)
            archive.write_bytes(b"archive")
            with self.assertRaisesRegex(ValueError, "checksum"): import_archive(mac, {**args, "expected_sha256": "incorrect"})
            self.assertEqual(mac.calls, [])
            deep = "/".join(["x" * 30] * 9)
            entry = {"relative": deep, "data": b"data", "resource": b"", "type": "TEXT", "creator": "TEST", "flags": 0}
            with patch("mac_archive.plan_archive", return_value=([], [entry])):
                with self.assertRaisesRegex(ValueError, "255 bytes"): import_archive(mac, args)
            self.assertEqual(mac.calls, [])

    def test_unexpected_extracted_files_and_fork_mismatch_are_rejected(self):
        listing = {"lsarContents": [{"XADFileName": "File", "XADFileSize": 4}]}
        for unexpected in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                def extract(command, **kwargs):
                    output = Path(command[command.index("-o") + 1])
                    (output / ("Unexpected" if unexpected else "File")).write_bytes(b"bad")
                    return SimpleNamespace(returncode=0)
                with patch("mac_archive.archive_listing", return_value=listing), patch("mac_archive.subprocess.run", side_effect=extract):
                    with self.assertRaises(ValueError): plan_archive(Path(tmp) / "archive", Path(tmp))


if __name__ == "__main__": unittest.main()
