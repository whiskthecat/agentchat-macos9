#!/usr/bin/env python3
"""
agentd — LAN bridge between the iMac G3 (Mac OS 9) chat app and a coding agent.

Targets the standard opencode server REST + SSE API, so vanilla opencode and
compatible forks both work.

HARDENING (after the 2026-09-27 incident: an operator agent probing this port
with curl created ~700 sessions because every TCP accept POSTed a new session
and every inbound line was submitted as a prompt, which the model then answered
by probing the port again — a self-sustaining agent feedback loop).

Rules enforced here:
  1. A session is created LAZILY — only on the first valid `P:` prompt.
     Connecting alone never touches the upstream.
  2. Only `P:`-prefixed lines are prompts. HTTP-shaped lines
     (GET/POST/... or HTTP/) are rejected and logged as probes.
  3. At most MAX_SESSIONS (default 2) live opencode sessions from this bridge;
     reconnects RESUME the client's existing session (keyed by client IP)
     instead of creating duplicates.
  4. At most ONE in-flight prompt per session; a second prompt while busy is
     refused with X:busy (no queueing, no duplication).
  5. A client disconnect mid-turn ABORTS the upstream run, except a pending
     human question stays recoverable. SSE loss never causes a resubmit.
  6. Basic rate limiting per session.

iMac <-> bridge wire protocol (newline-delimited, MacRoman payloads;
newlines escaped as \\n, backslashes as \\\\):

    app -> bridge:   P:<user prompt line>     chat prompt (only trigger)
                     B64M:<w>:<h>:<stride>    screenshot meta (C2)
                     B64C:<base64>            screenshot chunk
                     B64E                     end of screenshot
    bridge -> app:   T:<text delta>           assistant output
                     S:<status>               progress (tool use / session info)
                     D:                       turn complete
                     X:<error>                failure
                     C:<verb>                 control command from operator

Operator console: connect to port 8766 (localhost) and type:
    screen              -> ask the iMac for a screenshot (saved to ./shots/)
    update <filename>   -> tell the iMac to self-update from the file server
    quit                -> quit the iMac app
"""

import base64
import collections
import functools
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import signal
import sys
import threading
import time
import urllib.request
import uuid

from codex_backend import CodexBackend
from contact_store import ContactStore
from remote_mac import MacBroker, MacTools
from vm_debug import VM_DEBUG
from chat_images import ImageStore, Upload
from tool_questions import ToolQuestions
from mac_context import MCP_TOOL_TIMEOUT_SEC, session_instructions

SERVER = os.environ.get("OPENCODE_URL", "http://127.0.0.1:7777")
OPENCODE_ENABLED = os.environ.get("OPENCODE_ENABLED", "1") == "1"
# The contact a new connection starts with. Unset: the first discovered model, preferring
# OpenCode's free models, which work without any sign-in.
CONFIGURED_MODEL = os.environ.get("AGENT_MODEL", "")
TEST_MODE = os.environ.get("AGENT_TEST_MODE", "0") == "1"
CODEX_ENABLED = os.environ.get("CODEX_ENABLED", "1") == "1"
CODEX = CodexBackend([sys.executable, str(Path(__file__).with_name("stub_codex.py"))] if TEST_MODE else None)
STORE = ContactStore(os.environ.get("AGENT_STATE_FILE", str(Path(__file__).with_name("contacts.sqlite3"))))
IMAGES = ImageStore(os.environ.get("AGENT_STATE_FILE", str(Path(__file__).with_name("contacts.sqlite3"))))
MODELS = {}
MODEL_LOCK = threading.Lock()
CHAT_PORT = int(os.environ.get("CHAT_PORT", "8765"))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", "8766"))
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS", "2"))
TCP_IDLE_SEC = 15
TCP_PROBE_SEC = 5
TCP_PROBES = 3
TCP_TIMEOUT_MS = 30000
HEARTBEAT_SEC = 15
RESUME_WINDOW = float(os.environ.get("RESUME_WINDOW", "600"))  # keep dropped session 10 min
MIN_PROMPT_GAP = 0.5        # seconds between prompts per session
MAX_PROMPTS_PER_MIN = 8    # per session
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agentd.log")
SHOTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shots")

SHOT = {"buf": "", "chunks": 0, "w": 0, "h": 0, "stride": 0}
MAC = MacBroker()
MCP_LOCK = threading.Lock()
MCP_INSTALLED = False


def ensure_imac_mcp():
    global MCP_INSTALLED
    if os.environ.get("IMAC_TOOLS_ENABLED", "1") != "1": return
    with MCP_LOCK:
        if MCP_INSTALLED: return
        name = "imac_vmtest" if TEST_MODE else "imac"
        config = {"type": "local", "enabled": True, "timeout": MCP_TOOL_TIMEOUT_SEC * 1000,
                  "command": [sys.executable, str(Path(__file__).with_name("mac_mcp.py").resolve()),
                              "--admin-port", str(ADMIN_PORT)]}
        status = api("POST", "/mcp", {"name": name, "config": config}, timeout=15)
        entry = status.get(name, {}) if isinstance(status, dict) else {}
        if entry.get("status") not in ("connected", "ready"):
            raise RuntimeError("Macintosh MCP tools could not connect: " + str(entry))
        MCP_INSTALLED = True
        log("Macintosh tools registered with opencode: " + name)

HTTP_SHAPED = re.compile(
    r"^\s*(GET|POST|PUT|HEAD|DELETE|OPTIONS|TRACE|CONNECT)\s+\S+|^\s*HTTP/\d")

_log_lock = threading.Lock()


def log(msg):
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S"), msg)
    with _log_lock:
        print(line, flush=True)
        try:
            with open(LOG_PATH, "a") as f:
                f.write(line + "\n")
        except Exception:
            pass


