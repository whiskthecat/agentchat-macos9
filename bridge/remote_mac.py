"""MCP tool implementation for the attached Classic Macintosh, never the host.

The broker sends only C:RPC, matches nonce + connection, and waits for E: replies.
It cannot create an inference request. Transactions serialize tools across MCP
clients and retain one connection identity from first chunk through completion.
"""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time
import uuid
import zlib
from vm_debug import VM_DEBUG, hexdump
from mac_context import host_capabilities, guide_catalog, read_guide

CHUNK = 3072
MAX_FILE = 16 * 1024 * 1024
MAX_IMPORT_FILE = 64 * 1024 * 1024
HOST_READ_CHUNK = 64 * 1024
SCREEN_CHUNK = 12288
SCREEN_TRANSPORT = "packbits-planar-xrgb"
MAX_SCREEN_BYTES = 16 * 1024 * 1024
SCREEN_CAPTURE_SECONDS = 540


class MacError(RuntimeError):
    pass


class MacBroker:
    def __init__(self):
        self.lock = threading.Lock()
        self.tool_lock = threading.Lock()
        self.pending = {}

    def request(self, session, connection, op, path="", offset=0, count=0, data=b"", fork="data", create_only=False, expected_crc="-"):
        if op not in {"state", "system", "system_items", "system_probe", "registry_probe", "idle_metrics", "hardware", "application_command", "volumes", "list", "stat", "read", "write_begin", "write_stream_begin", "write_chunk", "write_commit", "write_cancel", "mkdir", "rename", "delete", "processes", "process_info", "process_read", "windows", "events", "screen_begin", "screen_read", "screen_end", "focus", "launch", "client", "palette", "set_info", "restore_info", "open"}:
            raise ValueError("Unknown Macintosh operation")
        valid_fork = fork in ("data", "resource")
        if op == "system_probe": valid_fork = fork in ("gestalt", "folder")
        if op == "registry_probe": valid_fork = isinstance(fork, str) and 1 <= len(fork) <= 31 and all(32 <= ord(c) <= 126 for c in fork)
        if not valid_fork or not isinstance(expected_crc, str) or expected_crc != "-" and (len(expected_crc) != 8 or any(c not in "0123456789abcdef" for c in expected_crc)):
            raise ValueError("Invalid fork or checksum")
        if not session.alive or session.connection_id != connection:
            raise MacError("The Macintosh disconnected; this operation was not continued on a new connection.")
        if not isinstance(offset, int) or not 0 <= offset <= 2147483647:
            raise ValueError("Invalid offset")
        if not isinstance(count, int) or not 0 <= count <= 2147483647:
            raise ValueError("Invalid count")
        encoded = path.encode("mac_roman")
        if len(encoded) > 255 or b"\0" in encoded or len(data) > CHUNK:
            raise ValueError("Macintosh path or chunk exceeds its limit")
        nonce = uuid.uuid4().hex
        answer = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[nonce] = (session, connection, answer)
        try:
            fields = [nonce, op, base64.b64encode(encoded).decode(), str(offset), str(count),
                      base64.b64encode(data).decode(), fork, "1" if create_only else "0", expected_crc]
            session.send("C:RPC\t" + "\t".join(fields))
            try: result = answer.get(timeout=20 if op == "application_command" else 15)
            except queue.Empty: raise MacError("Macintosh tool timed out; no request was repeated.")
            if "mac_error" in result:
                codes = {-43: "File not found", -48: "Name already exists", -54: "Permission denied or locked",
                         -47: "File or transaction is busy", -50: "Invalid argument or stale token",
                         -34: "Disk full", -108: "Not enough application memory", -120: "Folder not found"}
                raise MacError(codes.get(result["mac_error"], result.get("error", "Macintosh error")) +
                               " (Mac OS error %s)" % result["mac_error"])
            return result
        finally:
            with self.lock: self.pending.pop(nonce, None)

    def reply(self, session, connection, line):
        try:
            nonce, status, encoded = line[2:].split("\t", 2)
            with self.lock: pending = self.pending.get(nonce)
            if not pending or pending[0] is not session or pending[1] != connection:
                return False
            payload = base64.b64decode(encoded, validate=True)
            if len(payload) > 32768: return False
            result = json.loads(payload.decode("mac_roman"))
            if status not in ("0", "1") or not isinstance(result, dict): return False
            pending[2].put_nowait(result)
            return True
        except (ValueError, queue.Full):
            return False

    def disconnected(self, session, connection):
        with self.lock:
            pending = [p[2] for p in self.pending.values() if p[0] is session and p[1] == connection]
        for answer in pending:
            try: answer.put_nowait({"mac_error": -47, "error": "Macintosh disconnected"})
            except queue.Full: pass


