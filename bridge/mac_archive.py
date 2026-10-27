"""Bounded host extraction; Macintosh writes always use native client RPC.

unar's visible resource files are AppleDouble containers, not raw resource forks.
We unpack their fork/Finder-info entries before sending bytes to the File Manager.
"""
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

ARCHIVE_LIMIT = 64 * 1024 * 1024
EXPANDED_LIMIT = 64 * 1024 * 1024
ENTRY_LIMIT = 128  # Logical HFS files/folders, including parents.
RECORD_LIMIT = 4096
LISTING_LIMIT = 2 * 1024 * 1024


def appledouble_ranges(stream, size):
    """Read only the bounded directory, never a potentially 64 MiB fork."""
    header = stream.read(26)
    if len(header) != 26: raise ValueError("Truncated AppleDouble header")
    magic, version = struct.unpack_from(">II", header)
    if magic != 0x00051607 or version not in (0x10000, 0x20000): raise ValueError("Invalid AppleDouble file")
    count = struct.unpack_from(">H", header, 24)[0]
    if count > 32: raise ValueError("Invalid AppleDouble entries")
    directory = stream.read(count * 12)
    if len(directory) != count * 12: raise ValueError("Invalid AppleDouble entries")
    entries = {}; ranges = []
    for i in range(count):
        key, offset, length = struct.unpack_from(">III", directory, i * 12)
        if key in entries or offset < 26 + 12 * count or offset + length > size: raise ValueError("Invalid AppleDouble range")
        if any(offset < end and offset + length > start for start, end in ranges): raise ValueError("Overlapping AppleDouble ranges")
        ranges.append((offset, offset + length)); entries[key] = (offset, length)
    return entries


def appledouble(data):
    import io
    entries = appledouble_ranges(io.BytesIO(data), len(data))
    return {key: data[offset:offset + size] for key, (offset, size) in entries.items()}


def extract_resource(sidecar, resource):
    """Materialize one raw resource fork on disk, with bounded memory."""
    info = None
    with sidecar.open("rb") as source:
        entries = appledouble_ranges(source, os.fstat(source.fileno()).st_size)
        if 9 in entries:
            offset, size = entries[9]; source.seek(offset); info = source.read(min(size, 32))
        offset, remaining = entries.get(2, (0, 0)); source.seek(offset)
        with resource.open("wb") as target:
            while remaining:
                raw = source.read(min(65536, remaining))
                if not raw: raise ValueError("Truncated AppleDouble fork")
                target.write(raw); remaining -= len(raw)
    return info


