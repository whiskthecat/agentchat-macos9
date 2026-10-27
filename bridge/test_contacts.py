#!/usr/bin/env python3
"""Focused model-contact integration. Every turn goes to recording fakes."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request


def until(predicate, seconds=8):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError("timed out")


def main():
    with tempfile.TemporaryDirectory(prefix="agentchat-contacts-") as tmp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        os.environ.update(AGENT_TEST_MODE="1", CODEX_ENABLED="1",
                          CODEX_REASONING_EFFORT="xhigh", CODEX_EVENT_LOG=tmp + "/events.jsonl",
                          STUB_PORT=str(port),
                          OPENCODE_URL=f"http://127.0.0.1:{port}",
                          AGENT_STATE_FILE=tmp + "/contacts.sqlite3",
                          STUB_CODEX_STATE_FILE=tmp + "/codex.json")
        upstream = subprocess.Popen([sys.executable, str(Path(__file__).with_name("stub_opencode.py")), str(port)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            def ready():
                try:
                    return json.load(urllib.request.urlopen(os.environ["OPENCODE_URL"] + "/state", timeout=.3))
                except OSError: return False
            until(ready)
            import agentd as relay
            lines = []
            session = relay.Session("isolated-qa", lines.append)
            models = relay.model_catalog()
            muse, sol = "opencode/muse-spark-1.3-contributor-free", "codex/gpt-6.1-sol"
            # Free OpenCode models first, then each service's recommended model.
            assert models[0]["id"] == muse and sol in [m["id"] for m in models][:3]
            assert ready()["sessions"] == 0 and ready()["prompts"] == []
            # Visibility discovery/settings are read-only and never start turns.
            original_api = relay.api
            provider_fixture = original_api("GET", "/provider")
            provider_fixture["connected"].append("qa-images")
            provider_fixture.setdefault("default", {})["qa-images"] = "image-default"
            provider_fixture["all"].append({"id":"qa-images", "models":{
                "image-default":{"name":"Image generator", "capabilities":{"output":{"text":True},"toolcall":False}},
                "agent":{"name":"Agent model", "capabilities":{"output":{"text":True},"toolcall":True}}}})
            relay.api = lambda method,path,*a,**kw: provider_fixture if path=="/provider" else original_api(method,path,*a,**kw)
            discovered = {m["id"]:m for m in relay.model_catalog()}
            assert not discovered["qa-images/image-default"]["recommended"]
            assert discovered["qa-images/agent"]["recommended"]
            relay.api = original_api
            session.models()
            assert any(line.startswith("F:ITEM\t" + muse) for line in lines)
            assert any(line.startswith("F:ITEM\t" + sol) for line in lines)
            hidden = next(m["id"] for m in models if not m.get("recommended"))
            assert not any(line.startswith("F:ITEM\t" + hidden + "\t") for line in lines)
            session.model_settings("APPLY\t0\t%s\t0\t%s\t1" % (muse, hidden))
            assert relay.STORE.visibility(session.key) == {muse: False, hidden: True}
            lines.clear(); session.model_settings("LIST")
            assert any(line.startswith("W:ITEM\t" + muse + "\t") and line.endswith("\t0\t1") for line in lines)
            reopened = relay.ContactStore(tmp + "/contacts.sqlite3")
            assert reopened.visibility(session.key)[hidden] and not reopened.visibility("other-peer")
            reopened.db.close()
            lines.clear(); session.models()
            assert not any(line.startswith("F:ITEM\t" + muse) for line in lines)
            assert any(line.startswith("F:ITEM\t" + hidden) for line in lines)
            # Hiding the selected model removes its picker row without
            # changing the open contact. The old forced-current rule failed.
            current = session.model
            lines.clear(); session.model_settings("APPLY\t0\t%s\t0" % current)
            assert session.model == current and not any(line.startswith("F:ITEM\t" + current + "\t") for line in lines)
            # One provider discovery result updates settings and picker on
            # refresh. Hidden overrides survive disappearance/reappearance.
            original_catalog = relay.model_catalog
            relay.model_catalog = lambda: [m for m in original_catalog() if m["id"] != hidden]
            lines.clear(); session.model_settings("LIST")
            assert not any(line.startswith(("F:ITEM\t" + hidden + "\t", "W:ITEM\t" + hidden + "\t")) for line in lines)
            relay.model_catalog = original_catalog
            lines.clear(); session.model_settings("LIST")
            assert any(line.startswith("F:ITEM\t" + hidden + "\t") for line in lines)
            assert not any(line.startswith("F:ITEM\t" + current + "\t") for line in lines)
            original_models = relay.CODEX.models
            relay.CODEX.models = lambda: original_models() + [{"id":"codex/new-model", "name":"New model", "backend":"codex", "recommended":True}]
            lines.clear(); session.models()
            assert any(line.startswith("F:ITEM\tcodex/new-model") for line in lines)
            assert not any(line.startswith("F:ITEM\t" + muse) for line in lines)
            relay.CODEX.models = original_models
            before = relay.STORE.visibility(session.key)
            try: session.model_settings("APPLY\t1\tunknown/model\t1")
            except ValueError: pass
            else: raise AssertionError("Unknown model visibility accepted")
            assert relay.STORE.visibility(session.key) == before
            session.model_settings("APPLY\t1")
            assert not relay.STORE.visibility(session.key)
            assert ready()["sessions"] == 0 and ready()["prompts"] == []

            session.select_model(muse)
            assert session.sid is None and any(s.startswith("Y:" + muse) for s in lines)
            session._start_sse(); time.sleep(.15)
            session.prompt("focused Muse fixture")
            until(lambda: not session.inflight)
            muse_sid = session.sid
            assert any(s == "B:Muse Spark 1.3" for s in lines)
            assert ready()["models"][-1]["model"]["modelID"] == muse.split("/", 1)[1]
            session.select_model(sol); assert session.sid is None
            time.sleep(.6); session.prompt("focused Sol fixture")
            until(lambda: not session.inflight)
            sol_sid = session.sid
            assert sol_sid != muse_sid and any(s == "B:6.1 Sol" for s in lines), lines
            session.select("")
            session.select_model(muse)
            assert session.sid == muse_sid
            session.select_model(sol)
            assert session.sid is None, "A blank unsent conversation must reopen instead of the older chat"
            assert relay.STORE.last("isolated-qa", sol) == ""
            session.select(sol_sid)
            lines.clear(); session.select_model(muse)
            assert session.sid == muse_sid
            assert any(s.startswith("V:TEXT\tstub-reply-to:focused Muse") for s in lines)
            assert not any(sol_sid in s for s in lines if s.startswith("J:ITEM"))
            lines.clear(); session.select_model(sol)
            assert session.sid == sol_sid
            assert "V:TEXT\tcodex-stub-reply:focused Sol fixture" in lines
            server = relay.SERVER
            relay.SERVER = "http://127.0.0.1:1"
            try:
                session.select_model(sol)
                assert session.sid == sol_sid
            finally: relay.SERVER = server
            relay.CODEX.close()
            session.rename("Sol fixture renamed")
            assert relay.STORE.get(sol_sid)["title"] == "AgentChat: Sol fixture renamed"
            time.sleep(.6); session.prompt("cancel fixture"); time.sleep(.2); session.abort("test stop")
            assert not session.inflight
            until(lambda: bool(json.loads(Path(tmp + "/codex.json").read_text())["aborts"]))
            persistent = relay.ContactStore(tmp + "/contacts.sqlite3")
            assert persistent.last("isolated-qa", muse) == muse_sid
            assert persistent.history(sol_sid)[0]["parts"][0]["text"] == "focused Sol fixture"
            persistent.db.close()
            session.delete(sol_sid)
            assert relay.STORE.get(sol_sid) is None and relay.STORE.last("isolated-qa", sol) == ""
            assert relay.STORE.last("isolated-qa", muse) == muse_sid
            try: session.select_model("bogus/model")
            except ValueError: pass
            else: raise AssertionError("unknown model accepted")
            assert session.model == sol
            assert len(ready()["prompts"]) == 1
            fake = json.loads(Path(tmp + "/codex.json").read_text())
            assert fake["test_only"] and len(fake["prompts"]) == 2 and fake["archives"]
            assert all(p["effort"] == "xhigh" for p in fake["prompts"])
            events = [json.loads(line) for line in Path(tmp + "/events.jsonl").read_text().splitlines()]
            turns = [event for event in events if event["method"] == "request/turn/start"]
            assert len(turns) == 2 and all(event["params"]["effort"] == "xhigh" for event in turns)
            relay.OPENCODE_ENABLED = False
            def no_opencode(*args, **kwargs):
                raise AssertionError("Codex-only discovery must not contact opencode")
            relay.api = no_opencode
            assert all(m["backend"] == "codex" for m in relay.model_catalog())
            session.alive = False; relay.CODEX.close()
            print("PASS: catalog without turns, Muse/Sol routing, independent history, last-chat reopening, "
                  "rename, cancellation, persistence, deletion, invalid model rejection, Codex restart, "
                  "xhigh forwarding, tool/turn journal and Codex-only discovery; 3 fake turns, 0 real turns")
        finally:
            upstream.terminate(); upstream.wait(timeout=5)


if __name__ == "__main__": main()
