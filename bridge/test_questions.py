#!/usr/bin/env python3
"""Question integration against an isolated recording server; no real models."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time


def until(predicate):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if predicate(): return
        time.sleep(.05)
    raise AssertionError("Timed out waiting for recording backend")


def main():
    with tempfile.TemporaryDirectory(prefix="agentchat-questions-") as tmp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        os.environ.update(AGENT_TEST_MODE="1", CODEX_ENABLED="0", STUB_PORT=str(port),
                          OPENCODE_URL=f"http://127.0.0.1:{port}",
                          AGENT_STATE_FILE=tmp+"/contacts.sqlite3", IMAC_TOOLS_ENABLED="0")
        upstream = subprocess.Popen([sys.executable,str(Path(__file__).with_name("stub_opencode.py"))], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        sessions = []
        try:
            import agentd as relay
            def state():
                try: return relay.api("GET", "/state")
                except OSError: return None
            until(state)
            lines = []; session = relay.Session("question-qa",lines.append); sessions.append(session)
            session.model = "opencode/muse-spark-1.3-contributor-free"; session.questions_supported = True
            fixture = [{"question":"Which edit?\nKeep the draft.","header":"Edit", "options":[{"label":"Cleanup","description":"Clean edges"},{"label":"Resize","description":"Dimensions"}]},
                       {"question":"Choose effects", "header":"Effects", "multiple":True,
                        "options":[{"label":"One","description":"First"},{"label":"Two","description":"Second"}]}]
            relay.api("POST","/test/question_next_turn",{"questions":fixture})
            session.prompt("Recording question test")
            until(lambda: session.questions.pending)
            request = session.questions.pending; rid = request["id"]
            assert session.inflight and "S:Answer needed" in lines
            session._event({"type":"message.part.updated","properties":{"part":{"sessionID":session.sid,"type":"tool","tool":"question","state":{"status":"running"}}}})
            assert lines[-1] == "S:Answer needed"
            assert any(line.startswith("I:QUESTION\t0\t0\t1\tEdit\tWhich edit?\\n") for line in lines)
            original_prompts = len(state()["prompts"])
            # Invalid, stale and incomplete replies must never reach upstream.
            for commands in ([f"REPLY\t{rid}",f"SUBMIT\t{rid}"],
                             [f"REPLY\t{rid}",f"ANSWER\t{rid}\t0\tO\t99"],
                             ["REPLY\tque_stale"],
                             [f"REPLY\t{rid}",f"ANSWER\t{rid}\t0\tC\t   "],
                             [f"REPLY\t{rid}",f"ANSWER\t{rid}\t0\tO\t0",f"ANSWER\t{rid}\t0\tO\t1",f"ANSWER\t{rid}\t1\tO\t0",f"SUBMIT\t{rid}"]):
                before = len(lines)
                for command in commands: session.questions.line(command)
                assert any(line.startswith("I:ERROR\t") for line in lines[before:])
                assert state().get("question_replies",[]) == [] and session.inflight
            # Closing/reconnecting recovers the pending question without a prompt.
            recovered_lines = []; recovered = relay.Session("question-recovery",recovered_lines.append); sessions.append(recovered)
            recovered.select(session.sid)
            until(lambda: recovered.questions.pending)
            assert recovered.inflight and any(line.startswith("I:BEGIN\t"+rid) for line in recovered_lines)
            assert len(state()["prompts"]) == original_prompts
            # A stale event from another conversation cannot clear the question.
            recovered._event({"type":"question.replied","properties":{"sessionID":"ses_other","requestID":rid}})
            assert recovered.questions.pending
            for command in (f"REPLY\t{rid}",f"ANSWER\t{rid}\t0\tC\tMake it blue\\nwith \\\\ outlines",f"ANSWER\t{rid}\t1\tO\t0",f"ANSWER\t{rid}\t1\tO\t1",f"SUBMIT\t{rid}"):
                recovered.questions.line(command)
            until(lambda: not recovered.inflight)
            assert state()["question_replies"][0]["answers"] == [["Make it blue\nwith \\ outlines"],["One","Two"]]
            assert len(state()["prompts"]) == original_prompts
            assert any(line.startswith("T:") and "Recorded answers:" in line for line in recovered_lines)
            recovered.questions.line(f"SUBMIT\t{rid}")
            assert len(state()["question_replies"]) == 1
            # custom:false is enforced, and stopping cancels rather than answering.
            fixture[0]["custom"] = False
            relay.api("POST","/test/question_next_turn",{"questions":fixture[:1]})
            recovered.prompt("Recording cancellation test")
            until(lambda: recovered.questions.pending)
            rid = recovered.questions.pending["id"]
            recovered.questions.line(f"REPLY\t{rid}"); recovered.questions.line(f"ANSWER\t{rid}\t0\tC\tForbidden")
            assert recovered_lines[-1].startswith("I:ERROR\t")
            # An expired TCP resume window must not silently cancel a human's
            # still-pending question. A fresh Session can recover it by ID.
            before_aborts = len(state()["aborts"])
            recovered.alive = False
            recovered.disconnected_at = time.monotonic()-relay.RESUME_WINDOW-1
            relay.REG[recovered.key] = recovered
            replacement = relay.acquire(recovered.key)
            assert replacement is not recovered and len(state()["aborts"]) == before_aborts
            assert relay.api("GET","/question")[0]["id"] == rid
            replacement.send = recovered_lines.append; sessions.append(replacement)
            replacement.select(recovered.sid)
            assert replacement.inflight and replacement.questions.pending["id"] == rid
            replacement.abort("question test Stop")
            assert not replacement.questions.pending and not replacement.inflight
            assert recovered.sid in state()["aborts"] and len(state()["question_replies"]) == 1
            print("PASS questions: native wire, custom/multiple replies, validation, recovery, expired reconnect, continuation, cancellation; two recorded prompts, zero live inference")
        finally:
            for session in sessions: session.alive = False
            upstream.terminate(); upstream.wait(timeout=5)


if __name__ == "__main__": main()