def api(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SERVER + path, data=data,
                                headers={"content-type": "application/json"},
                                method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status in (204, 202):
            return None
        return json.load(r)


def esc(s):
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "")


def unesc(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            out.append("\n" if nxt == "n" else "\\" if nxt == "\\" else nxt)
            i += 2
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def chat_session(info):
    """Only expose conversations created for this app, including legacy ones."""
    title = info.get("title", "")
    return (title.startswith("AgentChat: ") or title == "iMac G3 chat") and not info.get("parentID")


def session_catalog():
    items = [s for s in api("GET", "/session?limit=1000") if chat_session(s)]
    return sorted(items, key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)[:100]


def model_catalog():
    """Discover configured text models without creating sessions or turns."""
    discovered = []
    errors = []
    if OPENCODE_ENABLED:
        legacy_catalog = True
        try:
            providers = api("GET", "/provider", timeout=5)
            legacy_catalog = "connected" not in providers
            configured = os.environ.get("OPENCODE_MODEL", CONFIGURED_MODEL)
            for provider in providers.get("all", []):
                if provider["id"] not in providers.get("connected", []):
                    continue
                text_models = {mid: entry for mid, entry in provider.get("models", {}).items()
                               if entry.get("capabilities", {}).get("output", {}).get("text", True)}
                candidates = [configured.split("/", 1)[1] if configured.startswith(provider["id"] + "/") else "",
                              providers.get("default", {}).get(provider["id"], "")]
                agent_models = {mid: entry for mid, entry in text_models.items()
                                if entry.get("capabilities", {}).get("toolcall", True)}
                suggested = next((mid for mid in candidates if mid in agent_models), next(iter(agent_models), ""))
                for mid, entry in text_models.items():
                    if entry.get("capabilities", {}).get("output", {}).get("text", True):
                        name = entry.get("name", mid)
                        cost = entry.get("cost") or {}
                        free = provider["id"] == "opencode" and not cost.get("input") and not cost.get("output")
                        discovered.append({"id": provider["id"] + "/" + mid, "name": name, "backend": "opencode",
                                           "source": "OpenCode / " + provider["id"], "recommended": mid == suggested or free,
                                           "free": free and mid in agent_models, "default": mid == suggested,
                                           "images": entry.get("capabilities", {}).get("input", {}).get("image", False)})
        except Exception as e:
            errors.append("opencode models: " + str(e))
        # Older upstreams may not expose /provider. Codex must be discovered
        # through its own catalog instead of being mislabeled as opencode.
        if legacy_catalog and CONFIGURED_MODEL and not CONFIGURED_MODEL.startswith("codex/") \
                and not any(m["id"] == CONFIGURED_MODEL for m in discovered):
            discovered.append({"id": CONFIGURED_MODEL, "name": CONFIGURED_MODEL.split("/", 1)[-1],
                               "backend": "opencode", "source": "OpenCode", "recommended": True})
    if CODEX_ENABLED:
        try:
            codex_models = CODEX.models()
            if codex_models and not any(m.get("recommended") for m in codex_models):
                codex_models[0]["recommended"] = True
            for m in codex_models:
                m["default"] = bool(m.get("recommended"))
            discovered += codex_models
        except Exception as e:
            errors.append("Codex models: " + str(e))
    # Configured model, then free OpenCode models (OpenCode's own default first), then
    # each service's recommendation.
    discovered.sort(key=lambda m: (m["id"] != CONFIGURED_MODEL, not m.get("free"), not m.get("default"),
                                   not m.get("recommended"), m["name"].casefold(), m["id"]))
    with MODEL_LOCK:
        MODELS.update({m["id"]: m for m in discovered})
    for error in errors:
        log(error)
    return discovered[:256]


def default_model():
    """The configured model if available, otherwise the first discovered one."""
    with MODEL_LOCK:
        known = dict(MODELS)
    if CONFIGURED_MODEL and CONFIGURED_MODEL in known:
        return CONFIGURED_MODEL
    try:
        catalog = model_catalog()
    except Exception as e:
        log("model discovery failed: " + str(e))
        catalog = []
    if CONFIGURED_MODEL and any(m["id"] == CONFIGURED_MODEL for m in catalog):
        return CONFIGURED_MODEL
    return catalog[0]["id"] if catalog else CONFIGURED_MODEL


def model_info(mid):
    with MODEL_LOCK:
        model = MODELS.get(mid)
    if model is None:
        model_catalog()
        with MODEL_LOCK:
            model = MODELS.get(mid)
    if model is None:
        raise ValueError("that model is not available on this relay")
    return model


def speaker(info, fallback):
    if info.get("speaker"):
        return info["speaker"]
    mid = "/".join([info.get("providerID", ""), info.get("modelID", "")])
    with MODEL_LOCK:
        return MODELS.get(mid, {}).get("name", fallback)


def serialized_view(method):
    """Keep a streamed picture in its original chat while selection/events run."""
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self.view_lock: return method(self, *args, **kwargs)
    return locked


class Session:
    """One resumable chat session, keyed by client IP. Owns an opencode session
    (created lazily) plus an SSE feeder while a client is attached."""

    def __init__(self, key, send):
        self.key = key
        self.view_lock = threading.RLock()
        self.shown_images = set()
        self.send = send
        self.sid = None
        self.assistant_msg = None
        self.part_types = {}
        self.inflight = False
        self.stopped = False
        self.alive = True
        self.last_prompt_ts = 0.0
        self.recent = collections.deque()  # prompt timestamps this window
        self.sse_thread = None
        self.disconnected_at = None
        self.model = default_model()
        self.codex_thread = None
        self.codex_turn = None
        self.codex_parts = {}
        self.connection_id = None
        self.handler_active = False
        self.retired_codex_turns = collections.deque(maxlen=64)
        self.questions = ToolQuestions(self, api)
        self.questions_supported = False

    # ---- upstream lifecycle -------------------------------------------
    def _lazy_create(self, text):
        if self.sid is None:
            title = " ".join(text.split())[:100] or "New conversation"
            if self.model.startswith("codex/"):
                self.codex_thread = CODEX.thread(self.model.split("/", 1)[1])
                self.sid = "ses_codex_" + uuid.uuid4().hex
                STORE.save(self.sid, self.model, "AgentChat: " + title, self.codex_thread)
            else:
                self.sid = api("POST", "/session", {"title": "AgentChat: " + title}).get("id")
                STORE.save(self.sid, self.model, "AgentChat: " + title)
            STORE.remember(self.key, self.model, self.sid)
            log("session %s created for %s" % (self.sid, self.key))
            if not self.model.startswith("codex/"):
                self._start_sse()
            self.send("K:" + self.sid)
            try:
                self.catalog()
            except Exception as e:
                self.send("V:ERROR\t" + esc("History unavailable: " + str(e)))
        self.send("S:resumed " + self.sid)

    def catalog(self):
        items = STORE.catalog() if self.model.startswith("codex/") else session_catalog()
        items = [item for item in items if (STORE.get(item["id"]) or {}).get("model", self.model) == self.model]
        items.sort(key=lambda item: item.get("time", {}).get("updated", 0), reverse=True)
        self.send("J:BEGIN")
        for item in items:
            title = item.get("title", "Conversation").removeprefix("AgentChat: ")
            title = " ".join(title.split())[:120]
            self.send("J:ITEM\t%s\t%s" % (item["id"], title))
        self.send("J:END")
        self.send("K:" + (self.sid or ""))

    def models(self, items=None):
        if items is None:
            items = model_catalog()
        choices = STORE.visibility(self.key)
        # Visibility controls the picker, including the selected model. Keep
        # the open conversation/title/drafts; hiding is not a context switch.
        items = [m for m in items if choices.get(m["id"], m.get("recommended", False))]
        self.send("F:BEGIN")
        for item in items:
            self.send("F:ITEM\t%s\t%s" % (item["id"], " ".join(item["name"].split())[:63]))
        self.send("F:END")
        self.send_model()

    def model_settings(self, payload):
        if payload == "LIST":
            items = model_catalog()
            choices = STORE.visibility(self.key)
            self.send("W:BEGIN")
            for model in items:
                self.send("W:ITEM\t%s\t%s\t%s\t%d\t%d" %
                          (model["id"], " ".join(model["name"].split())[:63], model.get("source", model["backend"]),
                           choices.get(model["id"], model.get("recommended", False)), model.get("recommended", False)))
            self.send("W:END")
            # One discovery result for both native lists, including removals.
            self.models(items)
        elif payload.startswith("APPLY\t"):
            fields = payload.split("\t")
            if len(fields) < 2 or fields[1] not in ("0", "1") or (len(fields) - 2) % 2:
                raise ValueError("Invalid model visibility settings")
            choices = {}
            for mid, shown in zip(fields[2::2], fields[3::2]):
                if mid not in MODELS or shown not in ("0", "1"):
                    raise ValueError("Unknown model or visibility value")
                choices[mid] = shown == "1"
            STORE.set_visibility(self.key, choices, reset=fields[1] == "1")
            self.models()
        else:
            raise ValueError("Unknown model settings command")

    def send_model(self):
        info = model_info(self.model)
        self.send("Y:%s\t%s\t%d" % (self.model, " ".join(info["name"].split())[:63], bool(info.get("images"))))

    @serialized_view
    def select_model(self, mid):
        if self.inflight:
            raise ValueError("stop the current reply before changing contacts")
        model_info(mid)  # validate before replacing current context
        old = self.model
        last = STORE.last(self.key, mid)
        self.model = mid
        try:
            self.select(last)
        except Exception:
            self.model = old
            self.send_model()
            raise

    @serialized_view
    def select(self, sid):
        if self.inflight and not (sid == self.sid and self.questions.pending):
            self.send("V:ERROR\tWait for the current reply before switching conversations.")
            return
        if sid:
            if not re.fullmatch(r"ses[A-Za-z0-9_-]{1,100}", sid):
                raise ValueError("invalid session ID")
            saved = STORE.get(sid)
            if sid.startswith("ses_codex_"):
                if not saved or not saved["thread"]:
                    raise ValueError("unknown AgentChat Codex conversation")
                info = {"id": sid, "title": saved["title"]}
                messages = STORE.history(sid)
            else:
                info = api("GET", "/session/" + sid)
                messages = api("GET", "/session/%s/message?limit=200" % sid)
            if not chat_session(info):
                raise ValueError("not an AgentChat conversation")
            self.model = saved["model"] if saved else default_model()
            STORE.remember(self.key, self.model, sid)
        else:
            messages = []  # New Chat stays lazy: no upstream session or prompt.
            STORE.remember(self.key, self.model, "")
        self.image_attachment = None
        self.image_error = None
        self.sid = sid or None
        self.stopped = False
        self.codex_thread = (STORE.get(sid) or {}).get("thread") if sid else None
        self.send_model()
        self.assistant_msg = None
        self.part_types.clear()
        self.shown_images.clear()
        self.questions.clear()
        if self.sid and not self.model.startswith("codex/"):
            self._start_sse()
        self.send("V:BEGIN\t%s\t%d" % (sid, int(time.time())))
        messages += IMAGES.messages(sid)
        messages.sort(key=lambda m: m.get("info", {}).get("time", {}).get("created", 0))
        for message in messages:
            info = message.get("info", {})
            role = info.get("role")
            if role not in ("user", "assistant"):
                continue
            if role == "assistant" and not info.get('id', '').startswith('image_'):
                self.assistant_msg = info.get("id")
                for part in message.get("parts", []):
                    if part.get("id") and part.get("type"):
                        self.part_types[part["id"]] = part["type"]
            text = "\n".join(p.get("text", "") for p in message.get("parts", [])
                             if p.get("type") == "text")
            images = list(message.get("parts", []))
            if role == 'assistant':
                for part in images:
                    try: image_id = IMAGES.import_part(part, self.key)
                    except (ValueError, OSError): image_id = None
                    if image_id: IMAGES.bind(info['id'], sid, image_id)
            images += IMAGES.parts(info.get("id", ""))
            image_ids = list(dict.fromkeys(i for p in images if (i := IMAGES.id_for_part(p))))
            if not text.strip() and not image_ids:
                continue
            created = info.get("time", {}).get("created", 0)
            stamp = str(int(created / 1000)) if created else ""
            self.send("V:MESSAGE\t%d\t%s\t%s" % (1 if role == "user" else 2, stamp,
                                                   "You" if role == "user" else speaker(info, model_info(self.model)["name"])))
            for start in range(0, len(text), 4096):
                self.send("V:TEXT\t" + esc(text[start:start + 4096]))
            for image_id in image_ids:
                IMAGES.show(image_id, self.send)
                self.send("G:ROW\t" + image_id)
                self.shown_images.add((info.get('id'), image_id))
            self.send("V:DONE")
        self.send("V:END")
        self.send("K:" + sid)
        self.catalog()
        self.questions.recover()
        if sid and not self.model.startswith('codex/'):
            try: status = api('GET', '/session/status').get(sid, {})
            except Exception: status = {}
            self._upstream_status(status)

    def _upstream_status(self, status):
        if status.get('type') in ('busy', 'retry') and not self.stopped:
            if not self.inflight:
                self.inflight = True
                self.send('S:thinking...')
        elif status.get('type') == 'idle':
            if self.questions.pending: return
            was_busy = self.inflight
            self.inflight = False
            self.stopped = False
            if was_busy:
                self.questions.clear()
                self.send('D:')

    def rename(self, title):
        if self.inflight or not self.sid:
            raise ValueError("select an idle conversation to rename")
        title = " ".join(title.split())[:100]
        if not title:
            raise ValueError("conversation name is empty")
        if self.model.startswith("codex/"):
            saved = STORE.get(self.sid)
            CODEX.thread(self.model.split("/", 1)[1], saved["thread"])
            CODEX.call("thread/name/set", {"threadId": saved["thread"], "name": title})
            STORE.save(self.sid, self.model, "AgentChat: " + title)
        else:
            api("PATCH", "/session/" + self.sid, {"title": "AgentChat: " + title})
            STORE.save(self.sid, self.model, "AgentChat: " + title)
        self.catalog()

    @serialized_view
    def delete(self, sid):
        if self.inflight or not self.sid or sid != self.sid:
            raise ValueError("select an idle conversation to delete")
        if sid.startswith("ses_codex_"):
            saved = STORE.get(sid)
            if not saved or not saved["thread"]:
                raise ValueError("unknown AgentChat Codex conversation")
            CODEX.thread(saved["model"].split("/", 1)[1], saved["thread"])
            CODEX.archive(saved["thread"])
        else:
            if not chat_session(api("GET", "/session/" + sid)):
                raise ValueError("not an AgentChat conversation")
            result = api("DELETE", "/session/" + sid)
            if result is False:
                raise ValueError("the bridge could not delete this conversation")
        STORE.delete(sid)
        IMAGES.delete_session(sid)
        self.send("Z:" + sid)  # acknowledge only after successful upstream deletion
        self.select("")

    def _start_sse(self):
        if self.sse_thread and self.sse_thread.is_alive():
            return
        self.sse_thread = threading.Thread(target=self._sse, daemon=True)
        self.sse_thread.start()

    def _sse(self):
        try:
            req = urllib.request.Request(SERVER + "/event")
            with urllib.request.urlopen(req, timeout=None) as resp:
                buf = b""
                while self.alive:
                    try:
                        chunk = resp.read1(65536)   # return as soon as any data arrives
                    except Exception:
                        chunk = resp.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, _, buf = buf.partition(b"\n")
                        line = raw_line.strip()
                        if not line.startswith(b"data:"):
                            continue
                        payload = line[5:].strip()
                        if payload:
                            try:
                                self._event(json.loads(payload))
                            except Exception:
                                pass
        except Exception as e:
            if self.alive:
                self.send("X:event stream lost: " + str(e))

    @serialized_view
    def _event(self, e):
        et = e.get("type")
        p = e.get("properties", {})
        if et == 'session.status':
            if p.get('sessionID') == self.sid:
                self._upstream_status(p.get('status', {}))
            return
        if not self.inflight and (et or "").startswith("message."):
            return  # ignore late deltas from a stopped turn
        if et in ("question.asked", "question.v2.asked"):
            try:
                self.questions.asked(p)
            except (ValueError, KeyError, TypeError) as error:
                if p.get("sessionID") == self.sid:
                    self.abort("unsupported question")
                    self.send("X:" + str(error))
                    self.send("D:")
        elif et in ("question.replied", "question.rejected", "question.v2.replied", "question.v2.rejected"):
            if p.get("sessionID") == self.sid:
                self.questions.clear(p.get("requestID"))
                if self.inflight: self.send("S:thinking...")
        elif et == "message.updated":
            info = p.get("info", {})
            if info.get("sessionID") == self.sid and info.get("role") == "assistant":
                if self.assistant_msg != info.get("id"):
                    self.send("B:" + speaker(info, model_info(self.model)["name"]))
                self.assistant_msg = info.get("id")
        elif et == "message.part.delta":
            if p.get("sessionID") != self.sid:
                return
            if (p.get("field") == "text" and self.assistant_msg
                    and p.get("messageID") == self.assistant_msg
                    and self.part_types.get(p.get("partID")) == "text"):
                d = p.get("delta", "")
                if d:
                    self.send("T:" + esc(d))
        elif et == "message.part.updated":
            part = p.get("part", {})
            if part.get("sessionID") != self.sid:
                return
            if part.get("id") and part.get("type"):
                self.part_types[part["id"]] = part["type"]
            if part.get('type') == 'file' and self.assistant_msg and part.get('messageID') == self.assistant_msg:
                try: image_id = IMAGES.import_part(part, self.key)
                except (ValueError, OSError) as error:
                    self.send('X:Picture unavailable: ' + esc(str(error))); return
                if image_id and (self.assistant_msg, image_id) not in self.shown_images:
                    IMAGES.bind(self.assistant_msg, self.sid, image_id)
                    IMAGES.show(image_id, self.send)
                    self.send('G:ROW\t' + image_id)
                    self.shown_images.add((self.assistant_msg, image_id))
            if part.get("type") == "tool":
                tool = part.get("tool", "")
                state = part.get("state", {}).get("status", "")
                if tool and state and not self.questions.pending:
                    self.send("S:tool " + tool + " " + state)
        elif et == "session.idle":
            sid = p.get("sessionID") or p.get("id")
            if sid == self.sid:
                self._upstream_status({'type':'idle'})

    def abort(self, reason, timeout=30):
        self.questions.clear()
        if self.inflight and self.sid:
            self.stopped = True
            log("aborting %s (%s)" % (self.sid, reason))
            if self.model.startswith("codex/"):
                self.inflight = False
                self._save_codex_parts()
                if self.codex_turn: self.retired_codex_turns.append(self.codex_turn)
                CODEX.interrupt(self.codex_thread, self.codex_turn, timeout=min(timeout, 20))
                return
            try:
                api("POST", "/session/%s/abort" % self.sid, {}, timeout=timeout)
            except Exception as e:
                log("abort failed for %s: %r" % (self.sid, e))
            self.inflight = False

    def _save_codex_parts(self):
        for mid, text in list(self.codex_parts.items()):
            if text:
                STORE.message(self.sid + "_" + mid, self.sid, "assistant", text, model_info(self.model)["name"])

    @serialized_view
    def _codex_event(self, method, params):
        tid = params.get("turnId") or params.get("turn", {}).get("id")
        if params.get("threadId", self.codex_thread) != self.codex_thread or tid in self.retired_codex_turns:
            return
        if method == 'turn/started' and not self.stopped:
            self.codex_turn = tid
            self.codex_parts = {}
            if not self.inflight:
                self.inflight = True
                self.send('S:thinking...')
            return
        if not self.inflight:
            return
        if tid and self.codex_turn and tid != self.codex_turn:
            return
        if method == "item/agentMessage/delta":
            mid = params["itemId"]
            if mid not in self.codex_parts:
                self.codex_parts[mid] = ""
                self.send("B:" + model_info(self.model)["name"])
            delta = params.get("delta", "")
            self.codex_parts[mid] += delta
            if delta:
                self.send("T:" + esc(delta))
        elif method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "agentMessage":
                mid = item["id"]
                if not self.codex_parts.get(mid) and item.get("text"):
                    self.codex_parts[mid] = item["text"]
                    self.send("B:" + model_info(self.model)["name"])
                    self.send("T:" + esc(item["text"]))
                self._save_codex_parts()
        elif method == "item/started":
            item = params.get("item", {})
            if item.get("type") in ("commandExecution", "fileChange", "mcpToolCall", "webSearch"):
                self.send("S:" + item["type"])
        elif method == "turn/completed":
            if tid: self.retired_codex_turns.append(tid)
            self._save_codex_parts()
            self.inflight = False
            turn = params.get("turn", {})
            if turn.get("status") == "failed":
                self.send("X:" + esc((turn.get("error") or {}).get("message", "Codex reply failed")))
            self.send("D:")
        elif method == "bridge/disconnected":
            self._save_codex_parts()
            self.inflight = False
            self.send("X:Codex CLI disconnected. Reconnect before trying again.")
            self.send("D:")
        elif method == "bridge/error":
            self.abort("Codex event handling failed")
            self.send("X:" + esc(params.get("message", "Codex event handling failed")))
            self.send("D:")

    # ---- prompt with limits --------------------------------------------
    @serialized_view
    def prompt(self, text):
        image_id = getattr(self, "image_attachment", None)
        if getattr(self, "image_error", None): raise ValueError(self.image_error)
        images = [IMAGES.part(image_id, self.key)] if image_id else []
        if images and not model_info(self.model).get("images"):
            raise ValueError("This model does not accept images; select an image-capable contact.")
        if not text.strip() and not images: raise ValueError("Message is empty")
        now = time.monotonic()
        if self.inflight:
            self.send("X:busy — one request at a time")
            return
        if now - self.last_prompt_ts < MIN_PROMPT_GAP:
            self.send("X:rate limited, wait a moment")
            return
        while self.recent and now - self.recent[0] > 60:
            self.recent.popleft()
        if len(self.recent) >= MAX_PROMPTS_PER_MIN:
            self.send("X:slow down — %d prompts/min max" % MAX_PROMPTS_PER_MIN)
            return
        self.recent.append(now)
        self.last_prompt_ts = now
        self._lazy_create(text)
        self.image_attachment = None
        self.stopped = False
        self.inflight = True   # set before submit; cleared by session.idle/abort
        try:
            if self.model.startswith("codex/"):
                self.codex_thread = CODEX.thread(self.model.split("/", 1)[1], self.codex_thread)
                self.codex_parts = {}
                self.codex_turn = None
                mid = uuid.uuid4().hex
                STORE.message(mid, self.sid, "user", text, "You")
                if image_id: IMAGES.bind(mid, self.sid, image_id)
                self.send("S:thinking...")
                self.codex_turn = CODEX.prompt(self.codex_thread, self.model.split("/", 1)[1], text, self._codex_event, images)
            else:
                ensure_imac_mcp()
                provider, model = self.model.split("/", 1)
                api("POST", "/session/%s/prompt_async" % self.sid,
                {"model": {"providerID": provider, "modelID": model},
                 "system": session_instructions(),
                 "tools": {"question": self.questions_supported},
                 "parts": ([{"type": "text", "text": text}] if text else []) + images})
                if image_id: IMAGES.bind("opencode_" + uuid.uuid4().hex, self.sid, image_id)
        except Exception as e:
            self.inflight = False
            self.send("X:prompt failed: " + str(e))

    @serialized_view
    def image_line(self, payload):
        fields = payload.split("\t")
        op = fields[0]; image_id = fields[1] if len(fields)>1 else ""
        try:
            upload = getattr(self, "image_upload", None)
            if upload and time.monotonic()-upload.used>60: self.image_upload=None; upload=None
            if op=="FETCH" and len(fields)==2:
                IMAGES.show(image_id,self.send)
            elif op=="BEGIN" and len(fields)==4:
                if self.inflight: raise ValueError("Wait for the current reply before uploading a picture")
                self.image_upload=Upload(image_id,int(fields[2]),int(fields[3]))
            elif op=="DATA" and len(fields)==3 and upload:
                upload.chunk(image_id,fields[2])
            elif op=="END" and len(fields)==2 and upload and image_id==upload.id:
                asset=IMAGES.save(upload,self.key); self.image_upload=None
                self.send("G:READY\t%s\t%d\t%d" % (image_id,asset['width'],asset['height']))
            elif op=="CANCEL" and len(fields)==2:
                if upload and upload.id==image_id: self.image_upload=None
            else: raise ValueError("Invalid picture upload sequence")
        except Exception as e:
            self.image_upload=None
            self.send("G:ERROR\t%s\t%s" % (image_id,esc(str(e))))

    @serialized_view
    def publish_image(self, data, sid, connection, caption=''):
        if not self.alive or self.connection_id != connection or self.sid != sid:
            raise ValueError('Macintosh connection or conversation changed before image delivery')
        asset = IMAGES.import_bytes(data, self.key)
        if not self.alive or self.connection_id != connection or self.sid != sid:
            with IMAGES.lock, IMAGES.db:
                IMAGES.db.execute('DELETE FROM image_assets WHERE id=? AND NOT EXISTS (SELECT 1 FROM message_images WHERE asset=?)', (asset['id'], asset['id']))
            raise ValueError('Macintosh connection or conversation changed during image decoding')
        name = model_info(self.model)['name']
        if self.model.startswith('codex/'): self._save_codex_parts()
        mid = IMAGES.publish(sid, asset['id'], name, caption)
        IMAGES.show(asset['id'], self.send)
        self.send('B:' + name)
        if caption: self.send('T:' + esc(caption))
        self.send('G:ROW\t' + asset['id'])
        return {'image': asset['id'], 'message': mid, 'conversation': sid,
                'width': asset['width'], 'height': asset['height'], 'displayed': True}


REG = {}           # client key (ip) -> Session
REG_LOCK = threading.Lock()
CURRENT = {"session": None}


def _live_count():
    return sum(1 for s in REG.values() if s.alive)


def acquire(key):
    """Return a Session for this client key, or None if bridge is full."""
    with REG_LOCK:
        sess = REG.get(key)
        now = time.monotonic()
        if sess is not None:
            if sess.alive:
                # two live connections from same client: refuse the second
                return None
            if now - sess.disconnected_at > RESUME_WINDOW:
                if not sess.questions.pending:
                    sess.abort("resume window expired")
                sess.alive = False
                del REG[key]
            else:
                if _live_count() >= MAX_SESSIONS:
                    return None
                if sess.handler_active:
                    # A failed writer can detach while its reader is still
                    # inside a provider call. Keep that old handler isolated;
                    # the native reconnect restores its saved conversation.
                    replacement = Session(key, None)
                    replacement.model = sess.model
                    replacement.recent = sess.recent.copy()
                    replacement.last_prompt_ts = sess.last_prompt_ts
                    REG[key] = replacement
                    return replacement
                # Reserve the resumed slot under the same admission lock.
                # Otherwise two simultaneous reconnects can share a Session.
                sess.alive = True
                sess.disconnected_at = None
                return sess  # resume same opencode session
        if _live_count() >= MAX_SESSIONS:
            return None
        sess = Session(key, None)   # send bound by handler below
        REG[key] = sess
        return sess


class TCPHandler(socketserver.StreamRequestHandler):
    def setup(self):
        # Kernel probes do not depend on application traffic or Mac scheduling.
        # USER_TIMEOUT also bounds unacknowledged writes / zero-window stalls.
        # These are per-socket settings, never host-wide sysctl changes.
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.request.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, TCP_IDLE_SEC)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, TCP_PROBE_SEC)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, TCP_PROBES)
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, TCP_TIMEOUT_MS)
        super().setup()

    def disconnect(self):
        # Wake readline even when failure was noticed by an SSE/admin writer.
        try:
            self.request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class ChatHandler(TCPHandler):
    def handle(self):
        lock = threading.Lock()
        end_lock = threading.Lock()
        stopped = threading.Event()
        peer = "%s" % (self.client_address[0],)
        sess = None
        connection_id = None
        ended = False

        def end_connection():
            nonlocal ended
            stopped.set()
            self.disconnect()
            with end_lock:
                if ended or sess is None:
                    return
                ended = True
                MAC.disconnected(sess, connection_id)
                try:
                    if sess.inflight and not sess.questions.pending:
                        sess.abort("client disconnected mid-turn", timeout=5)
                except Exception as exc:
                    log("disconnect abort failed for %s: %s" % (peer, exc))
                finally:
                    with REG_LOCK:
                        sess.alive = False
                        sess.disconnected_at = time.monotonic()
                        if CURRENT.get("session") is sess:
                            CURRENT["session"] = next((s for s in REG.values() if s.alive), None)
                    log("disconnected %s connection=%s" % (peer, connection_id))

        def send(line):
            failed = False
            with lock:
                if stopped.is_set():
                    return
                logged = "\t".join(line.split("\t")[:2]) if line.startswith("G:DATA\t") else "\t".join(line.split("\t")[:3]) if line.startswith("C:RPC\t") else line
                if line != "H:alive":
                    log("SENT %r to %s" % (logged, peer))
                try:
                    self.wfile.write((line + "\n").encode("mac_roman", "replace"))
                    self.wfile.flush()
                except OSError as exc:
                    log("send failed to %s: %s" % (peer, exc))
                    stopped.set()
                    failed = True
            # Abort/question cleanup can call send again. Never hold the
            # write lock while entering lifecycle cleanup.
            if failed:
                end_connection()

        sess = acquire(peer)
        if sess is None:
            with REG_LOCK:
                duplicate = bool(REG.get(peer) and REG[peer].alive)
            reason = "this Mac is already connected; waiting for its previous connection to close" if duplicate else "bridge busy — %d sessions max" % MAX_SESSIONS
            log("connection refused from %s (%s)" % (peer, reason))
            send("X:" + reason)
            return
        sess.send = send
        sess.questions_supported = False
        sess.alive = True
        sess.disconnected_at = None
        connection_id = sess.connection_id = uuid.uuid4().hex
        sess.handler_active = True
        sess.peer_address = self.client_address
        if sess.sid and not sess.model.startswith("codex/"):
            sess._start_sse()
        with REG_LOCK:
            CURRENT["session"] = sess
        log("connected %s session=%s inflight=%s"
            % (peer, sess.sid, sess.inflight))
        def heartbeat():
            # Independent of history/provider calls in the reader. Old clients
            # safely ignore H:alive; v94 arms its relay-loss watchdog on it.
            while not stopped.wait(HEARTBEAT_SEC):
                send("H:alive")
        try:
            send("H:alive")
            send("S:connected")
            threading.Thread(target=heartbeat, daemon=True).start()
            while not stopped.is_set():
                raw = self.rfile.readline(65537)
                if not raw:
                    break
                if len(raw) > 65536:
                    send("X:client line exceeds protocol limit")
                    break
                line = raw.decode("mac_roman", "replace").rstrip("\r\n")
                if not line.strip():
                    continue
                if line.startswith("E:"):
                    MAC.reply(sess, connection_id, line)
                    continue
                if line == "H:questions":
                    sess.questions_supported = True
                    continue
                if line.startswith("I:"):
                    sess.questions.line(line[2:])
                    continue
                if line.startswith("G:"):
                    sess.image_line(line[2:]); continue
                if line.startswith("Q:"):
                    try:
                        IMAGES.get(line[2:],sess.key)
                        sess.image_attachment=line[2:]; sess.image_error=None
                    except ValueError as e:
                        sess.image_attachment=None; sess.image_error=str(e)
                        send("X:"+str(e))
                    continue
                if line.startswith("B64M:"):
                    try:
                        _, w, h, stride = line.split(":")
                        SHOT.update(w=int(w), h=int(h), stride=int(stride))
                    except Exception:
                        pass
                    continue
                if line.startswith("B64C:"):
                    SHOT["buf"] += line[5:]
                    SHOT["chunks"] += 1
                    continue
                if line.startswith("B64E"):
                    _save_shot()
                    continue
                if line.startswith("C:"):
                    continue  # app won't send C:, but just in case
                if line in ("F:", "L:", "N:", "A:") or line.startswith(("M:", "O:", "R:", "Z:", "W:")):
                    try:
                        if line == "F:":
                            sess.models()
                        elif line.startswith("W:"):
                            sess.model_settings(line[2:])
                        elif line.startswith("M:"):
                            sess.select_model(line[2:])
                        elif line == "L:":
                            sess.catalog()
                        elif line == "A:":
                            sess.abort("user stopped reply")
                            send("S:reply stopped")
                            send("D:")
                        elif line.startswith("R:"):
                            sess.rename(unesc(line[2:]))
                        elif line.startswith("Z:"):
                            sess.delete(line[2:])
                        else:
                            sess.select(line[2:] if line.startswith("O:") else "")
                    except Exception as e:
                        send("V:ERROR\t" + esc("History unavailable: " + str(e)))
                    continue
                if line.startswith("P:"):
                    log("PROMPT raw %r from %s" % (line[2:], peer))
                    try:
                        sess.prompt(unesc(line[2:]))
                    except Exception as e:
                        send("X:prompt failed: " + str(e))
                    continue
                # Anything else is NOT a prompt.
                if HTTP_SHAPED.search(line):
                    log("PROBE rejected from %s: %r" % (peer, line[:80]))
                    send("X:http probes are not chat — use P:")
                else:
                    log("ignored non-prompt from %s: %r" % (peer, line[:80]))
        except Exception as e:
            sys.stderr.write("chat handler: %r\n" % (e,))
        finally:
            end_connection()
            with REG_LOCK:
                sess.handler_active = False
            try:
                self.request.close()
            except Exception:
                pass


