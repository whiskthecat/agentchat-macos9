#!/usr/bin/env python3
"""Fake opencode API server that RECORDS every call for bridge testing.
Port 7788. Emits: session create, prompt_async (opens a turn that closes
with session.idle after TURN_SECONDS), abort. SSE /event broadcasts."""
import json
import os
from pathlib import Path
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

STATE = {"sessions": 0, "prompts": [], "aborts": [], "deletes": [], "turn_seconds": 1.5}
BROADCAST = []          # list of queues
BLOCK_LOCK = threading.Lock()
SESSIONS = {}
MESSAGES = {}
TURNS = {}
SESSION_STATUS = {}
STATE_FILE = os.environ.get("STUB_STATE_FILE")
FILE_LOCK = threading.Lock()
HOLD_NEXT_TURN = threading.Event()
QUESTION_NEXT_TURN = None
QUESTIONS = {}
QUESTION_ANSWERS = {}


def persist():
    if STATE_FILE:
        with FILE_LOCK:
            path = Path(STATE_FILE)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"state": STATE, "sessions": SESSIONS, "messages": MESSAGES}))
            tmp.replace(path)


def restore():
    if not STATE_FILE or not Path(STATE_FILE).exists():
        return
    data = json.loads(Path(STATE_FILE).read_text())
    if "state" in data:
        STATE.update(data["state"]); SESSIONS.update(data["sessions"]); MESSAGES.update(data["messages"])
    else:
        # Migrate a snapshot from the original recording-only stub.
        STATE.update(data)
        now = int(time.time() * 1000)
        for index, prompt in enumerate(STATE["prompts"]):
            sid, text = prompt["sid"], prompt["text"]
            stamp = now - (len(STATE["prompts"]) - index) * 3000
            if sid not in SESSIONS:
                SESSIONS[sid] = {"id": sid, "title": "AgentChat: " + " ".join(text.split())[:100],
                                 "time": {"created": stamp, "updated": stamp}}
                MESSAGES[sid] = []
            for role, body in (("user", text), ("assistant", "stub-reply-to:" + text)):
                MESSAGES[sid].append({"info": {"role": role, "time": {"created": stamp}},
                                      "parts": [{"type": "text", "text": body}]})
            SESSIONS[sid]["time"]["updated"] = stamp
        persist()