def schema(properties=None, required=()):
    return {"type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}


PATH = {"type": "string", "description": "Absolute HFS path on the Macintosh, e.g. Macintosh HD:Documents:Notes. Use list_volumes first; this is not a host path."}
FORK = {"type": "string", "enum": ["data", "resource"], "default": "data"}
TOOLS = []


def tool(name, description, props=None, required=(), read=True):
    TOOLS.append({"name": "imac_" + name, "description": description, "inputSchema": schema(props, required),
                  "annotations": {"readOnlyHint": read, "destructiveHint": not read, "openWorldHint": False}})


tool("get_state", "Live AgentChat build, OS version, model/contact, connection, draft length, transcript rows, heap and display diagnostics.")
tool("get_system_configuration", "Read native memory configuration (VM enabled, physical/logical RAM, backing store, disk cache), OS/MMU, Open Transport and sound status, blessed System Folder paths, hardware/display facts and running process partitions. Unsupported facts are null. Read-only. Use list_system_items for enabled-on-disk extensions/control panels; it does not prove they ran at boot.")
tool("list_system_items", "Paginate enabled or disabled System Folder items through the native Folder Manager. Enabled-on-disk is not a resident INIT list. No folders are created.", {"folder": {"type": "string", "enum": ["extensions", "control_panels", "startup_items", "disabled_extensions", "disabled_control_panels"]}, "start": {"type": "integer", "minimum": 1, "maximum": 32767, "default": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 32, "default": 24}}, ("folder",))
tool("system_probe", "Read a public native Gestalt selector, resolve a Folder Manager selector without creating it, or read an exact PowerPC Name Registry path/property (at most 512 bytes). Gestalt/folder selectors are four ASCII characters including spaces, or 0x followed by eight hex digits. Unknown queries preserve status and null. Registry paths come from get_hardware. No setters, arbitrary pointers or executable probes.", {"kind": {"type": "string", "enum": ["gestalt", "folder", "registry"]}, "selector": {"type": "string"}, "path": {"type": "string", "maxLength": 255}, "property": {"type": "string", "maxLength": 31}}, ("kind",))
tool("get_idle_metrics", "Read cumulative AgentChat event-loop wakes, elapsed time outside the outer WaitNextEvent, actual network receive/send calls and OT notifications. Subtract two snapshots for rates; reading itself is work. Nested waits/preemption can be included; no separately attributed OS/network interrupt time. Never resets counters or changes scheduling.")
tool("get_hardware", "Read the Macintosh's native hardware inventory through Gestalt, QuickDraw display devices and the PowerPC Name Registry: machine/CPU family, clock selectors, RAM, OS version, display dimensions/depth and bounded CPU/GPU registry properties. Unknown selectors return null; retain exact raw values rather than guessing hardware. Read-only; no firmware changes or inference. Requires a matching client build.")
tool("get_context", "Read the current Macintosh connection, mounted volumes, enabled native/VM/archive capabilities and bundled operating guides before a Mac task. Works offline too, reporting which guest facts are unavailable. Never starts inference.")
tool("read_guide", "Read bundled Mac operating instructions or a task workflow. Use install-software for archive/fork-preserving installations, edit-files for native file changes, and debug-mac for RAM/desktop/VM inspection. Works without a guest connection.",
     {"guide": {"type": "string", "enum": ["operating", "install-software", "edit-files", "debug-mac"]}}, ("guide",))
tool("list_volumes", "List mounted Macintosh disks, free space and lock state.")
tool("list_directory", "List a Macintosh folder with both fork sizes, Finder type/creator, dates and lock state. Follow next_index for pagination.",
     {"path": PATH, "start": {"type": "integer", "minimum": 1, "default": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 32, "default": 24}}, ("path",))
tool("file_info", "Inspect a Macintosh file or folder including resource fork and Finder metadata.", {"path": PATH}, ("path",))
tool("read_file", "Read a bounded slice of a Macintosh data or resource fork. MacRoman text is returned with LF newlines; base64 preserves arbitrary bytes. Follow next_offset for larger files.",
     {"path": PATH, "fork": FORK, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 16384},
      "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text"}}, ("path",))
tool("write_file", "Write a Macintosh fork atomically through a temporary sibling and File Manager exchange. Preserve the other fork and Finder metadata. Text uses MacRoman/CR. Optional expected_sha256 detects a stale edit; new files can require absence. Maximum 16 MiB.",
     {"path": PATH, "content": {"type": "string"}, "fork": FORK, "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text"},
      "expected_sha256": {"type": "string"}, "create_only": {"type": "boolean", "default": False}}, ("path", "content"), False)
tool("import_file", "Stream an existing regular file from the relay host to one Macintosh fork, without binary content in model arguments or results. Absolute host_path and exact HFS destination required. Maximum 64 MiB; SHA-256 is checked before guest writes, and streaming checks size, SHA-256 and native CRC before atomic commit. create_only defaults true; replacing a fork preserves the other fork and Finder metadata. expected_sha256 describes the HOST source, not an existing destination. This is a single raw fork; use import_archive for fork-preserving Mac archives.",
     {"host_path": {"type": "string", "minLength": 1, "maxLength": 4096}, "destination": PATH, "fork": FORK,
      "expected_sha256": {"type": "string", "minLength": 64, "maxLength": 64}, "create_only": {"type": "boolean", "default": True}},
     ("host_path", "destination"), False)
tool("replace_in_file", "Modify exact text in a Macintosh data fork, preserving the resource fork and Finder metadata. Require exactly one match unless replace_all is true; check expected_sha256 for a previously read version.",
     {"path": PATH, "old_text": {"type": "string"}, "new_text": {"type": "string"}, "replace_all": {"type": "boolean", "default": False}, "expected_sha256": {"type": "string"}}, ("path", "old_text", "new_text"), False)
tool("make_directory", "Create one Macintosh directory; its parent must exist.", {"path": PATH}, ("path",), False)
tool("rename", "Rename a Macintosh file or folder within its current parent. Preserve both forks and metadata.", {"path": PATH, "new_name": {"type": "string", "maxLength": 31}}, ("path", "new_name"), False)
tool("delete", "Delete the exact Macintosh file or empty folder. Does not recurse or accept wildcards.", {"path": PATH}, ("path",), False)
tool("list_processes", "Read one bounded native Process Manager snapshot: accumulated active ticks, launch ticks, partition/free heap, mode/identity/launcher and foreground/background state for up to 40 processes. Native timestamps and scan cost allow subtraction without using RPC latency. Ticks are nominal 1/60 second, coarse scheduler accounting rather than hardware busy time; no interrupt attribution. This query does not arm AgentChat burst polling.")
tool("get_process_usage", "Compare two native process snapshots across 1..60 seconds (default 1). The relay waits quietly between exactly two scans; no periodic native sampler or timer. Uses native tick timestamps, not round-trip time; includes both snapshots and per-process active tick deltas as a fraction of native elapsed time. Idle/yield/system time may be charged to a process; zero residual does not prove zero OS/IRQ work. A signed residual is unaccounted wall time, NOT interrupt CPU utilization: idle, system work, coarse counters and scan skew cannot be separated. Process churn or truncated inventories make residual unknown; exited/new processes have no complete delta. Holds one connection and the Mac tool transaction; fails on reconnect. Read-only, no inference.",
     {"seconds": {"type": "integer", "minimum": 1, "maximum": 60, "default": 1}})