def _save_shot():
    if not SHOT["buf"]:
        return
    os.makedirs(SHOTS_DIR, exist_ok=True)
    ts = int(time.time())
    raw = base64.b64decode(SHOT["buf"])
    base = os.path.join(SHOTS_DIR, "shot-%d" % ts)
    try:
        with open(base + ".raw", "wb") as f:
            f.write(raw)
        if SHOT["w"] and SHOT["h"] and SHOT["stride"]:
            from PIL import Image, ImageOps
            # QD 1-bit bitmaps: MSB-first rows, stride-byte rows.
            # QD bit sense is inverted vs PIL mode "1" (QD 0=white),
            # so invert after converting.
            img = Image.frombytes("1", (SHOT["w"], SHOT["h"]), raw, "raw",
                                "1", SHOT["stride"])
            img = ImageOps.invert(img.convert("RGB"))
            img.save(base + ".png")
            log("screenshot saved: %s.png (%dx%d, %d chunks)"
                % (base, SHOT["w"], SHOT["h"], SHOT["chunks"]))
        else:
            log("screenshot saved raw only: %s.raw (%d bytes)" % (base, len(raw)))
    except Exception as e:
        log("screenshot save failed: %r (raw written)" % (e,))
    SHOT["buf"] = ""
    SHOT["chunks"] = 0
    SHOT["w"] = SHOT["h"] = SHOT["stride"] = 0