def publish(obj):
    if obj.get('type') == 'session.status':
        p = obj['properties']; SESSION_STATUS[p['sessionID']] = p['status']
    elif obj.get('type') == 'session.idle':
        SESSION_STATUS[obj['properties']['sessionID']] = {'type':'idle'}
    with BLOCK_LOCK:
        for q in list(BROADCAST):
            q.append(json.dumps(obj))


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        global QUESTION_NEXT_TURN
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        p = self.path
        if p == "/test/question_next_turn":
            QUESTION_NEXT_TURN = json.loads(raw)["questions"]
            self._json(200, {"queued": True})
        elif p.startswith("/question/") and p.endswith("/reply"):
            request_id = p.split("/")[2]
            if request_id not in QUESTIONS:
                self._json(404, {"error": "Question no longer pending"}); return
            request = QUESTIONS.pop(request_id)
            answers = json.loads(raw)["answers"]
            QUESTION_ANSWERS[request_id] = answers
            STATE.setdefault("question_replies", []).append({"id": request_id, "sid": request["sessionID"], "answers": answers})
            persist(); self._json(200, True)
            publish({"type": "question.replied", "properties": {"sessionID": request["sessionID"], "requestID": request_id, "answers": answers}})
        elif p == "/test/hold_next_turn":
            HOLD_NEXT_TURN.set(); self._json(200, {"held": True})
        elif p == '/test/stream':
            body = json.loads(raw); sid = body['sid']; text = body['text']
            if sid not in SESSIONS or not isinstance(text, str) or len(text) > 16384:
                self._json(400, {'error':'Invalid recording stream'}); return
            delay = max(.02, min(.5, float(body.get('delay', .08))))
            chunk = max(1, min(128, int(body.get('chunk', 24))))
            wake_delay = max(0, min(10, float(body.get('wake_delay', 0))))
            mid = 'msg_external_' + uuid.uuid4().hex
            cancelled = threading.Event()
            STATE.setdefault('external_turns', []).append({'sid':sid,'text':text,'mid':mid})
            persist(); self._json(202, {'message':mid})
            def external_turn():
                if cancelled.wait(wake_delay): return
                TURNS[sid] = cancelled
                stamp = int(time.time()*1000)
                message = {'info':{'id':mid,'role':'assistant','time':{'created':stamp}},
                           'parts':[{'id':mid+'_text','type':'text','text':''}]}
                MESSAGES[sid].append(message)
                publish({'type':'session.status','properties':{'sessionID':sid,'status':{'type':'busy'}}})
                publish({'type':'message.updated','properties':{'info':{'id':mid,'role':'assistant','sessionID':sid}}})
                publish({'type':'message.part.updated','properties':{'part':{'id':mid+'_text','type':'text','sessionID':sid,'messageID':mid}}})
                for at in range(0,len(text),chunk):
                    if cancelled.wait(delay): return
                    delta = text[at:at+chunk]; message['parts'][0]['text'] += delta
                    publish({'type':'message.part.delta','properties':{'sessionID':sid,'messageID':mid,'partID':mid+'_text','field':'text','delta':delta}})
                persist()
                publish({'type':'session.status','properties':{'sessionID':sid,'status':{'type':'idle'}}})
                publish({'type':'session.idle','properties':{'sessionID':sid}})
            threading.Thread(target=external_turn,daemon=True).start()
        elif p == "/mcp":
            body = json.loads(raw); STATE.setdefault("mcp", {})[body["name"]] = body["config"]; persist()
            self._json(200, {body["name"]: {"status": "connected"}})
        elif p == "/session":
            STATE["sessions"] += 1
            sid = "ses_stub_%d" % STATE["sessions"]
            now = int(time.time() * 1000)
            info = {"id": sid, "title": json.loads(raw).get("title", ""),
                    "time": {"created": now, "updated": now}}
            SESSIONS[sid] = info
            MESSAGES[sid] = []
            persist()
            self._json(200, info)
            publish({"type": "session.updated",
                    "properties": {"info": {"id": sid}}})
        elif "/prompt_async" in p:
            sid = p.split("/")[2]
            body = json.loads(raw or b"{}")
            text = (body.get("parts") or [{}])[0].get("text", "")
            mid = "msg_stub_%d" % len(STATE["prompts"])
            STATE["prompts"].append({"sid": sid, "text": text})
            STATE.setdefault("instructions", []).append({"sid": sid, "system": body.get("system")})
            STATE.setdefault("models", []).append({"sid": sid, "model": body.get("model", {})})
            now = int(time.time() * 1000)
            MESSAGES[sid].append({"info": {"id": mid + "_user", "role": "user", "time": {"created": now}},
                                  "parts": body.get("parts", [{"type": "text", "text": text}])})
            STATE.setdefault("images", []).extend({"sid":sid,"part":p} for p in body.get("parts",[]) if p.get("type")=="file")
            cancelled = threading.Event()
            held = HOLD_NEXT_TURN.is_set(); HOLD_NEXT_TURN.clear()
            question_fixture = QUESTION_NEXT_TURN; QUESTION_NEXT_TURN = None
            TURNS[sid] = cancelled
            persist()
            self._json(202, {})

            def turn():
                if cancelled.wait(0.2):
                    return
                publish({"type": "message.updated", "properties": {
                    "info": {"id": mid, "sessionID": sid, "role": "assistant",
                             **body.get("model", {})}}})
                publish({"type": "message.part.updated", "properties": {
                    "part": {"id": "prt_1", "sessionID": sid, "type": "text"}}})
                publish({"type": "message.part.delta", "properties": {
                    "sessionID": sid, "messageID": mid, "partID": "prt_1",
                    "field": "text", "delta": "stub-reply-to:" + text}})
                MESSAGES[sid].append({"info": {"id": mid, "role": "assistant", "time": {"created": now + 1},
                                              **body.get("model", {})},
                                      "parts": [{"id": "prt_1", "type": "text", "text": "stub-reply-to:" + text}]})
                SESSIONS[sid]["time"]["updated"] = int(time.time() * 1000)
                persist()
                if question_fixture:
                    request_id = "que_stub_%d" % len(STATE["prompts"])
                    request = {"id": request_id, "sessionID": sid, "questions": question_fixture,
                               "tool": {"messageID": mid, "callID": "call_stub_question"}}
                    QUESTIONS[request_id] = request
                    publish({"type": "question.asked", "properties": request})
                    while request_id not in QUESTION_ANSWERS:
                        if cancelled.wait(0.1):
                            QUESTIONS.pop(request_id, None); return
                    answer_text = "\nRecorded answers: " + json.dumps(QUESTION_ANSWERS.pop(request_id))
                    publish({"type": "message.part.delta", "properties": {
                        "sessionID": sid, "messageID": mid, "partID": "prt_1", "field": "text", "delta": answer_text}})
                    MESSAGES[sid][-1]["parts"][0]["text"] += answer_text
                    persist()
                if cancelled.wait(30 if held else STATE["turn_seconds"]):
                    return
                publish({"type": "session.idle",
                        "properties": {"sessionID": sid}})
            threading.Thread(target=turn, daemon=True).start()
        elif p.endswith("/abort"):
            STATE["aborts"].append(p.split("/")[2])
            sid = p.split("/")[2]
            if sid in TURNS:
                TURNS[sid].set()
            for request_id, request in list(QUESTIONS.items()):
                if request["sessionID"] == sid: QUESTIONS.pop(request_id, None)
            persist()
            publish({"type": "session.idle", "properties": {"sessionID": sid}})
            self._json(200, {})
        else:
            self._json(404, {"err": "unexpected " + p})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/state":
            self._json(200, STATE)
        elif path == "/question":
            self._json(200, list(QUESTIONS.values()))
        elif path == '/session/status':
            self._json(200, SESSION_STATUS)
        elif path == "/provider":
            # Optional private recording fixture: simulate disappearing and
            # returning provider models without restarting or issuing a turn.
            fixture = os.environ.get("STUB_PROVIDER_FILE")
            if fixture and os.path.isfile(fixture):
                with open(fixture) as f: self._json(200, json.load(f))
                return
            self._json(200, {"connected": ["opencode", "strix"], "all": [
                {"id": "opencode", "models": {"muse-spark-1.3-contributor-free": {"name": "Muse Spark 1.3", "capabilities":{"input":{"image":True}}}}},
                {"id": "strix", "models": {"halogen-qwen3.8-flash-next": {"name": "Qwen 3.8 Flash", "capabilities":{"input":{"image":True}}}}}]})
        elif path == "/session":
            self._json(200, list(SESSIONS.values()))
        elif path.startswith("/session/"):
            sid = path.split("/")[2]
            if sid not in SESSIONS:
                self._json(404, {"error": "unknown session"})
            elif path.endswith("/message"):
                self._json(200, MESSAGES[sid])
            else:
                self._json(200, SESSIONS[sid])
        elif path == "/event":
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            q = []
            with BLOCK_LOCK:
                BROADCAST.append(q)
            try:
                while True:
                    while q:
                        self.wfile.write(b"data: " + q.pop(0).encode() + b"\n\n")
                    self.wfile.flush()
                    time.sleep(0.05)
            except Exception:
                pass
            finally:
                with BLOCK_LOCK:
                    # Empty lists compare equal; remove this subscriber by identity.
                    BROADCAST[:] = [subscriber for subscriber in BROADCAST if subscriber is not q]
        else:
            self._json(404, {})

    def do_PATCH(self):
        sid = urlsplit(self.path).path.split("/")[-1]
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n))
        if sid not in SESSIONS:
            self._json(404, {})
            return
        SESSIONS[sid]["title"] = body.get("title", SESSIONS[sid]["title"])
        SESSIONS[sid]["time"]["updated"] = int(time.time() * 1000)
        persist()
        self._json(200, SESSIONS[sid])

    def do_DELETE(self):
        path = urlsplit(self.path).path
        parts = path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "session" or parts[1] not in SESSIONS:
            self._json(404, {"error": "unknown session"})
            return
        sid = parts[1]
        if sid in TURNS:
            TURNS.pop(sid).set()
        del SESSIONS[sid]
        MESSAGES.pop(sid, None)
        STATE["deletes"].append(sid)
        persist()
        publish({"type": "session.deleted", "properties": {"info": {"id": sid}}})
        self._json(200, True)


if __name__ == "__main__":
    restore()
    ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("STUB_PORT", "7788"))), H).serve_forever()
