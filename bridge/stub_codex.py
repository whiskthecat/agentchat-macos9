#!/usr/bin/env python3
"""Recording Codex app-server stand-in. Never imports or runs a real model."""
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid

LOCK = threading.Lock()
THREADS = {}
TURNS = {}
STATE = {"test_only": True, "prompts": [], "aborts": [], "archives": []}
PATH = os.environ.get("STUB_CODEX_STATE_FILE")
if PATH and Path(PATH).exists():
    STATE.update(json.loads(Path(PATH).read_text()))


def record():
    if PATH:
        temp = Path(PATH + ".tmp")
        temp.write_text(json.dumps(STATE)); temp.replace(PATH)


def emit(message):
    with LOCK:
        print(json.dumps(message), flush=True)


def notify(method, params):
    emit({"method": method, "params": params})


def request(message):
    method, params = message.get("method"), message.get("params", {})
    if "id" not in message: return
    result = {}
    if method == "model/list":
        result = {"data": [{"id": "gpt-6.1-sol", "model": "gpt-6.1-sol",
                            "displayName": "6.1 Sol", "hidden": False, "isDefault": True},
                           {"id": "gpt-5.5", "model": "gpt-5.5", "displayName": "GPT-5.5", "hidden": False}], "nextCursor": None}
        # Optional scroll fixture. These are recording-only entries, never
        # provider model names or real inference destinations.
        for i in range(min(200, max(0, int(os.environ.get("STUB_CODEX_EXTRA_MODELS", "0"))))):
            mid = "scroll-fixture-%03d" % i
            result["data"].append({"id": mid, "model": mid,
                "displayName": "Test Model %03d" % i, "hidden": False})
    elif method in ("thread/start", "thread/resume"):
        tid = params.get("threadId") or str(uuid.uuid4())
        THREADS[tid] = params.get("model", "gpt-6.1-sol")
        STATE.setdefault("thread_setups", []).append({"method": method, **params})
        record()
        result = {"thread": {"id": tid}, "model": THREADS[tid]}
    elif method == "turn/start":
        tid, turn = params["threadId"], uuid.uuid4().hex
        if tid not in THREADS: raise ValueError("Stub thread must be loaded before a turn")
        text = "".join(p.get("text", "") for p in params["input"] if p["type"]=="text")
        STATE.setdefault("images", []).extend({"thread":tid,"input":p} for p in params["input"] if p["type"]=="image")
        prompt = {"thread": tid, "model": params["model"], "text": text}
        if "effort" in params: prompt["effort"] = params["effort"]
        STATE["prompts"].append(prompt); record()
        cancelled = TURNS[turn] = threading.Event()
        emit({"id": message["id"], "result": {"turn": {"id": turn}}})
        def reply():
            base = {"threadId": tid, "turnId": turn, "itemId": turn + "_reply"}
            for delta in ("codex-stub-reply:", text):
                if cancelled.wait(.15): return
                notify("item/agentMessage/delta", {**base, "delta": delta})
            notify("item/completed", {"threadId": tid, "turnId": turn,
                "item": {"id": base["itemId"], "type": "agentMessage", "text": "codex-stub-reply:" + text}})
            if cancelled.wait(1): return
            notify("turn/completed", {"threadId": tid, "turn": {"id": turn, "status": "completed"}})
        threading.Thread(target=reply, daemon=True).start()
        return
    elif method == "turn/interrupt":
        turn = params["turnId"]
        if turn in TURNS: TURNS[turn].set()
        STATE["aborts"].append(turn); record()
        notify("turn/completed", {"threadId": params["threadId"], "turn": {"id": turn, "status": "interrupted"}})
    elif method == "thread/archive":
        STATE["archives"].append(params["threadId"]); record()
    elif method not in ("initialize", "thread/name/set"):
        emit({"id": message["id"], "error": {"code": -32601, "message": "Unknown stub method"}});return
    emit({"id": message["id"], "result": result})


for line in sys.stdin:
    message = {}
    try:
        message = json.loads(line)
        request(message)
    except Exception as e:
        emit({"id": message.get("id"), "error": {"code": -32000, "message": str(e)}})