def safe_name(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or "\\" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError("Unsafe archive path: " + repr(name))
    for p in path.parts:
        if ":" in p or "\0" in p or len(p.encode("mac_roman")) > 31: raise ValueError("Name is not a Classic HFS filename: " + repr(p))
    return path


def archive_listing(archive):
    with tempfile.TemporaryFile() as listing:
        try:
            subprocess.run(["lsar", "-j", str(archive)], stdout=listing, stderr=subprocess.PIPE, timeout=20, check=True)
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.decode(errors="replace").strip()[:400]
            raise ValueError("Archive could not be recognized: " + detail) from exc
        if listing.tell() > LISTING_LIMIT: raise ValueError("Archive listing exceeds 2 MiB")
        listing.seek(0); result = json.load(listing)
    entries = result["lsarContents"]
    if not entries: raise ValueError("Archive contains no files")
    if len(entries) > RECORD_LIMIT: raise ValueError("Archive listing exceeds 4096 fork records; use a smaller archive")
    return result


def select_entries(entries, paths=None):
    """Use archive indexes, never filename wildcard matching."""
    if paths is not None:
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32 or any(not isinstance(p, str) for p in paths):
            raise ValueError("paths must contain 1..32 exact archive file or folder names")
        paths = [str(safe_name(p.rstrip("/"))) for p in paths]
        for path in paths:
            if not any(e["XADFileName"] == path or e["XADFileName"].startswith(path + "/") for e in entries):
                raise ValueError("Archive path not found: %r. Use imac_inspect_archive for exact names." % path)
    selected = []
    for index, entry in enumerate(entries):
        name = entry["XADFileName"]
        if paths is not None and not any(name == p or name.startswith(p + "/") for p in paths): continue
        if PurePosixPath(name).name == ".DS_Store" or "__MACOSX" in PurePosixPath(name).parts: continue
        selected.append((entry.get("XADIndex", index), entry))
    if not selected: raise ValueError("Archive selection contains no importable files")
    return selected


def analyze_entries(selected):
    total = 0; files = {}; folders = set(); indexes = []
    for index, entry in selected:
        if not isinstance(index, int) or isinstance(index, bool) or index < 0: raise ValueError("Invalid archive entry index")
        indexes.append(index)
        path = safe_name(entry["XADFileName"])
        if entry.get("XADIsLink") or entry.get("XADIsSymbolicLink") or entry.get("XADIsHardLink"):
            raise ValueError("Archive links are not supported: " + str(path))
        size = entry.get("XADFileSize", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0: raise ValueError("Invalid archive fork length")
        total += size
        folders.update(parent for parent in path.parents if str(parent) != ".")
        if entry.get("XADIsDirectory"): folders.add(path)
        else:
            record = files.setdefault(path, {})
            fork = "resource" if entry.get("XADIsResourceFork") else "data"
            if fork in record: raise ValueError("Duplicate %s fork for %s" % (fork, path))
            record[fork] = entry
    if folders.intersection(files): raise ValueError("File/folder archive collision")
    names = {}
    for path in folders | files.keys():
        key = str(path).casefold()
        if key in names and names[key] != path:
            raise ValueError("Names collide on case-insensitive HFS: %s and %s" % (names[key], path))
        names[key] = path
    return folders, files, total, indexes


def import_problems(folders, files, total):
    problems = []; count = len(folders) + len(files)
    if count > ENTRY_LIMIT:
        problems.append("Archive selection has %d logical HFS files/folders; the limit is %d. Use imac_inspect_archive and paths to select a needed subset." % (count, ENTRY_LIMIT))
    if total > EXPANDED_LIMIT:
        problems.append("Selected forks expand to %d bytes; the limit is 64 MiB. Select a smaller subset with paths." % total)
    return problems


def sidecar_path(path, mode):
    return path.with_name(path.name + ".rsrc" if mode == "visible" else "._" + path.name)


def sidecar_mode(files, folders=()):
    # Real project files ending in .rsrc must not collide with extracted forks.
    for mode in ("visible", "hidden"):
        if not any(sidecar_path(p, mode) in set(files) | set(folders) for p in files): return mode
    raise ValueError("Resource sidecar names collide in both extraction modes; use paths to select a nonconflicting subset")


def finder_info(metadata, info=None):
    type_code = metadata.get("XADFileType", 0).to_bytes(4, "big").decode("mac_roman")
    creator = metadata.get("XADFileCreator", 0).to_bytes(4, "big").decode("mac_roman")
    flags = metadata.get("XADFinderFlags", 0)
    if info and len(info) >= 10:
        type_code = info[:4].decode("mac_roman"); creator = info[4:8].decode("mac_roman"); flags = int.from_bytes(info[8:10], "big")
    return type_code, creator, flags


def plan_archive(archive, output, paths=None):
    """Validate selected logical entries before extraction or guest writes."""
    entries = archive_listing(archive)["lsarContents"]
    folders, files, total, indexes = analyze_entries(select_entries(entries, paths))
    problems = import_problems(folders, files, total)
    if problems: raise ValueError(" ".join(problems))
    mode = sidecar_mode(files, folders)
    # Indexes extract only validated records and bypass wildcard interpretation.
    try:
        subprocess.run(["unar", "-q", "-D", "-k", mode, "-o", str(output), "-i", str(archive), *map(str, indexes)],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=True)
    except subprocess.CalledProcessError as exc:
        raise ValueError("Archive extraction failed: " + exc.stderr.decode(errors="replace").strip()[:400]) from exc
    allowed_files = set(files) | {sidecar_path(p, mode) for p in files}
    expanded = 0
    for path in output.rglob("*"):
        if path.is_symlink(): raise ValueError("Extracted archive contains a link")
        relative = PurePosixPath(path.relative_to(output))
        if path.is_file():
            if relative not in allowed_files: raise ValueError("Unexpected extracted file: " + str(relative))
            expanded += path.stat().st_size
        elif relative not in folders: raise ValueError("Unexpected extracted directory: " + str(relative))
    if expanded > EXPANDED_LIMIT + ENTRY_LIMIT * 4096: raise ValueError("Extracted output exceeds limit")
    result = []
    fork_output = Path(tempfile.mkdtemp(prefix=".agentchat-forks-", dir=output))
    for path, forks in sorted(files.items(), key=lambda x: str(x[0])):
        file = output.joinpath(*path.parts); sidecar = output.joinpath(*sidecar_path(path, mode).parts)
        # unar can omit the empty data fork of a resource-only application.
        if not file.exists(): file.touch()
        resource = fork_output / ("%04d.resource" % len(result))
        info = extract_resource(sidecar, resource) if sidecar.exists() else None
        if not resource.exists(): resource.touch()
        for key, value in (("data", file), ("resource", resource)):
            expected = forks.get(key, {}).get("XADFileSize", 0)
            if value.stat().st_size != expected: raise ValueError("Extracted %s fork length mismatch: %s" % (key, path))
        metadata = forks.get("resource", forks.get("data"))
        type_code, creator, flags = finder_info(metadata, info)
        result.append({"relative": str(path), "data": file, "resource": resource, "type": type_code, "creator": creator, "flags": flags})
    return sorted(folders, key=lambda p: (len(p.parts), str(p))), result


def load_source(args, archive):
    if bool(args.get("url")) == bool(args.get("host_path")): raise ValueError("Supply exactly one url or host_path")
    original = None
    if args.get("url"):
        if urlparse(args["url"]).scheme not in ("http", "https"): raise ValueError("Archive URL must use HTTP or HTTPS")
        source = urllib.request.urlopen(args["url"], timeout=5)
        if urlparse(source.url).scheme not in ("http", "https"):
            source.close(); raise ValueError("Unsupported archive redirect")
    else:
        path = Path(args["host_path"])
        if not path.is_absolute(): raise ValueError("host_path must be an absolute regular archive file on the relay host")
        source = os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)), "rb")
    with source:
        if not args.get("url"):
            original = os.fstat(source.fileno())
            if not stat.S_ISREG(original.st_mode): raise ValueError("host_path must be a regular archive file")
            if original.st_size > ARCHIVE_LIMIT: raise ValueError("Archive exceeds 64 MiB")
        digest, size, deadline = hashlib.sha256(), 0, time.monotonic() + 30
        with archive.open("wb") as target:
            while size <= ARCHIVE_LIMIT:
                if args.get("url") and time.monotonic() > deadline: raise TimeoutError("Archive download exceeded 30 seconds")
                read = source.read1 if args.get("url") else source.read
                raw = read(min(65536, ARCHIVE_LIMIT + 1 - size))
                if not raw: break
                size += len(raw)
                if size > ARCHIVE_LIMIT: raise ValueError("Archive exceeds 64 MiB")
                digest.update(raw); target.write(raw)
        if original is not None:
            fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if size != original.st_size or fingerprint(original) != fingerprint(os.fstat(source.fileno())):
                raise ValueError("Host archive changed while checking its checksum")
    if not size: raise ValueError("Archive is empty; check the download and its redirects")
    checksum = digest.hexdigest()
    if args.get("expected_sha256") and checksum != args["expected_sha256"].lower(): raise ValueError("Archive checksum mismatch")
    return checksum