class AdminHandler(TCPHandler):
    def handle(self):
        failed = False
        def out(line):
            nonlocal failed
            try:
                self.wfile.write((line + "\n").encode())
                self.wfile.flush()
            except OSError:
                failed = True
                self.disconnect()

        out("agentd admin console. commands: connections | screen | update <file> | quit | tool <json> | rpc <json>")
        try:
            while not failed:
                raw = self.rfile.readline(24 * 1024 * 1024 + 1)
                if not raw:
                    break
                if len(raw) > 24 * 1024 * 1024:
                    out(json.dumps({"isError": True, "content": [{"type": "text", "text": "Admin request exceeds limit"}]})); break
                cmd = raw.decode().strip()
                if not cmd:
                    continue
                if cmd == "connections":
                    with REG_LOCK:
                        now = time.monotonic()
                        table = [{"peer": getattr(s, "peer_address", (s.key, None)),
                                  "alive": s.alive, "connection": s.connection_id,
                                  "inflight": s.inflight,
                                  "disconnected_seconds": None if s.alive else now - s.disconnected_at}
                                 for s in REG.values()]
                    out(json.dumps({"max_sessions": MAX_SESSIONS, "sessions": table}))
                    continue
                sess = CURRENT.get("session")
                if cmd.startswith(("tool ", "rpc ")):
                    try:
                        request = json.loads(cmd[5:] if cmd.startswith("tool ") else cmd[4:])
                        is_host = cmd.startswith("tool ") and (request.get("name", "").startswith("imac_vm_") or
                            request.get("name") in {"imac_get_context", "imac_read_guide", "imac_inspect_archive"})
                        if not is_host and (sess is None or not sess.alive):
                            raise RuntimeError("No live Macintosh connection. Launch AgentChat and connect to this relay.")
                        with MAC.tool_lock:
                            tools = MacTools(MAC, sess)
                            result = tools.call(request["name"], request.get("arguments", {})) if cmd.startswith("tool ") else tools.rpc(**request)
                            if request.get("name") == "imac_get_state" and "structuredContent" in result:
                                result["structuredContent"]["relay_test_mode"] = TEST_MODE
                                result["content"][0]["text"] = json.dumps(result["structuredContent"])
                        out(json.dumps(result))
                    except Exception as e:
                        out(json.dumps({"isError": True, "content": [{"type": "text", "text": str(e)}]}))
                    continue
                if sess is None or not getattr(sess, "alive", False):
                    out("!! no live iMac chat connection")
                    continue
                if cmd == "screen":
                    sess.send("C:SCREEN")
                    out("-> screenshot requested; check bridge stdout and ./shots/")
                elif cmd.startswith("update "):
                    sess.send("C:UPDATE " + cmd[7:])
                    out("-> update requested: " + cmd[7:])
                elif cmd == "quit":
                    sess.send("C:QUIT")
                    out("-> quit sent")
                else:
                    out("?? unknown command: " + cmd)
        except Exception:
            pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    global SERVER
    url = sys.argv[1] if len(sys.argv) > 1 else SERVER
    SERVER = url
    def shutdown(signum, frame):
        VM_DEBUG.close()
        for session in list(REG.values()):
            try:
                if session.inflight: session.abort("relay shutting down")
            except Exception as e: log("shutdown abort: " + str(e))
        CODEX.close()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    if TEST_MODE:
        state = api("GET", "/state", timeout=5)
        if not isinstance(state.get("prompts"), list) or "turn_seconds" not in state:
            raise SystemExit("AGENT_TEST_MODE requires the recording opencode stub")
    log("agentd: chat :%d  admin :%d  -> %s  model %s  max_sessions %d"
        % (CHAT_PORT, ADMIN_PORT, SERVER, CONFIGURED_MODEL or "(first available)", MAX_SESSIONS))
    threading.Thread(target=Server(("127.0.0.1", ADMIN_PORT), AdminHandler)
                    .serve_forever, daemon=True).start()
    Server(("0.0.0.0", CHAT_PORT), ChatHandler).serve_forever()


if __name__ == "__main__":
    main()
