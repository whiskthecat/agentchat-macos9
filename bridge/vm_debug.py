"""Out-of-band QEMU inspection. Explicitly enabled with AGENT_QMP_SOCKET.

This adapter works while the guest and its TCP service are halted. It exposes
bounded inspection and stop/continue, never a general monitor or host shell.
Whole-machine pause leases automatically resume only pauses owned here.
"""
import atexit
import base64
import hashlib
import json
import os
import re
import socket
import threading
import time
import uuid


class QMP:
    def __init__(self, path):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        try:
            self.socket.connect(path)
            self.stream = self.socket.makefile("rwb")
            self.sequence = 0
            if "QMP" not in self.read(): raise RuntimeError("Unexpected QMP greeting")
            self.command("qmp_capabilities")
        except Exception:
            self.close()
            raise

    def read(self):
        line = self.stream.readline(2 * 1024 * 1024 + 1)
        if not line or len(line) > 2 * 1024 * 1024:
            raise RuntimeError("Invalid QMP response")
        return json.loads(line)

    def command(self, name, arguments=None):
        self.sequence += 1
        request = {"execute": name, "id": self.sequence}
        if arguments is not None: request["arguments"] = arguments
        self.stream.write(json.dumps(request).encode() + b"\n"); self.stream.flush()
        while True:
            reply = self.read()
            if reply.get("id") != self.sequence: continue
            if "error" in reply: raise RuntimeError(reply["error"].get("desc", "QMP operation failed"))
            return reply["return"]

    def close(self):
        if getattr(self, "stream", None): self.stream.close()
        self.socket.close()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()


def hexdump(data, address):
    rows = []
    for offset in range(0, len(data), 16):
        chunk = data[offset:offset+16]
        printable = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append("%08x  %-47s  %s" % (address+offset, chunk.hex(" "), printable))
    return "\n".join(rows)


def register_snapshot(q):
    raw = q.command("human-monitor-command", {"command-line": "info registers"})
    registers = {name.lower(): "0x"+value for name,value in re.findall(
        r"\b(NIP|LR|CTR|XER|MSR|CR|SRR0|SRR1|SDR1)\s+([0-9a-fA-F]+)", raw)}
    for row, values in re.findall(r"GPR(\d+)\s+([^\r\n]+)", raw):
        for i, value in enumerate(values.split()): registers["r"+str(int(row)+i)] = "0x"+value
    msr = int(registers["msr"], 16) if "msr" in registers else None
    return {"architecture": "PowerPC", "scope": "current CPU context", "registers": registers,
            "data_address_translation": bool(msr & 0x10) if msr is not None else None,
            "instruction_address_translation": bool(msr & 0x20) if msr is not None else None, "raw": raw}


_KEYS = set("abcdefghijklmnopqrstuvwxyz0123456789") | {
    "ret", "esc", "spc", "backspace", "delete", "tab", "up", "down", "left", "right",
    "home", "end", "pgup", "pgdn", "minus", "equal", "bracket_left", "bracket_right",
    "semicolon", "apostrophe", "comma", "dot", "slash", "backslash", "grave"}
_MODIFIERS = {"cmd": "meta_l", "shift": "shift", "option": "alt", "control": "ctrl"}
_PUNCT = dict(zip(" .,/;'[]-=\\`", ["spc", "dot", "comma", "slash", "semicolon", "apostrophe", "bracket_left", "bracket_right", "minus", "equal", "backslash", "grave"]))
_SHIFTED = dict(zip('!@#$%^&*()_+{}:"<>?|~', ["1","2","3","4","5","6","7","8","9","0","minus","equal","bracket_left","bracket_right","semicolon","apostrophe","comma","dot","slash","backslash","grave"]))

def desktop_input(q, args):
    action = args["action"]
    mods = [m for m in args.get("modifiers", "").split(",") if m]
    if len(mods) != len(set(mods)) or any(m not in _MODIFIERS for m in mods): raise ValueError("Invalid modifiers")
    keys = []
    if action == "key":
        if args.get("key") not in _KEYS: raise ValueError("Unsupported key")
        keys = [(args["key"], False)]
    elif action == "text":
        if mods: raise ValueError("Text does not accept held modifiers")
        text = args.get("text", "")
        if not text or len(text) > 512: raise ValueError("Text must be 1..512 ASCII characters")
        for c in text:
            if c.isascii() and c.isalnum(): keys.append((c.lower(), c.isupper()))
            elif c in _PUNCT: keys.append((_PUNCT[c], False))
            elif c in _SHIFTED: keys.append((_SHIFTED[c], True))
            elif c in "\r\n\t": keys.append(("tab" if c == "\t" else "ret", False))
            else: raise ValueError("Unsupported input character")
    elif action in ("click", "double_click", "drag"):
        w, h = args.get("width",800), args.get("height",600)
        for x,y in [(args.get("x"),args.get("y"))] + ([(args.get("end_x"),args.get("end_y"))] if action == "drag" else []):
            if x is None or y is None or not (0 <= x < w and 0 <= y < h): raise ValueError("Pointer outside screen bounds")
    else: raise ValueError("Unsupported input action")
    held = set(); button = False
    def key(code, down):
        if down: held.add(code)
        q.command("input-send-event", {"events": [{"type":"key", "data":{"down":down,"key":{"type":"qcode","data":code}}}]})
        if not down: held.discard(code)
    def pointer(x,y):
        q.command("input-send-event", {"events": [{"type":"abs", "data":{"axis":"x","value":round(x*32767/(w-1))}}, {"type":"abs", "data":{"axis":"y","value":round(y*32767/(h-1))}}]})
    def btn(down):
        nonlocal button
        if down: button = True
        q.command("input-send-event", {"events":[{"type":"btn","data":{"button":"left","down":down}}]})
        if not down: button = False
    try:
        for m in mods: key(_MODIFIERS[m], True)
        for code, shifted in keys:
            if shifted: key("shift",True)
            key(code,True); time.sleep(.04); key(code,False)
            if shifted: key("shift",False)
            time.sleep(.03)
        if action in ("click", "double_click", "drag"):
            pointer(args["x"],args["y"]); time.sleep(.06); btn(True); time.sleep(.06)
            if action == "drag":
                for step in range(1,13):
                    pointer(round(args["x"]+(args["end_x"]-args["x"])*step/12),round(args["y"]+(args["end_y"]-args["y"])*step/12)); time.sleep(.04)
            btn(False)
            if action == "double_click": time.sleep(.07); btn(True); time.sleep(.06); btn(False)
        return {"submitted":True, "action":action, "scope":"QEMU desktop", "keys":len(keys)}
    finally:
        if button:
            try: btn(False)
            except Exception: pass
        for code in list(held):
            try: key(code,False)
            except Exception: pass