def inspect_archive(args):
    """Paginated inventory, without extraction or Macintosh requests."""
    if not shutil.which("lsar"): raise RuntimeError("Install unar/lsar on the bridge to inspect archives")
    start = args.get("start", 0); limit = args.get("limit", 32)
    if not isinstance(start, int) or isinstance(start, bool) or start < 0 or not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 64:
        raise ValueError("Invalid archive pagination")
    with tempfile.TemporaryDirectory(prefix="agentchat-inspect-") as tmp:
        archive = Path(tmp) / "download"; digest = load_source(args, archive)
        listing = archive_listing(archive); selected = select_entries(listing["lsarContents"], args.get("paths"))
        records = {}; total = 0; problems = []
        for _, entry in selected:
            name = entry["XADFileName"]
            record = records.setdefault(name, {"path": name, "directory": bool(entry.get("XADIsDirectory")), "data_bytes": 0, "resource_bytes": 0})
            key = "resource_bytes" if entry.get("XADIsResourceFork") else "data_bytes"
            record[key] += entry.get("XADFileSize", 0); total += entry.get("XADFileSize", 0)
            if not record["directory"]:
                record["type"], record["creator"], record["finder_flags"] = finder_info(entry)
        try:
            folders, files, _, _ = analyze_entries(selected)
            logical_count = len(folders) + len(files)
            problems += import_problems(folders, files, total); sidecar_mode(files, folders)
        except (ValueError, UnicodeError) as exc:
            logical_count = len(records); problems.append(str(exc))
        rows = sorted(records.values(), key=lambda r: r["path"])
        for record in rows:
            if record.get("type") == "APPL" and not record["data_bytes"] and record["resource_bytes"]:
                record["note"] = "Resource-only Classic application; an empty data fork is normal."
        if not shutil.which("unar"): problems.append("Install unar to enable extraction/import")
        return {"archive_sha256": digest, "format": listing.get("lsarFormatName", "unknown"),
                "fork_records": len(selected), "logical_entries": logical_count, "expanded_bytes": total,
                "importable": not problems, "issues": problems, "entries": rows[start:start + limit],
                "next_start": start + limit if start + limit < len(rows) else None,
                "limits": {"archive_bytes": ARCHIVE_LIMIT, "expanded_bytes": EXPANDED_LIMIT, "logical_entries": ENTRY_LIMIT}}


