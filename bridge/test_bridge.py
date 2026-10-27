#!/usr/bin/env python3
"""Stub verification for hardened agentd. Starts fake upstream + bridge on
test ports, exercises: probes rejected, lazy session, busy refusal, junk
ignored, mid-turn abort, resume-same-session, capacity cap. PASS/FAIL per
check; exits nonzero on failure."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

BR = "127.0.0.1", 18865
STUB = "http://127.0.0.1:18888/state"
HERE = str(Path(__file__).resolve().parent)
fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("  " + extra if extra else ""))
    if not cond:
        fails.append(name)


def state():
    return json.loads(urllib.request.urlopen(STUB, timeout=5).read())


class Client:
    """Line reader that KEEPS all history so multiple expects can't consume
    each other's lines."""

    def __init__(self, src_ip=None):
        self.s = socket.create_connection(BR, timeout=5)
        self.s.settimeout(0.3)
        self.buf = b""
        self.history = []

    def sendline(self, line):
        self.s.sendall((line + "\n").encode())

    def pump(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                d = self.s.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not d:
                break
            self.buf += d
            while b"\n" in self.buf:
                l, _, self.buf = self.buf.partition(b"\n")
                self.history.append(l.decode("mac_roman", "replace"))
        return self.history

    def expect(self, substr, timeout=5.0):
        return self.expect_after(substr, 0, timeout)

    def expect_after(self, substr, from_idx, timeout=5.0):
        end = time.monotonic() + timeout
        while True:
            for ln in self.history[from_idx:]:
                if substr in ln:
                    return ln
            if time.monotonic() >= end:
                return None
            self.pump(0.3)

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


def bind_client(src):
    s = socket.socket()
    s.settimeout(5)
    s.bind((src, 0))
    s.connect(BR)
    s.settimeout(0.1)
    c = Client.__new__(Client)
    c.s, c.buf, c.history = s, b"", []
    return c


proc_stub = subprocess.Popen(
    [sys.executable, "-c", "from stub_opencode import H, ThreadingHTTPServer; "
     "ThreadingHTTPServer(('127.0.0.1',18888), H).serve_forever()"], cwd=HERE)
time.sleep(0.5)
logfile = tempfile.NamedTemporaryFile(prefix="agentchat-bridge-", suffix=".log", delete=False)
proc_bridge = subprocess.Popen(
    [sys.executable, HERE + "/agentd.py"],
    env={**os.environ, "OPENCODE_URL": "http://127.0.0.1:18888", "CHAT_PORT": "18865",
         "ADMIN_PORT": "18866", "AGENT_MODEL": "strix/halogen-qwen3.8-flash-next",
         "AGENT_TEST_MODE": "1", "CODEX_ENABLED": "0",
         "AGENT_STATE_FILE": logfile.name + ".sqlite3",
         "PATH": "/usr/bin:/bin"},
    stdout=logfile, stderr=subprocess.STDOUT)
time.sleep(0.8)

try:
    # 1. curl-style probe: must NOT create any session
    c = Client()
    hello = c.expect("S:", timeout=3)
    c.sendline("GET /ping HTTP/1.1")
    rej = c.expect("X:", timeout=2)
    c.sendline("Host: 127.0.0.1:8765")
    c.sendline("User-Agent: curl/7.81.0")
    c.close()
    time.sleep(0.8)
    check("probe rejected with X:", rej and "probe" in rej, repr(rej))
    check("probe created NO session", state()["sessions"] == 0, str(state()))

    # 2. legit P: prompt -> session created lazily, streaming, D:
    a = bind_client("127.0.0.1")
    a.expect("S:", timeout=3)
    a.sendline("P:hello")
    d1 = a.expect("T:stub-reply-to:hello", timeout=4)
    fin = a.expect("D:", timeout=4)
    st = state()
    check("P: creates exactly one session", st["sessions"] == 1, str(st["sessions"]))
    check("streams reply", d1 is not None)
    check("turn completes with D:", fin is not None)
    check("prompt recorded", len(st["prompts"]) == 1 and st["prompts"][0]["text"] == "hello")
    sid1 = st["prompts"][0]["sid"]

    # 3. second prompt while busy -> X:busy, not submitted
    idx = len(a.history)
    a.sendline("P:one")
    time.sleep(0.7)          # past rate limit, turn still open (~1.7s)
    a.sendline("P:two")
    busy = a.expect_after("X:busy", idx, timeout=2)
    fin2 = a.expect_after("D:", idx, timeout=4)
    st = state()
    check("busy refused", busy is not None, repr(busy))
    check("busy prompt NOT submitted", len(st["prompts"]) == 2,
          str(st["prompts"]))
    check("first turn still completes", fin2 is not None)

    # 4. junk line ignored entirely
    a.sendline("just some random text no prefix")
    time.sleep(0.8)
    st = state()
    check("junk creates nothing", len(st["prompts"]) == 2 and st["sessions"] == 1,
          str(st["prompts"]))

    # 5. disconnect mid-turn -> upstream aborted
    idx = len(a.history)
    a.sendline("P:long")
    a.expect_after("T:stub-reply-to:long", idx, timeout=4)
    a.close()
    time.sleep(1.2)
    st = state()
    check("mid-turn abort sent", st["aborts"] == [sid1], str(st["aborts"]))

    # 6. reconnect resumes SAME session
    a2 = bind_client("127.0.0.1")
    # The connection greeting is S:connected. Session identity is announced
    # on the first prompt, when the bridge actually uses the retained session.
    a2.expect("S:connected", timeout=3)
    a2.sendline("P:again")
    res = a2.expect("S:resumed " + sid1, timeout=3)
    check("reconnect resumes same session", res is not None, repr(res))
    rep = a2.expect("T:stub-reply-to:again", timeout=4)
    a2.expect("D:", timeout=4)
    st = state()
    check("resumed prompt goes to same session",
          len(st["prompts"]) == 4 and st["prompts"][-1]["sid"] == sid1
          and st["prompts"][-1]["text"] == "again",
          str(st["prompts"]))
    check("still only one session", st["sessions"] == 1)

    # 7. capacity: 2nd host allowed, 3rd refused
    b = bind_client("127.0.0.2")
    b.expect("S:", timeout=3)
    b.sendline("P:b1")
    b.expect("T:", timeout=4)
    st = state()
    check("second host gets own session", st["sessions"] == 2, str(st["sessions"]))
    c3 = bind_client("127.0.0.3")
    ref = c3.expect("X:", timeout=2)
    c3.close()
    check("third host refused (MAX_SESSIONS=2)",
          ref is not None and "busy" in ref, repr(ref))
    check("third host created nothing", state()["sessions"] == 2)
    # after B disconnects, slot frees -> 127.0.0.3 can get in
    b.close()
    time.sleep(0.8)
    c4 = bind_client("127.0.0.3")
    ok = c4.expect("S:", timeout=3)
    c4.sendline("P:c4")
    got = c4.expect("T:", timeout=4)
    check("slot frees after disconnect", ok is not None and got is not None)
    c4.close()
    time.sleep(0.5)

    # History/settings controls never submit prompts or start inference.
    before = state()
    idx = len(a2.history)
    a2.sendline("L:")
    listed = a2.expect_after("J:END", idx)
    check("history lists existing conversations", listed is not None and
          any(x.startswith("J:ITEM\t" + sid1 + "\t") for x in a2.history[idx:]))
    check("listing creates no sessions or prompts", state() == before)
    idx = len(a2.history)
    a2.sendline("N:")
    a2.expect_after("V:END", idx)
    check("New Chat is lazy", state() == before)
    idx = len(a2.history)
    a2.sendline("O:" + sid1)
    restored = a2.expect_after("V:END", idx)
    check("history restores user and assistant text", restored is not None and
          "V:TEXT\thello" in a2.history[idx:] and "V:TEXT\tstub-reply-to:hello" in a2.history[idx:])
    check("history replay creates no inference", state() == before)
    begin = next((line for line in a2.history[idx:] if line.startswith("V:BEGIN\t" + sid1 + "\t")), "")
    sample = begin.rsplit("\t", 1)[-1]
    check("history includes a server clock sample", sample.isdigit() and abs(int(sample) - time.time()) < 10)
    stamps = [line.split("\t")[2] for line in a2.history[idx:] if line.startswith("V:MESSAGE\t")]
    check("history supplies timestamps for native local-time formatting", stamps and
          all(stamp.isdigit() and 0 < int(stamp) <= time.time() for stamp in stamps))
    check("history identifies the speaker", all(len(line.split("\t")) == 4 and line.split("\t")[3]
          for line in a2.history[idx:] if line.startswith("V:MESSAGE\t")))
    idx = len(a2.history)
    a2.sendline("R:Renamed G3 conversation")
    a2.expect_after("J:END", idx)
    check("rename updates upstream title", any(x == "J:ITEM\t" + sid1 + "\tRenamed G3 conversation"
          for x in a2.history[idx:]))
    idx = len(a2.history)
    a2.sendline("O:../../bad")
    check("invalid history ID rejected", a2.expect_after("V:ERROR", idx) is not None)
    idx = len(a2.history)
    # Keep the fake turn open until Abort, rather than racing its 1.5s idle.
    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:18888/test/hold_next_turn", data=b"{}", method="POST"), timeout=3).close()
    a2.sendline("P:stop test")
    a2.expect_after("T:stub-reply-to:stop test", idx)
    a2.sendline("N:")
    check("cannot switch a busy conversation", a2.expect_after("V:ERROR\tWait", idx) is not None)
    before_delete = state()
    delete_idx = len(a2.history)
    a2.sendline("Z:" + sid1)
    check("cannot delete a busy conversation",
          a2.expect_after("V:ERROR", delete_idx) is not None and state() == before_delete)
    a2.sendline("A:")
    check("explicit Stop aborts upstream", a2.expect_after("S:reply stopped", idx) is not None and
          state()["aborts"][-1] == sid1)
    idx = len(a2.history)
    time.sleep(0.6)
    a2.sendline("P:after stop")
    check("can send after Stop", a2.expect_after("T:stub-reply-to:after stop", idx) is not None)
    a2.expect_after("D:", idx)
    idx = len(a2.history)
    a2.sendline("N:"); a2.expect_after("V:END", idx)
    time.sleep(0.6)
    idx = len(a2.history)
    a2.sendline("P:second real conversation")
    a2.expect_after("D:", idx)
    sid2 = state()["prompts"][-1]["sid"]
    check("New Chat prompt uses distinct session", sid2 != sid1)
    idx = len(a2.history)
    a2.sendline("O:" + sid1); a2.expect_after("V:END", idx)
    a2.sendline("P:continue original")
    a2.expect_after("D:", idx)
    check("selected history retains its upstream context", state()["prompts"][-1]["sid"] == sid1)

    before = state()
    idx = len(a2.history)
    a2.sendline("Z:" + sid2)
    check("delete rejects a conversation that is not selected",
          a2.expect_after("V:ERROR", idx) is not None and state() == before)
    idx = len(a2.history)
    a2.sendline("Z:../../bad")
    check("delete rejects an invalid ID without touching upstream",
          a2.expect_after("V:ERROR", idx) is not None and state() == before)
    before = state()
    idx = len(a2.history)
    a2.sendline("Z:" + sid1)
    a2.expect_after("J:END", idx)
    after = state()
    check("delete removes exactly the selected conversation",
          after["deletes"] == before["deletes"] + [sid1] and
          not any(line.startswith("J:ITEM\t" + sid1 + "\t") for line in a2.history[idx:]) and
          any(line.startswith("J:ITEM\t" + sid2 + "\t") for line in a2.history[idx:]))
    check("delete acknowledges then opens a blank lazy conversation",
          "Z:" + sid1 in a2.history[idx:] and "V:END" in a2.history[idx:] and "K:" in a2.history[idx:])
    check("delete sends no prompt and creates no session",
          after["prompts"] == before["prompts"] and after["sessions"] == before["sessions"])
    idx = len(a2.history)
    a2.sendline("O:" + sid1)
    check("deleted history cannot be opened again", a2.expect_after("V:ERROR", idx) is not None)
finally:
    proc_bridge.terminate()
    proc_stub.terminate()
    proc_bridge.wait(timeout=5)
    proc_stub.wait(timeout=5)
    logfile.close()

print("RESULT:", "ALL PASS" if not fails else "FAILURES: %s" % fails)
print("Bridge log:", logfile.name)
sys.exit(1 if fails else 0)