class VmDebug:
    def __init__(self, path=None):
        self.path = path if path is not None else os.environ.get("AGENT_QMP_SOCKET", "")
        self.lock = threading.RLock()
        self.lease = None
        self.deadline = 0
        self.last_error = None
        atexit.register(self.close)

    def close(self):
        with self.lock:
            if self.lease: self._expire(self.lease)

    def _expire(self, lease):
        with self.lock:
            if self.lease != lease: return
            try:
                with QMP(self.path) as q:
                    status = q.command("query-status")
                    if status["status"] == "paused": q.command("cont")
                self.lease = None
            except Exception as e:
                self.last_error = "Automatic resume failed: " + str(e)

    def call(self, action, args):
        if not self.path:
            raise RuntimeError("QEMU debugging is disabled. Set AGENT_QMP_SOCKET on the VM relay. These tools do not control a physical G3.")
        with self.lock, QMP(self.path) as q:
            status = q.command("query-status")
            if action == "vm_status":
                return {**status, "scope": "whole QEMU Macintosh", "pause_lease": self.lease,
                        "lease_seconds_remaining": max(0, round(self.deadline-time.monotonic(), 2)) if self.lease else 0,
                        "last_error": self.last_error}
            if action == "vm_pause":
                seconds = args.get("seconds", 30)
                if status["status"] != "running" and not (status["status"] == "paused" and self.lease):
                    raise RuntimeError("VM is already stopped outside this adapter; existing debugger state was preserved.")
                if status["running"]: q.command("stop")
                lease = self.lease = uuid.uuid4().hex
                self.deadline = time.monotonic() + seconds; self.last_error = None
                timer = threading.Timer(seconds, self._expire, (lease,)); timer.daemon = True; timer.start()
                return {"paused": True, "scope": "whole QEMU Macintosh", "pause_lease": lease, "auto_resume_seconds": seconds}
            if action == "vm_resume":
                if args.get("pause_lease") and args["pause_lease"] != self.lease:
                    raise RuntimeError("Stale pause lease; a newer pause was preserved.")
                if status["status"] == "paused": q.command("cont")
                elif not status["running"]: raise RuntimeError("VM cannot continue from state " + status["status"])
                self.lease = None; self.last_error = None
                return {"running": True, "scope": "whole QEMU Macintosh"}
            if action == "vm_input":
                if not status["running"]: raise RuntimeError("Resume the VM before sending input")
                return desktop_input(q, args)
            if status["status"] != "paused":
                raise RuntimeError("Pause the VM before inspecting CPU registers or VM memory.")
            if action == "vm_registers":
                return register_snapshot(q)
            if action == "vm_read_memory":
                address, count = args["address"], args.get("count", 256)
                if address+count > 0x100000000: raise ValueError("Memory range wraps the 32-bit address space")
                space = args.get("space", "virtual")
                context = register_snapshot(q)
                command = ("xp" if space == "physical" else "x") + "/%dxb 0x%x" % (count, address)
                raw = q.command("human-monitor-command", {"command-line": command})
                values = []
                for line in raw.splitlines():
                    match = re.fullmatch(r"[0-9a-fA-F]+:\s*((?:0x[0-9a-fA-F]{2}\s*)+)", line)
                    if match: values.extend(int(x,16) for x in re.findall(r"0x([0-9a-fA-F]{2})", match[1]))
                if len(values) != count:
                    detail = ""
                    if space == "virtual":
                        detail = " Virtual addresses use the paused CPU's current mapping (MSR %s)." % context["registers"].get("msr", "unknown")
                        if context["data_address_translation"] is False:
                            detail += " Data translation is disabled in this CPU context; use physical memory or resume and pause in a translated context."
                    raise RuntimeError("QEMU could not read the complete requested memory range: " + raw[:200] + detail)
                data = bytes(values)
                return {"address": address, "bytes": count, "space": space,
                        "cpu_msr": context["registers"].get("msr"), "data_address_translation": context["data_address_translation"],
                        "content": base64.b64encode(data).decode(),
                        "sha256": hashlib.sha256(data).hexdigest(), "hexdump": hexdump(data, address)}
            raise ValueError("Unknown VM operation")


VM_DEBUG = VmDebug()
