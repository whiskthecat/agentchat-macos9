"""Shared runtime, offline MCP and backend instruction checks; fakes only."""
import json
import os
from pathlib import Path
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.request

from mac_context import PACKAGE_ROOT, operating_instructions, prepare_runtime, read_guide, runtime_workspace, session_instructions
from remote_mac import MacTools


class RuntimeTests(unittest.TestCase):
    def test_installer_preserves_local_edits_and_updates_owned_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"; package = Path(tmp) / "package"
            shutil.copytree(PACKAGE_ROOT, package)
            with patch("mac_context.PACKAGE_ROOT", package):
                report = prepare_runtime(workspace)
                self.assertEqual(len(report["files"]), 4)
                (workspace / "AGENTS.md").write_text("user instructions")
                skill = ".agents/skills/agentchat-edit-files/SKILL.md"
                (package / skill).write_text((package / skill).read_text() + "\nUpdated workflow.\n")
                report = prepare_runtime(workspace)
                self.assertEqual((workspace / "AGENTS.md").read_text(), "user instructions")
                self.assertEqual((workspace / skill).read_text(), (package / skill).read_text())
                self.assertIn("AGENTS.md", report["preserved_local_files"])
                (workspace / skill).write_text("local workflow")
                prepare_runtime(workspace)
                self.assertEqual((workspace / skill).read_text(), "local workflow")

    def test_existing_guidance_and_external_symlinks_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "work"; workspace.mkdir()
            external = Path(tmp) / "outside"; external.mkdir()
            (workspace / "AGENTS.md").write_text("existing rules")
            (workspace / ".agents").symlink_to(external, target_is_directory=True)
            report = prepare_runtime(workspace)
            self.assertEqual((workspace / "AGENTS.md").read_text(), "existing rules")
            self.assertEqual(list(external.iterdir()), [])
            self.assertEqual(len(report["preserved_local_files"]), 4)

    def test_recording_workspace_is_isolated(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"AGENT_TEST_MODE": "1", "AGENT_STATE_FILE": tmp + "/state.sqlite3"}):
            with patch.dict(os.environ):
                os.environ.pop("AGENT_WORKSPACE", None)
                self.assertEqual(runtime_workspace(), Path(tmp) / "agent-workspace")

    def test_relocated_package_installs_without_a_repository_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            install = Path(tmp) / "installed"; bridge = install / "bridge"; bridge.mkdir(parents=True)
            for name in ("mac_context.py", "prepare_runtime.py"):
                shutil.copyfile(Path(__file__).with_name(name), bridge / name)
            shutil.copytree(PACKAGE_ROOT, install / "agent_runtime")
            workspace = Path(tmp) / "persistent workspace"
            run = subprocess.run([sys.executable, str(bridge / "prepare_runtime.py"), "--workspace", str(workspace)],
                                 cwd=tmp, text=True, capture_output=True, timeout=5, check=True)
            report = json.loads(run.stdout)
            self.assertEqual(len(report["files"]), 4)
            self.assertEqual((workspace / "AGENTS.md").read_text(), read_guide("operating"))

    def test_offline_guides_and_context_need_no_native_requests(self):
        tools = MacTools(None, None)
        with patch.dict(os.environ, {"AGENT_QMP_SOCKET": ""}):
            context = tools.call("imac_get_context", {})["structuredContent"]
        self.assertFalse(context["connected"])
        self.assertFalse(context["native_tools_ready"])
        self.assertEqual({g["guide"] for g in context["guides"]}, {"install-software", "edit-files", "debug-mac"})
        self.assertNotIn("imac_read_file", context["available_tools"])
        guide = tools.call("imac_read_guide", {"guide": "install-software"})["structuredContent"]
        self.assertEqual(guide["text"], read_guide("install-software"))
        with self.assertRaises(ValueError): tools.call("imac_read_guide", {"guide": "../../AGENTS.md"})

    def test_live_context_uses_current_volume_names_and_skips_paused_rpc(self):
        tools = MacTools(None, SimpleNamespace(alive=True, connection_id="fixture"))
        calls = []
        def rpc(op):
            calls.append(op)
            return {"version": "fixture", "conversation": "fixture-chat"} if op == "state" else {"volumes": [{"name": "User disk"}]}
        tools.rpc = rpc
        with patch.dict(os.environ, {"AGENT_QMP_SOCKET": ""}):
            context = tools.call("imac_get_context", {})["structuredContent"]
        self.assertEqual(calls, ["state", "volumes"])
        self.assertEqual(context["volumes"][0]["name"], "User disk")
        self.assertTrue(context["native_tools_ready"])
        calls.clear()
        with patch.dict(os.environ, {"AGENT_QMP_SOCKET": "missing"}), patch("remote_mac.VM_DEBUG.call", side_effect=FileNotFoundError("missing socket")):
            context = tools.call("imac_get_context", {})["structuredContent"]
        self.assertTrue(context["native_tools_ready"])
        self.assertIn("vm_error", context)
        calls.clear()
        with patch.dict(os.environ, {"AGENT_QMP_SOCKET": "fixture"}), patch("remote_mac.VM_DEBUG.call", return_value={"running": False, "status": "paused"}):
            context = tools.call("imac_get_context", {})["structuredContent"]
        self.assertEqual(calls, [])
        self.assertFalse(context["native_tools_ready"])
        self.assertIn("imac_vm_resume", context["available_tools"])
        self.assertNotIn("imac_read_file", context["available_tools"])

    def test_stdio_initialization_contains_shared_guidance_without_upstream(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        response = subprocess.run([sys.executable, str(Path(__file__).with_name("mac_mcp.py")), "--admin-port", "1"],
            input=json.dumps(request) + "\n", text=True, capture_output=True, timeout=5, check=True)
        self.assertEqual(json.loads(response.stdout)["result"]["instructions"], operating_instructions())


class BackendTests(unittest.TestCase):
    def test_both_backends_receive_same_context_without_changing_user_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
            env = {"AGENT_TEST_MODE": "1", "OPENCODE_URL": "http://127.0.0.1:%d" % port,
                   "STUB_PORT": str(port),
                   "CODEX_ENABLED": "1", "AGENT_WORKSPACE": tmp + "/workspace", "AGENT_QMP_SOCKET": "",
                   "STUB_STATE_FILE": "",
                   "AGENT_STATE_FILE": tmp + "/state.sqlite3", "STUB_CODEX_STATE_FILE": tmp + "/codex.json"}
            with patch.dict(os.environ, env):
                upstream = subprocess.Popen([sys.executable, str(Path(__file__).with_name("stub_opencode.py")), str(port)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                relay = None; session = None
                try:
                    for _ in range(100):
                        try:
                            with urllib.request.urlopen(env["OPENCODE_URL"] + "/state", timeout=.2) as r: json.load(r)
                            break
                        except OSError: time.sleep(.03)
                    else: self.fail("Recording upstream did not start")
                    import agentd as relay
                    session = relay.Session("runtime-fixture", lambda line: None)
                    session.prompt("install the requested game")
                    session.prompt("do not duplicate this busy turn")
                    with urllib.request.urlopen(env["OPENCODE_URL"] + "/state", timeout=2) as r: state = json.load(r)
                    self.assertEqual([p["text"] for p in state["prompts"]], ["install the requested game"])
                    self.assertEqual(state["instructions"][0]["system"], session_instructions())
                    tid = relay.CODEX.thread("gpt-6.1-sol")
                    resumed = relay.CODEX.thread("gpt-6.1-sol", "fixture-resumed-thread")
                    codex = json.loads(Path(env["STUB_CODEX_STATE_FILE"]).read_text())
                    self.assertEqual(codex["prompts"], [])
                    self.assertEqual([s["method"] for s in codex["thread_setups"]], ["thread/start", "thread/resume"])
                    for setup in codex["thread_setups"]:
                        self.assertEqual(setup["developerInstructions"], state["instructions"][0]["system"])
                        self.assertEqual(setup["cwd"], env["AGENT_WORKSPACE"])
                    self.assertTrue(tid); self.assertEqual(resumed, "fixture-resumed-thread")
                    self.assertEqual(len(list((Path(env["AGENT_WORKSPACE"]) / ".agents/skills").glob("*/SKILL.md"))), 3)
                    # Host documentation/context must remain readable before a Mac connects.
                    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), relay.AdminHandler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    try:
                        from mac_mcp import invoke
                        response = invoke(server.server_address[1], "imac_read_guide", {"guide": "operating"})
                        self.assertEqual(response["structuredContent"]["text"], read_guide("operating"))
                        context = invoke(server.server_address[1], "imac_get_context", {})["structuredContent"]
                        self.assertFalse(context["connected"])
                        self.assertTrue(context["relay_test_mode"])
                        import zipfile
                        archive = Path(tmp) / "offline.zip"
                        with zipfile.ZipFile(archive, "w") as z: z.writestr("Document", b"fixture")
                        inventory = invoke(server.server_address[1], "imac_inspect_archive", {"host_path": str(archive)})
                        self.assertEqual(inventory["structuredContent"]["entries"][0]["data_bytes"], 7)
                    finally:
                        server.shutdown(); server.server_close()
                finally:
                    if relay:
                        if session: session.alive = False
                        relay.CODEX.close()
                    upstream.terminate(); upstream.wait(timeout=5)


if __name__ == "__main__": unittest.main()
