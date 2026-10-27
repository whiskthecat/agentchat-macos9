#!/usr/bin/env python3
"""P:-free native import timing and fork verification; VMTEST unless opted in.

Run once per relay configuration: AGENT_MAC_BINARY=0 is the legacy baseline;
unset it for raw-fork-v1. Creates and deletes only a new unique HFS folder.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
import uuid
import zlib

from mac_mcp import invoke


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-port", type=int, default=8868)
    parser.add_argument("--volume", default="m")
    parser.add_argument("--label", default="binary")
    parser.add_argument("--output", required=True)
    parser.add_argument("--host-path", help="Existing source, otherwise synthesize 16 MiB")
    parser.add_argument("--application", help="Optional small application MacBinary to import and hash both forks")
    parser.add_argument("--allow-production", action="store_true", help="Explicit owner opt-in for the physical Mac")
    args = parser.parse_args()
    def call(name, **params):
        result = invoke(args.admin_port, "imac_" + name, params)
        if result.get("isError"): raise RuntimeError(result["content"][0]["text"])
        return result["structuredContent"]
    state = call("get_state")
    if not args.allow_production and not (state["test_build"] and state["relay_test_mode"]):
        raise RuntimeError("Native benchmark requires VMTEST at both ends, or explicit --allow-production")
    if state["busy"] or state["draft_bytes"]: raise RuntimeError("Use an idle client with an empty draft")
    folder = args.volume.rstrip(":") + ":ACXfer" + uuid.uuid4().hex[:8]
    owned, report = [], {"label": args.label, "state": state, "folder": folder, "imports": []}
    output = Path(args.output); output.parent.mkdir(parents=True, exist_ok=True)
    try:
        call("make_directory", path=folder); owned.append(folder)
        with tempfile.TemporaryDirectory(prefix="agentchat-native-transfer-") as temp:
            source = Path(args.host_path).resolve() if args.host_path else Path(temp) / "synthetic.bin"
            if not args.host_path:
                with source.open("wb") as stream:
                    for _ in range(256): stream.write(bytes(range(256)) * 256)
            digest, crc, size = hashlib.sha256(), 0, 0
            with source.open("rb") as stream:
                while raw := stream.read(65536): digest.update(raw); crc = zlib.crc32(raw, crc); size += len(raw)
            destination = folder + ":large.bin"
            before = call("get_idle_metrics"); started = time.monotonic()
            result = call("import_file", host_path=str(source), destination=destination, expected_sha256=digest.hexdigest())
            seconds = time.monotonic() - started; owned.append(destination)
            after = call("get_idle_metrics"); info = call("file_info", path=destination)
            assert result["written"] == size and result["sha256"] == digest.hexdigest() and result["crc32"] == "%08x" % crc
            assert info["data_bytes"] == size
            report["imports"].append(dict(result, seconds=seconds, mib_per_second=size / 1048576 / seconds,
                metrics_before=before, metrics_after=after, proof="native exact length + IEEE CRC-32; host SHA-256 before/after"))
            print(json.dumps(report["imports"][-1]), flush=True)
            if args.application:
                archive_folder = folder + ":Application"
                started = time.monotonic()
                imported = call("import_archive", host_path=str(Path(args.application).resolve()), destination=archive_folder)
                imported["import_seconds"] = time.monotonic() - started
                owned.append(archive_folder)
                for entry in imported["files"]:
                    owned.append(entry["path"])
                    for fork in ("data", "resource"):
                        h, offset = hashlib.sha256(), 0
                        while True:
                            import base64
                            part = call("read_file", path=entry["path"], fork=fork, encoding="base64", offset=offset, limit=65536)
                            h.update(base64.b64decode(part["content"], validate=True))
                            if part["next_offset"] is None: break
                            offset = part["next_offset"]
                        assert h.hexdigest() == entry[fork + "_sha256"]
                    entry["verified_info"] = call("file_info", path=entry["path"])
                    assert entry["verified_info"]["type"] == entry["type"]
                    assert entry["verified_info"]["creator"] == entry["creator"]
                report["application"] = imported
    finally:
        remaining = []
        for path in reversed(owned):
            try: call("delete", path=path)
            except Exception: remaining.append(path)
        report["cleanup_remaining"] = remaining
        output.write_text(json.dumps(report, indent=2) + "\n")
    if report["cleanup_remaining"]: raise RuntimeError("Benchmark cleanup incomplete; inspect report")


if __name__ == "__main__": main()