tool("list_windows", "Inspect AgentChat's native windows, their bounds, titles and visibility. Screenshot shows the entire desktop.")
tool("event_log", "Read the last 64 client diagnostic events, status changes and remote tool names. Contains no file bodies or prompts.")
tool("screenshot", "Capture the Macintosh's current desktop in full color through QuickDraw. Returns a PNG image plus transport dimensions/byte counts. Compatible clients use native PackBits; older clients fall back to exact raw pixels. Physical hardware may need minutes; the bounded wait is 600 seconds. Requires no VNC viewer.")
tool("launch_application", "Launch a Macintosh APPL file through the native Process Manager.", {"path": PATH}, ("path",), False)
tool("focus_application", "Bring a running Macintosh application forward using IDs from list_processes.", {"high": {"type": "integer", "minimum": 0}, "low": {"type": "integer", "minimum": 0}}, ("high", "low"), False)
tool("client_action", "Operate AgentChat: redraw, arrange/recover its windows, stop a reply, start a new chat, select a known model contact, or set a draft. A draft is never sent by this tool; only the human Send/Return action submits inference.",
     {"action": {"type": "string", "enum": ["redraw", "arrange_windows", "stop", "new_chat", "select_model", "set_draft"]}, "value": {"type": "string", "default": ""}}, ("action",), False)
tool("send_image", "Show an image inline in the current Macintosh chat and save it in conversation history, as an assistant message. Use this to return generated/edited pictures, diagrams or screenshots to the user; opening a document in Finder does not put it in chat. source=host reads a file on the relay computer; source=mac reads the Macintosh data fork by HFS path. PNG/JPEG/GIF/BMP/TIFF/WebP are normalized to native QuickDraw pixels. GIF shows its first frame. Up to 16 MiB/16 million pixels, stored at up to 768px and previewed at 320x240. No inference is submitted. Get the current conversation ID from get_state to prevent delivery to a different chat.",
     {"path": {"type": "string", "minLength": 1, "maxLength": 4096},
      "source": {"type": "string", "enum": ["host", "mac"], "default": "host"},
      "conversation": {"type": "string", "minLength": 1, "maxLength": 104},
      "caption": {"type": "string", "maxLength": 1024}}, ("path", "conversation"), False)

tool("set_file_info", "Set native Finder type, creator and optional flags without altering either file fork. Omitted fields are preserved. Finder's cached initialization flag is cleared to refresh icons.",
     {"path": PATH, "type": {"type": "string", "minLength": 4, "maxLength": 4}, "creator": {"type": "string", "minLength": 4, "maxLength": 4},
      "flags": {"type": "integer", "minimum": 0, "maximum": 65535}}, ("path",), False)
tool("restore_file_info", "Restore snapshotted native HFS creation/modification dates and exact Finder flags on an existing unlocked file. Dates are unsigned seconds since 1904 as reported by file_info. Retains both forks, type, creator and remaining catalog fields; does not clear Finder's initialization flag. Use after restoring and verifying trial application forks. Requires native client v95 or later.",
     {"path": PATH, "created_mac": {"type": "integer", "minimum": 0, "maximum": 4294967295},
      "modified_mac": {"type": "integer", "minimum": 0, "maximum": 4294967295},
      "finder_flags": {"type": "integer", "minimum": 0, "maximum": 65535}},
     ("path", "created_mac", "modified_mac", "finder_flags"), False)
tool("open_document", "Ask the native Finder to open a file with its registered application. Also opens applications normally using their Finder metadata. Submission does not guarantee the receiving application's success.", {"path": PATH}, ("path",), False)
ARCHIVE_SOURCE = {"url": {"type": "string"}, "host_path": {"type": "string"}, "expected_sha256": {"type": "string"},
                  "paths": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 1024}, "minItems": 1, "maxItems": 32}}
tool("inspect_archive", "Read a host/downloaded Classic Mac archive inventory without extraction or guest writes. Reports format, SHA-256, exact paths, logical entry count, both fork sizes, Finder codes and import issues. Installed lsar recognizes BinHex, StuffIt, MacBinary and supported ZIPs, including nested formats; do not invent a decoder before inspection. Resource-only APPL files are normal. Optional paths select exact file names or folder subtrees; pagination follows next_start.",
     {**ARCHIVE_SOURCE, "start": {"type": "integer", "minimum": 0, "default": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 64, "default": 32}})
TOOLS[-1]["annotations"]["openWorldHint"] = True
tool("import_archive", "Download or read an original host archive and import it into a NEW Macintosh HFS folder through the native File Manager, preserving both forks and Finder metadata. Inspect first; pass optional paths to select exact archive files (both forks) or folder subtrees while retaining relative paths. Installed unar handles StuffIt, BinHex and MacBinary. Maximum 16 MiB archive, 4 MiB selected forks, 128 logical HFS files/folders. Handles legitimate .rsrc filenames. Requires unar/lsar; no mounted disk or model binary payload.",
     {**ARCHIVE_SOURCE, "destination": PATH}, ("destination",), False)
TOOLS[-1]["annotations"]["openWorldHint"] = True

PSN = {"high": {"type": "integer", "minimum": 0, "maximum": 2147483647},
       "low": {"type": "integer", "minimum": 0, "maximum": 2147483647}}
tool("application_command", "Send one bounded JSON object to a running cooperating Classic application's private AGCT/cmnd Apple-event handler, using its actual PSN from list_processes. Supports applications with an explicitly implemented handler, such as the native OpenRCT2 port; arbitrary apps are not made scriptable. Never launches/focuses the target or sends inference. Command is at most 1024 serialized ASCII bytes; response must be a JSON object of at most 4096 ASCII bytes. Native wait is 10 seconds and never retries. A timeout/canceled wait may mean the command executed; inspect state before repeating writes.",
     {**PSN, "command": {"type": "object", "description": "Application-defined command object, e.g. {\"op\":\"status\"}; use that application's documented verbs."}},
     ("high", "low", "command"), False)