def import_archive(mac, args):
    if not shutil.which("unar") or not shutil.which("lsar"): raise RuntimeError("Install unar/lsar on the bridge to import archives")
    destination = args["destination"].rstrip(":"); parts = destination.split(":")
    if len(parts) < 2 or any(not p or "\0" in p or len(p.encode("mac_roman")) > 31 for p in parts):
        raise ValueError("Destination must be a new absolute HFS folder")
    owned = []
    with tempfile.TemporaryDirectory(prefix="agentchat-archive-") as tmp:
        tmp = Path(tmp); archive = tmp / "download"; output = tmp / "unpacked"; output.mkdir()
        digest = load_source(args, archive)
        folders, files = plan_archive(archive, output, args.get("paths"))
        native_paths = [destination] + [destination + ":" + str(p).replace("/", ":") for p in folders] + [destination + ":" + f["relative"].replace("/", ":") for f in files]
        if any(len(p.encode("mac_roman")) > 255 for p in native_paths): raise ValueError("Imported HFS path exceeds 255 bytes; choose a shorter destination or selection")
        try:
            mac.rpc("mkdir", path=destination); owned.append(destination)
            for folder in folders:
                path = destination + ":" + str(folder).replace("/", ":")
                mac.rpc("mkdir", path=path); owned.append(path)
            report = []
            for entry in files:
                path = destination + ":" + entry["relative"].replace("/", ":")
                data = mac.import_file({"host_path": str(entry["data"].resolve()), "destination": path, "create_only": True})
                owned.append(path)
                resource = None
                if entry["resource"].stat().st_size:
                    resource = mac.import_file({"host_path": str(entry["resource"].resolve()), "destination": path,
                                                "fork": "resource", "create_only": False})
                mac.call("imac_set_file_info", {"path": path, "type": entry["type"], "creator": entry["creator"], "flags": entry["flags"]})
                report.append({"path": path, "data_bytes": data["written"], "resource_bytes": resource["written"] if resource else 0,
                               "data_sha256": data["sha256"], "resource_sha256": resource["sha256"] if resource else hashlib.sha256(b"").hexdigest(),
                               "data_transport": data.get("transport"), "resource_transport": resource.get("transport") if resource else None,
                               "type": entry["type"], "creator": entry["creator"], "finder_flags": entry["flags"] & ~0x100})
            return {"destination": destination, "archive_sha256": digest, "files": report, "folders": len(folders) + 1}
        except Exception as exc:
            remaining = []
            for path in reversed(owned):
                try: mac.rpc("delete", path=path)
                except Exception: remaining.append(path)
            if remaining: raise RuntimeError("Archive import failed: %s. Cleanup incomplete: %r" % (exc, remaining)) from exc
            raise
