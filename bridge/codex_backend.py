"""Codex CLI's native app-server protocol, over a private stdio connection.

No shell interpolation, credential copying, or inference during discovery.
Protocol reference: https://learn.chatgpt.com/docs/app-server
"""
import atexit
import json
import os
from pathlib import Path
import queue
import shlex
import subprocess
import threading
import sys
import time
from mac_context import MCP_TOOL_TIMEOUT_SEC, prepare_runtime, session_instructions


class CodexBackend:
    def __init__(self, command=None):
        self.command = command or shlex.split(os.environ.get("CODEX_COMMAND", "codex app-server --stdio"))
        if command is None and os.environ.get("IMAC_TOOLS_ENABLED", "1") == "1":
            # Per-process overrides; leave the user's config and credentials alone.
            overrides = {"command": sys.executable,
                         "args": [str(Path(__file__).with_name("mac_mcp.py").resolve()), "--admin-port", os.environ.get("ADMIN_PORT", "8766")],
                         "startup_timeout_sec": 10, "tool_timeout_sec": MCP_TOOL_TIMEOUT_SEC}
            for key, value in overrides.items():
                self.command += ["-c", "mcp_servers.imac." + key + "=" + json.dumps(value)]
        self.process = None
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.pending = {}
        self.listeners = {}
        self.next_id = 0
        self.loaded = set()
        self.events = queue.Queue()
        threading.Thread(target=self._dispatch, daemon=True).start()
        atexit.register(self.close)

    def _journal(self, method, params):
        """Optional tool/turn evidence for explicitly requested live experiments."""
        path = os.environ.get("CODEX_EVENT_LOG")
        if not path: return
        def bounded(value):
            if isinstance(value, str) and len(value) > 4096:
                return value[:4096] + " [truncated]"
            if isinstance(value, dict): return {k: bounded(v) for k, v in value.items()}
            if isinstance(value, list): return [bounded(v) for v in value[:64]]
            return value
        try:
            with self.lock, open(path, "a") as stream:
                stream.write(json.dumps({"time": time.time(), "method": method,
                                         "params": bounded(params)}) + "\n")
        except OSError:
            pass  # Diagnostics must not interfere with a running turn.

    def start(self):
        with self.start_lock:
            if self.process and self.process.poll() is None:
                return
            self.loaded.clear()
            self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            text=True, bufsize=1)
            threading.Thread(target=self._read, args=(self.process,), daemon=True).start()
            try:
                self.call("initialize", {"clientInfo": {"name": "agentchat", "title": "AgentChat Mac OS 9",
                                               "version": "1"}}, start=False)
                self.notify("initialized", {})
            except Exception:
                self.close()
                raise

    def _write(self, message):
        if message.get("method") in ("thread/start", "thread/resume", "turn/start", "turn/interrupt"):
            self._journal("request/" + message["method"], message.get("params", {}))
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise RuntimeError("Codex CLI is not running")
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()

    def notify(self, method, params):
        self._write({"method": method, "params": params})

    def call(self, method, params, timeout=20, start=True):
        if start:
            self.start()
        with self.lock:
            self.next_id += 1
            rid = self.next_id
            result = self.pending[rid] = queue.Queue(maxsize=1)
        try:
            self._write({"id": rid, "method": method, "params": params})
            reply = result.get(timeout=timeout)
            if "error" in reply:
                raise RuntimeError(reply["error"].get("message", "Codex request failed"))
            return reply.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(rid, None)

    def _read(self, process):
        try:
            for raw in process.stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if "id" in message and "method" not in message:
                    with self.lock:
                        pending = self.pending.get(message["id"])
                    if pending:
                        try: pending.put_nowait(message)
                        except queue.Full: pass
                elif "id" in message:
                    # The relay has no interactive approval/elicitation UI.
                    # Never leave the model waiting indefinitely for one.
                    self._write({"id": message["id"], "error": {
                        "code": -32601, "message": "This interaction is unavailable in AgentChat."}})
                else:
                    params = message.get("params", {})
                    method = message.get("method", "")
                    if method in ("turn/started", "turn/completed", "item/started", "item/completed",
                                  "thread/tokenUsage/updated", "error"):
                        # Do not journal the model's private reasoning content.
                        if params.get("item", {}).get("type") != "reasoning":
                            self._journal(method, params)
                    thread = params.get("threadId")
                    with self.lock:
                        listener = self.listeners.get(thread)
                    if listener:
                        self.events.put((listener, message.get("method", ""), params))
        finally:
            with self.lock:
                if self.process is not process: return
                pending = list(self.pending.values())
                listeners = list(self.listeners.values())
            for entry in pending:
                try:
                    entry.put_nowait({"error": {"message": "Codex CLI disconnected"}})
                except queue.Full:
                    pass
            for listener in listeners:
                self.events.put((listener, "bridge/disconnected", {}))

    def _dispatch(self):
        while True:
            listener, method, params = self.events.get()
            try:
                listener(method, params)
            except Exception as e:
                try: listener("bridge/error", {"message": str(e)})
                except Exception: pass

    def models(self):
        answer = self.call("model/list", {"limit": 100, "includeHidden": False})
        return [{"id": "codex/" + item["model"],
                 "name": item.get("displayName", item["model"]),
                 "backend": "codex", "source": "Codex", "recommended": bool(item.get("isDefault")),
                 "images": "image" in item.get("inputModalities", ["text", "image"])} for item in answer.get("data", []) if not item.get("hidden")]

    def thread(self, model, thread_id=None):
        self.start()
        if thread_id and thread_id in self.loaded:
            return thread_id
        workspace = prepare_runtime()["workspace"]
        options = {"model": model, "modelProvider": "openai",
                   "developerInstructions": session_instructions(),
                   "cwd": workspace,
                   "approvalPolicy": "never", "sandbox": os.environ.get("CODEX_SANDBOX", "workspace-write")}
        if thread_id:
            options["threadId"] = thread_id
        result = self.call("thread/resume" if thread_id else "thread/start", options)
        tid = result["thread"]["id"]
        self.loaded.add(tid)
        return tid

    def prompt(self, thread_id, model, text, listener, images=()):
        with self.lock:
            self.listeners[thread_id] = listener
        inputs = ([{"type": "text", "text": text}] if text else []) + [{"type": "image", "url": image["url"]} for image in images]
        options = {"threadId": thread_id, "model": model, "input": inputs}
        effort = os.environ.get("CODEX_REASONING_EFFORT")
        if effort: options["effort"] = effort
        result = self.call("turn/start", options)
        return result["turn"]["id"]

    def interrupt(self, thread_id, turn_id, timeout=20):
        if thread_id and turn_id:
            self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=timeout)

    def archive(self, thread_id):
        self.call("thread/archive", {"threadId": thread_id})
        with self.lock:
            self.listeners.pop(thread_id, None)
        self.loaded.discard(thread_id)

    def close(self):
        process = self.process
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