MEMORY_RANGE = {"offset": {"type": "integer", "minimum": 0, "maximum": 2147483647, "default": 0},
                "count": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 256}}
tool("process_info", "Inspect another native application's live partition address, size, flags, CPU ticks, launch identity and foreground state. Use the Process Manager IDs from list_processes.", PSN, ("high", "low"))
tool("read_process_memory", "Read a bounded range of another Macintosh application's RAM using native shared-address-space access. Offset is relative to its Process Manager partition. Returns exact base64 and a hex/ASCII dump. Live reads may change between chunks; no memory writes.",
     {**PSN, **MEMORY_RANGE}, ("high", "low"))
tool("find_process_memory", "Search a bounded window of another Macintosh application's RAM for exact MacRoman text or base64 bytes. Returns partition offsets and absolute addresses. Follow next_offset; search windows overlap to catch boundary matches. Maximum 64 matches per call.",
     {**PSN, **MEMORY_RANGE, "pattern": {"type": "string"}, "encoding": {"type": "string", "enum": ["text", "base64"], "default": "text"}}, ("high", "low", "pattern"))
tool("vm_status", "VM-only: QEMU CPU running/paused state and pause lease. Works while AgentChat is stopped or disconnected; requires AGENT_QMP_SOCKET. Does not operate a physical G3.")
tool("vm_pause", "VM-only: halt the whole Macintosh CPU, including all programs, through QMP. Auto-resume after the requested lease. While paused, use vm_registers/vm_read_memory and vm_resume; native client tools cannot run.",
     {"seconds": {"type": "integer", "minimum": 1, "maximum": 300, "default": 30}}, (), False)
tool("vm_resume", "VM-only: continue the whole Macintosh CPU. Optional pause_lease rejects stale resume requests; omit it for explicit recovery of an externally paused VM.",
     {"pause_lease": {"type": "string"}}, (), False)
tool("vm_registers", "VM-only: inspect the current paused PowerPC CPU registers, including instruction pointer and stack pointer. Pause the VM first; these are the current CPU context, not a chosen process's saved registers.")
tool("vm_read_memory", "VM-only: inspect a paused Macintosh virtual or physical memory range, returning base64 and hex/ASCII. Virtual reads use the current CPU mapping; exception handlers may have translation disabled. Check vm_registers before using a process_info application address. Requires a paused VM.",
     {"address": {"type": "integer", "minimum": 0, "maximum": 4294967295},
      "count": {"type": "integer", "minimum": 1, "maximum": 65536, "default": 256},
      "space": {"type": "string", "enum": ["virtual", "physical"], "default": "virtual"}}, ("address",))

tool("vm_input", "VM-only: send bounded desktop mouse or keyboard input through QEMU virtual hardware. Use screenshot and window bounds first; key/text act on the CURRENT foreground application. All pressed buttons/modifiers are released. Not available on a physical G3.",
     {"action": {"type": "string", "enum": ["click", "double_click", "drag", "key", "text"]},
      "x": {"type": "integer", "minimum": 0}, "y": {"type": "integer", "minimum": 0},
      "end_x": {"type": "integer", "minimum": 0}, "end_y": {"type": "integer", "minimum": 0},
      "width": {"type": "integer", "minimum": 2, "maximum": 8192, "default": 800},
      "height": {"type": "integer", "minimum": 2, "maximum": 8192, "default": 600},
      "key": {"type": "string"}, "modifiers": {"type": "string", "description": "Comma-separated cmd,shift,option,control."},
      "text": {"type": "string", "maxLength": 512}}, ("action",), False)


def process_usage_delta(before, after):
    """Subtract 32-bit native tick counters, never host/RPC clocks.

    A disappearing process has no final active counter. Preserve that unknown
    instead of assigning its CPU time to the system. PSN plus launch tick guards
    against replacement. No clamping of negative residuals/percent over 100:
    scheduler quantization and non-atomic scan skew are evidence, not CPU idle.
    """
    def ticks(value):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 0xffffffff:
            raise MacError("Client lacks valid process accounting; requires AgentChat v93 or newer")
        return value
    def delta(a, b): return (ticks(b) - ticks(a)) & 0xffffffff
    def identity(p): return (p["high"], p["low"], ticks(p["launch_ticks"]))
    elapsed = delta(before.get("sample_start_ticks"), after.get("sample_start_ticks"))
    if not 0 < elapsed < 0x80000000:
        raise MacError("Native process sampling interval is zero or discontinuous")
    for snap in (before, after):
        if snap.get("ticks_per_second") != 60 or "truncated" not in snap:
            raise MacError("Client lacks process accounting; requires AgentChat v93 or newer")
    old = {identity(p): p for p in before["processes"]}
    new = {identity(p): p for p in after["processes"]}
    rows = []; total = 0; complete = not before["truncated"] and not after["truncated"]
    for ident in dict.fromkeys([*old, *new]):
        a, b = old.get(ident), new.get(ident); p = b or a
        row = {k: p[k] for k in ("name", "high", "low", "launch_ticks")}
        row.update(status="continuing" if a and b else "started" if b else "exited",
                   active_delta_ticks=None, elapsed_ticks=None, active_percent=None)
        if a and b:
            used = delta(a.get("active_ticks"), b.get("active_ticks"))
            interval = delta(a.get("sample_ticks"), b.get("sample_ticks"))
            if used < 0x80000000 and 0 < interval < 0x80000000:
                row.update(active_delta_ticks=used, elapsed_ticks=interval,
                           active_percent=round(100 * used / interval, 3))
                total += used
            else:
                row["status"] = "counter_discontinuity"; complete = False
        else:
            complete = False
        rows.append(row)
    rows.sort(key=lambda p: (p["active_delta_ticks"] is None, -(p["active_delta_ticks"] or 0), p["name"]))
    return {"elapsed_ticks": elapsed, "elapsed_seconds": elapsed / 60,
            "ticks_per_second": 60, "processes": rows,
            "measured_active_ticks": total, "accounting_complete": complete,
            "unaccounted_ticks": elapsed - total if complete else None,
            "unaccounted_percent": round(100 * (elapsed - total) / elapsed, 3) if complete else None,
            "interrupt_ticks": None,
            "scope": "Process Manager scheduler-accounted active time, not hardware busy CPU utilization; idle/yield/system time may be charged to processes, so zero residual does not prove zero OS/IRQ work; signed residual includes unaccounted wall time and scan/counter skew; cannot isolate interrupt time; process churn/truncation makes residual unknown; endpoint scans add observer work",
            "before": before, "after": after}


def native_text(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r").encode("mac_roman")


def unpack_screen_bits(packed, expected):
    """Decode native PackBits packets without trusting their expansion lengths."""
    if not isinstance(expected, int) or isinstance(expected, bool) or not 1 <= expected <= MAX_SCREEN_BYTES:
        raise MacError("Invalid expanded screenshot size")
    if len(packed) > MAX_SCREEN_BYTES:
        raise MacError("Packed screenshot exceeds its limit")
    result = bytearray(); at = 0
    while at < len(packed):
        code = packed[at]; at += 1
        if code == 128: continue  # Standard PackBits no-op.
        repeat = code > 128
        length = 257-code if repeat else code+1
        source_length = 1 if repeat else length
        if source_length > len(packed)-at:
            raise MacError("Truncated screenshot PackBits packet")
        if length > expected-len(result):
            raise MacError("Screenshot PackBits packet exceeds the declared size")
        if repeat: result.extend(bytes([packed[at]]) * length)
        else: result.extend(packed[at:at+length])
        at += source_length
    if len(result) != expected:
        raise MacError("Incomplete screenshot PackBits data")
    return result


class MacTools:
    def __init__(self, broker, session):
        self.broker, self.session = broker, session
        self.connection = getattr(session, "connection_id", None)

    def rpc(self, op, **params):
        return self.broker.request(self.session, self.connection, op, **params)

    def read(self, path, fork="data", offset=0, limit=MAX_FILE):
        data = bytearray()
        while len(data) < limit:
            part = self.rpc("read", path=path, fork=fork, offset=offset+len(data), count=min(CHUNK, limit-len(data)))
            raw = base64.b64decode(part["data"], validate=True)
            if len(raw) != part["bytes"]: raise MacError("Incomplete Macintosh read")
            data += raw
            if part["eof"]: break
        return bytes(data), part["total"]

    def write(self, path, data, fork="data", expected=None, create_only=False):
        if len(data) > MAX_FILE: raise ValueError("File exceeds 16 MiB")
        if create_only:
            try: self.rpc("stat", path=path)
            except MacError as e:
                if "-43" not in str(e): raise
            else: raise MacError("File already exists; create_only was requested")
        expected_crc = "-"
        if expected:
            current, total = self.read(path, fork)
            if total > len(current) or hashlib.sha256(current).hexdigest() != expected:
                raise MacError("The file changed; read it again before editing")
            expected_crc = "%08x" % zlib.crc32(current)
        token = self.rpc("write_begin", path=path, count=len(data), fork=fork, create_only=create_only, expected_crc=expected_crc)["token"]
        try:
            for offset in range(0, len(data), CHUNK):
                self.rpc("write_chunk", path=token, offset=offset, data=data[offset:offset+CHUNK])
            committed = self.rpc("write_commit", path=token)
        except Exception:
            try: self.rpc("write_cancel", path=token)
            except Exception: pass
            raise
        return {"written": len(data), "sha256": hashlib.sha256(data).hexdigest(), "path": path, "fork": fork,
                "cleanup_warning": committed.get("cleanup_warning", False)}

    def import_file(self, args):
        path = Path(args["host_path"])
        if not path.is_absolute():
            raise ValueError("Use an absolute path to a regular file on the relay host")
        expected = args.get("expected_sha256")
        if expected is not None:
            expected = expected.lower()
            if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
                raise ValueError("expected_sha256 must contain 64 hexadecimal digits")
        # Nonblocking open prevents a file replaced with a FIFO from hanging.
        # Keep this descriptor through both passes so path replacement cannot
        # swap the source between validation and upload.
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            original = os.fstat(source.fileno())
            if not stat.S_ISREG(original.st_mode):
                raise ValueError("Source must be a regular host file")
            if original.st_size > MAX_IMPORT_FILE:
                raise ValueError("Host file exceeds 64 MiB")
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            digest, crc, total = hashlib.sha256(), 0, 0
            while raw := source.read(HOST_READ_CHUNK):
                total += len(raw)
                if total > original.st_size:
                    raise MacError("Host source changed while checking its checksum")
                digest.update(raw); crc = zlib.crc32(raw, crc)
            if total != original.st_size or identity(os.fstat(source.fileno())) != identity(original):
                raise MacError("Host source changed while checking its checksum")
            checksum = digest.hexdigest()
            if expected is not None and checksum != expected:
                raise ValueError("Host file SHA-256 does not match expected_sha256")
            source.seek(0)
            destination, fork = args["destination"], args.get("fork", "data")
            crc_hex = "%08x" % crc
            token = self.rpc("write_stream_begin", path=destination, count=total, fork=fork,
                             create_only=args.get("create_only", True), expected_crc=crc_hex)["token"]
            try:
                uploaded, uploaded_crc, uploaded_hash = 0, 0, hashlib.sha256()
                while uploaded < total:
                    raw = source.read(min(CHUNK, total-uploaded))
                    if not raw: raise MacError("Host source became shorter during import")
                    response = self.rpc("write_chunk", path=token, offset=uploaded, data=raw)
                    uploaded += len(raw)
                    if response.get("received") != uploaded:
                        raise MacError("Macintosh did not acknowledge the complete import chunk")
                    uploaded_hash.update(raw); uploaded_crc = zlib.crc32(raw, uploaded_crc)
                if (source.read(1) or uploaded_hash.hexdigest() != checksum or uploaded_crc != crc or
                        identity(os.fstat(source.fileno())) != identity(original)):
                    raise MacError("Host source changed during import; staged data was cancelled")
                committed = self.rpc("write_commit", path=token)
                if (committed.get("committed") is not True or committed.get("written") != total or
                        committed.get("crc32") != crc_hex):
                    raise MacError("Macintosh did not verify the imported fork's size and CRC")
            except Exception:
                try: self.rpc("write_cancel", path=token)
                except Exception: pass
                raise
        return {"path": destination, "fork": fork, "written": total, "sha256": checksum, "crc32": crc_hex,
                "cleanup_warning": committed.get("cleanup_warning", False)}

    def screenshot(self):
        from PIL import Image
        # Old clients ignore this request path and retain their raw metadata.
        started = time.monotonic()
        meta = self.rpc("screen_begin", path=SCREEN_TRANSPORT)
        failed = False
        try:
            if not isinstance(meta, dict): raise MacError("Invalid screenshot metadata")
            width, height, stride = (meta.get(key) for key in ("width", "height", "stride"))
            if (any(not isinstance(n, int) or isinstance(n, bool) for n in (width, height, stride)) or
                    not 1 <= width <= 4096 or not 1 <= height <= 8192 or
                    not width*4 <= stride <= 16383 or stride % 4 or meta.get("format") != "XRGB"):
                raise MacError("Invalid screenshot dimensions or pixel format")
            unpacked_bytes = stride*height
            if unpacked_bytes > MAX_SCREEN_BYTES: raise MacError("Unexpected desktop size")
            transport = meta.get("transport", "raw")
            if transport not in ("raw", SCREEN_TRANSPORT): raise MacError("Unknown screenshot transport")
            total = meta.get("transport_bytes", unpacked_bytes)
            if (not isinstance(total, int) or isinstance(total, bool) or not 1 <= total <= unpacked_bytes or
                    (transport == "raw" and total != unpacked_bytes)):
                raise MacError("Invalid screenshot transport size")
            token = meta.get("token")
            if not isinstance(token, str) or not 1 <= len(token) <= 40 or "\0" in token:
                raise MacError("Invalid screenshot token")
            data = bytearray(); read_chunks = 0
            while len(data) < total:
                # Leave room within the outer 600-second wait for the current
                # bounded RPC and screen_end, releasing the transaction lock.
                if time.monotonic()-started >= SCREEN_CAPTURE_SECONDS:
                    raise MacError("Screenshot exceeded its 540-second capture budget; snapshot cleanup was requested")
                count = min(SCREEN_CHUNK, total-len(data))
                part = self.rpc("screen_read", path=token, offset=len(data), count=count)
                if not isinstance(part, dict) or part.get("total") != total:
                    raise MacError("Screenshot transport changed during capture")
                encoded = part.get("data")
                if not isinstance(encoded, str) or len(encoded) > (SCREEN_CHUNK+2)//3*4:
                    raise MacError("Invalid screenshot chunk")
                try: raw = base64.b64decode(encoded, validate=True)
                except ValueError as exc: raise MacError("Invalid screenshot chunk encoding") from exc
                if len(raw) != count: raise MacError("Incomplete or oversized screenshot chunk")
                data += raw
                read_chunks += 1
            if transport == SCREEN_TRANSPORT:
                planar = unpack_screen_bits(data, unpacked_bytes)
                data = bytearray(unpacked_bytes); plane_bytes = unpacked_bytes//4
                for channel in range(4):
                    data[channel::4] = planar[channel*plane_bytes:(channel+1)*plane_bytes]
            img = Image.frombytes("RGB", (width, height), bytes(data), "raw", "XRGB", stride)
            out = io.BytesIO(); img.save(out, format="PNG")
            return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(out.getvalue()).decode()}],
                    "structuredContent": {"width": width, "height": height, "stride": stride, "transport": transport,
                                          "transport_bytes": total, "unpacked_bytes": unpacked_bytes, "read_chunks": read_chunks}}
        except BaseException:
            failed = True
            raise
        finally:
            try: self.rpc("screen_end")
            except Exception:
                if not failed: raise

    def application_command(self, args):
        if not isinstance(args["command"], dict): raise ValueError("Application command must be a JSON object")
        try:
            command = json.dumps(args["command"], ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("Application command must contain JSON values") from exc
        if len(command) > 1024: raise ValueError("Application command exceeds 1024 serialized ASCII bytes")
        try:
            result = self.rpc("application_command", offset=args["high"], count=args["low"], data=command)
        except MacError as exc:
            raise MacError(str(exc) + "; command may have executed. Inspect application state before repeating; no request was retried.") from exc
        if not isinstance(result, dict): raise MacError("Invalid application reply envelope")
        error, application_error, elapsed = (result.get(key) for key in ("error", "application_error", "elapsed"))
        if (any(not isinstance(n, int) or isinstance(n, bool) for n in (error, application_error, elapsed)) or
                not -32768 <= error <= 32767 or not -2147483648 <= application_error <= 2147483647 or
                not 0 <= elapsed <= 4294967295):
            raise MacError("Invalid application reply error/timing fields")
        detail = result.get("detail", "")
        if not isinstance(detail, str) or len(detail) > 191: raise MacError("Invalid application reply detail")
        if error or application_error:
            message = detail or "Application rejected the command"
            message += " (Mac OS error %d, application error %d)" % (error, application_error)
            if error in (-1711, -1712): message += "; command may have executed. Inspect state before repeating writes."
            raise MacError(message)
        text = result.get("reply")
        if (not isinstance(text, str) or not 1 <= len(text) <= 4096 or
                any(ord(ch) > 126 or (ord(ch) < 32 and ch not in "\t\r\n") for ch in text)):
            raise MacError("Application reply must be 1..4096 ASCII JSON bytes")
        try:
            def invalid_number(value): raise ValueError("Non-finite JSON number")
            reply = json.loads(text, parse_constant=invalid_number)
        except (ValueError, RecursionError) as exc: raise MacError("Application returned malformed JSON") from exc
        if not isinstance(reply, dict): raise MacError("Application reply must be a JSON object")
        return {"high": args["high"], "low": args["low"], "elapsed_ticks": elapsed, "reply": reply}

    def process_memory(self, args):
        high, low = args["high"], args["low"]
        meta = self.rpc("process_info", offset=high, count=low)
        offset = args.get("offset", 0); count = args.get("count", 256)
        if offset >= meta["partition_bytes"]: raise ValueError("Offset is outside the application partition")
        count = min(count, meta["partition_bytes"]-offset)
        data = bytearray()
        while len(data) < count:
            part = self.rpc("process_read", path="%d:%d" % (high,low), offset=offset+len(data), count=min(CHUNK,count-len(data)))
            if any(part[k] != meta[k] for k in ("base_address", "partition_bytes", "launch_ticks")):
                raise MacError("The application partition changed during the read")
            raw = base64.b64decode(part["data"], validate=True)
            if len(raw) != min(CHUNK,count-len(data)): raise MacError("Incomplete process memory read")
            data += raw
        return bytes(data), meta

    def call(self, name, args):
        definitions = {t["name"]: t for t in TOOLS}
        if name not in definitions: raise ValueError("Unknown Macintosh tool")
        definition = definitions[name]["inputSchema"]
        if not isinstance(args, dict) or set(args) - set(definition["properties"]): raise ValueError("Unknown tool arguments")
        if any(k not in args for k in definition["required"]): raise ValueError("Missing tool arguments")
        for key, value in args.items():
            s = definition["properties"][key]
            typ = s["type"]
            if typ == "string" and not isinstance(value, str) or typ == "boolean" and not isinstance(value, bool) or typ == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError("Invalid type for " + key)
            if typ == "string" and (len(value) < s.get("minLength", 0) or len(value) > s.get("maxLength", 2147483647)): raise ValueError("Invalid length for " + key)
            if typ == "array":
                if not isinstance(value, list) or not s.get("minItems", 0) <= len(value) <= s.get("maxItems", 2147483647):
                    raise ValueError("Invalid array for " + key)
                item = s["items"]
                if any(not isinstance(v, str) or not item.get("minLength", 0) <= len(v) <= item.get("maxLength", 2147483647) for v in value):
                    raise ValueError("Invalid item for " + key)
            if typ == "object" and not isinstance(value, dict): raise ValueError("Invalid object for " + key)
            if "enum" in s and value not in s["enum"]: raise ValueError("Invalid " + key)
            if "minimum" in s and value < s["minimum"] or "maximum" in s and value > s["maximum"]: raise ValueError("Out of range " + key)
        short = name.removeprefix("imac_")
        if short.startswith("vm_"):
            result = VM_DEBUG.call(short, args)
            return {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}
        simple = {"get_state": "state", "get_hardware": "hardware", "get_idle_metrics": "idle_metrics", "list_volumes": "volumes", "file_info": "stat", "make_directory": "mkdir", "delete": "delete",
                  "list_processes": "processes", "list_windows": "windows", "event_log": "events", "launch_application": "launch", "open_document": "open"}
        if short == "read_guide":
            result = {"guide": args["guide"], "text": read_guide(args["guide"])}
        elif short == "get_context":
            result = host_capabilities()
            result["guides"] = guide_catalog()
            result["connected"] = bool(self.session and self.session.alive and self.connection)
            result["guest"] = None; result["volumes"] = []
            paused = False
            if result["vm_tools_enabled"]:
                try:
                    result["vm"] = VM_DEBUG.call("vm_status", {})
                    paused = not result["vm"].get("running", True)
                except (RuntimeError, OSError) as exc: result["vm_error"] = str(exc)
            if result["connected"] and not paused:
                try:
                    result["guest"] = self.rpc("state")
                    result["volumes"] = self.rpc("volumes")["volumes"]
                except RuntimeError as exc: result["guest_error"] = str(exc)
            elif paused: result["guest_error"] = "VM CPU is paused; resume it before native inspection"
            result["native_tools_ready"] = result["connected"] and not paused and result["guest"] is not None
            result["available_tools"] = [t["name"] for t in TOOLS if
                (t["name"].startswith("imac_vm_") and result["vm_tools_enabled"]) or
                (t["name"] in {"imac_get_context", "imac_read_guide"}) or
                (t["name"] == "imac_inspect_archive" and result["archive_tools_available"]) or
                (result["native_tools_ready"] and not t["name"].startswith("imac_vm_") and t["name"] not in {"imac_get_context", "imac_read_guide", "imac_inspect_archive", "imac_import_archive"}) or
                (result["native_tools_ready"] and result["archive_tools_available"] and t["name"] == "imac_import_archive")]
        elif short == "inspect_archive":
            from mac_archive import inspect_archive
            result = inspect_archive(args)
        elif short in simple: result = self.rpc(simple[short], path=args.get("path", ""))
        elif short == "set_file_info":
            current = self.rpc("stat", path=args["path"])
            if current["directory"]: raise ValueError("Finder type and creator apply to files")
            codes = args.get("type", current["type"].ljust(4,"\0")).encode("mac_roman") + args.get("creator", current["creator"].ljust(4,"\0")).encode("mac_roman")
            if len(codes) != 8: raise ValueError("Finder type and creator must each be four MacRoman bytes")
            result = self.rpc("set_info", path=args["path"], data=codes, offset=args.get("flags", 0), count=int("flags" in args))
        elif short == "restore_file_info":
            values = [(args["finder_flags"], 2), (args["created_mac"], 4), (args["modified_mac"], 4)]
            if any(type(value) is not int or not 0 <= value < 1 << (8 * size) for value, size in values):
                raise ValueError("Catalog dates/flags must be unsigned native integers")
            payload = b"".join(value.to_bytes(size, "big") for value, size in values)
            self.rpc("restore_info", path=args["path"], data=payload)
            # Some Classic File Manager date edge cases normalize on a later
            # catalog access. Confirm through a separate RPC before claiming
            # exact restoration; never retry a metadata write automatically.
            result = self.rpc("stat", path=args["path"])
            if any(result.get(key) != args[key] for key in ("created_mac", "modified_mac", "finder_flags")):
                raise MacError("File Manager did not retain the requested catalog values; inspect file_info before repeating writes")
        elif short == "import_archive":
            from mac_archive import import_archive
            result = import_archive(self, args)
        elif short == "import_file": result = self.import_file(args)
        elif short == "application_command": result = self.application_command(args)
        elif short == "send_image":
            from chat_images import MAX_SOURCE_BYTES
            if self.session.sid != args['conversation']:
                raise MacError("Conversation changed; inspect get_state before sending a picture")
            if args.get('source', 'host') == 'mac':
                data, total = self.read(args['path'], limit=MAX_SOURCE_BYTES)
                if total > len(data): raise ValueError("Picture exceeds 16 MiB")
            else:
                path = Path(args['path'])
                if not path.is_absolute() or not path.is_file():
                    raise ValueError("Use an absolute path to a regular image file on the relay host")
                with path.open('rb') as stream: data = stream.read(MAX_SOURCE_BYTES + 1)
            result = self.session.publish_image(data, args['conversation'], self.connection, args.get('caption', ''))
        elif short == "list_directory": result = self.rpc("list", path=args["path"], offset=args.get("start", 1), count=args.get("limit", 24))
        elif short == "rename": result = self.rpc("rename", path=args["path"], data=args["new_name"].encode("mac_roman"))
        elif short == "focus_application": result = self.rpc("focus", offset=args["high"], count=args["low"])
        elif short == "client_action": result = self.rpc("client", path=args["action"], data=native_text(args.get("value", "")))
        elif short == "get_system_configuration":
            result = self.rpc("system")
            result["hardware"] = self.rpc("hardware")
            result["processes"] = self.rpc("processes")["processes"]
        elif short == "list_system_items":
            folders = {"extensions": "extn", "control_panels": "ctrl", "startup_items": "strt", "disabled_extensions": "extD", "disabled_control_panels": "ctrD"}
            result = self.rpc("system_items", path=folders[args["folder"]], offset=args.get("start", 1), count=args.get("limit", 24))
            result["inventory_basis"] = "enabled/disabled on disk; not proof of boot-time loading"
        elif short == "system_probe":
            if args["kind"] == "registry":
                path, prop = args.get("path", ""), args.get("property", "")
                if not path or not prop or len(prop) > 31 or any(ord(c) < 32 or ord(c) > 126 for c in path + prop):
                    raise MacError("Registry path/property must be bounded printable ASCII")
                result = self.rpc("registry_probe", path=path, fork=prop)
            else:
                selector = args.get("selector", "")
                if not re.fullmatch(r"0x[0-9a-fA-F]{8}", selector) and (len(selector) != 4 or any(ord(c) < 32 or ord(c) > 126 for c in selector)):
                    raise MacError("Selector must be four printable ASCII bytes (spaces count)")
                result = self.rpc("system_probe", path=selector, fork=args["kind"])
        elif short == "get_process_usage":
            before = self.rpc("processes")
            # Sleep on the host, leaving the native quiet watchdog unchanged.
            # MacTools.rpc retains this transaction's connection identity.
            time.sleep(args.get("seconds", 1))
            result = process_usage_delta(before, self.rpc("processes"))
            result["requested_seconds"] = args.get("seconds", 1)
        elif short == "process_info": result = self.rpc("process_info", offset=args["high"], count=args["low"])
        elif short in ("read_process_memory", "find_process_memory"):
            pattern = None
            if short == "find_process_memory":
                pattern = base64.b64decode(args["pattern"], validate=True) if args.get("encoding") == "base64" else args["pattern"].encode("mac_roman")
                if not pattern or len(pattern) > min(1024,args.get("count",256)): raise ValueError("Pattern must be 1..1024 bytes and fit the search window")
            data, meta = self.process_memory(args)
            offset = args.get("offset",0); address = meta["base_address"]+offset
            result = {"name": meta["name"], "base_address": meta["base_address"], "launch_ticks": meta["launch_ticks"],
                      "offset": offset, "address": address, "bytes": len(data), "partition_bytes": meta["partition_bytes"],
                      "next_offset": offset+len(data) if offset+len(data) < meta["partition_bytes"] else None}
            if pattern is None:
                result.update(content=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), hexdump=hexdump(data,address))
            else:
                matches=[]; cursor=0
                while True:
                    at=data.find(pattern,cursor)
                    if at < 0: break
                    if len(matches)==64: break
                    matches.append({"offset":offset+at,"address":address+at}); cursor=at+1
                result["matches"]=matches; result["truncated"]=at>=0
                if at>=0: result["next_offset"]=offset+at
                elif result["next_offset"] is not None: result["next_offset"] -= len(pattern)-1
        elif short == "read_file":
            offset = args.get("offset", 0); data, total = self.read(args["path"], args.get("fork", "data"), offset, args.get("limit", 16384))
            result = {"content": base64.b64encode(data).decode() if args.get("encoding") == "base64" else data.decode("mac_roman").replace("\r", "\n"),
                      "bytes": len(data), "total": total, "sha256": hashlib.sha256(data).hexdigest(), "next_offset": offset+len(data) if offset+len(data) < total else None}
        elif short == "write_file":
            data = base64.b64decode(args["content"], validate=True) if args.get("encoding") == "base64" else native_text(args["content"])
            result = self.write(args["path"], data, args.get("fork", "data"), args.get("expected_sha256"), args.get("create_only", False))
        elif short == "replace_in_file":
            data, total = self.read(args["path"])
            if total > len(data): raise MacError("File exceeds 16 MiB")
            digest = hashlib.sha256(data).hexdigest()
            if args.get("expected_sha256") and args["expected_sha256"] != digest: raise MacError("The file changed; read it again before editing")
            old, new = native_text(args["old_text"]), native_text(args["new_text"])
            count = data.count(old) if old else 0
            if not count or count != 1 and not args.get("replace_all"): raise MacError("Expected exactly one text match; found " + str(count))
            result = self.write(args["path"], data.replace(old, new, -1 if args.get("replace_all") else 1), expected=digest)
            result["replacements"] = count if args.get("replace_all") else 1
        elif short == "screenshot":
            return self.screenshot()
        else: raise ValueError("Unknown tool")
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result}
