#!/usr/bin/env python3
"""Provider long-import configuration and context checks; recording fakes only."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from codex_backend import CodexBackend
from mac_context import (MCP_TOOL_TIMEOUT_SEC, PACKAGE_ROOT, host_capabilities,
                         operating_instructions, prepare_runtime, read_guide, session_instructions)
from remote_mac import MAX_IMPORT_FILE


def codex_overrides(command):
    return dict((value.split("=", 1)[0], json.loads(value.split("=", 1)[1]))
                for flag, value in zip(command, command[1:]) if flag == "-c")


class CodexTimeoutTests(unittest.TestCase):
    def test_per_process_override_retains_startup_and_sets_bounded_tool_wait(self):
        with mock.patch.dict(os.environ, {"CODEX_COMMAND": "recording-cli app-server --stdio",
                                           "IMAC_TOOLS_ENABLED": "1", "ADMIN_PORT": "18868"}):
            backend = CodexBackend()
            self.addCleanup(backend.close)
        config = codex_overrides(backend.command)
        self.assertEqual(backend.command[:3], ["recording-cli", "app-server", "--stdio"])
        self.assertEqual(config["mcp_servers.imac.startup_timeout_sec"], 10)
        self.assertEqual(config["mcp_servers.imac.tool_timeout_sec"], 600)
        self.assertEqual(config["mcp_servers.imac.args"][-2:], ["--admin-port", "18868"])
        self.assertIsNone(backend.process)

    def test_disabled_registration_and_explicit_command_keep_existing_semantics(self):
        for command, enabled in ((None, "0"), (["explicit-recording-cli"], "1")):
            with self.subTest(command=command, enabled=enabled), mock.patch.dict(os.environ,
                    {"CODEX_COMMAND": "default-recording-cli", "IMAC_TOOLS_ENABLED": enabled}):
                backend = CodexBackend(command)
                self.addCleanup(backend.close)
                self.assertFalse(codex_overrides(backend.command))
                self.assertIsNone(backend.process)

    def test_recording_cli_receives_config_and_context_without_prompts_or_user_config_edits(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            home = Path(tmp) / "codex-home"; home.mkdir()
            user_config = home / "config.toml"
            original_config = b'model = "user-choice"\n'
            user_config.write_bytes(original_config)
            state_file = Path(tmp) / "codex.json"
            stub = Path(__file__).with_name("stub_codex.py").resolve()
            command = shlex.join([sys.executable, str(stub)])
            env = {"AGENT_TEST_MODE": "1", "CODEX_COMMAND": command, "CODEX_HOME": str(home),
                   "STUB_CODEX_STATE_FILE": str(state_file), "AGENT_WORKSPACE": str(workspace),
                   "IMAC_TOOLS_ENABLED": "1", "AGENT_QMP_SOCKET": "", "CODEX_EVENT_LOG": ""}
            with mock.patch.dict(os.environ, env):
                backend = CodexBackend()
                try:
                    # Assert the executable before starting anything. Only the
                    # shipped stdin/stdout stand-in can receive these requests.
                    self.assertEqual(backend.command[:2], [sys.executable, str(stub)])
                    self.assertTrue(backend.models())
                    backend.thread("gpt-6.1-sol")
                    state = json.loads(state_file.read_text())
                    self.assertTrue(state["test_only"])
                    self.assertEqual(state["prompts"], [])
                    setup = state["thread_setups"][0]
                    self.assertEqual(setup["developerInstructions"], session_instructions())
                    self.assertIn("imac_import_file", setup["developerInstructions"])
                    self.assertEqual(setup["cwd"], str(workspace))
                    self.assertEqual(codex_overrides(backend.command)["mcp_servers.imac.tool_timeout_sec"], 600)
                finally:
                    backend.close()
            self.assertEqual(user_config.read_bytes(), original_config)


class ContextTimeoutTests(unittest.TestCase):
    def test_context_describes_relay_host_source_and_the_real_import_limits(self):
        context = host_capabilities()
        self.assertEqual(context["raw_file_import"], {"max_bytes": MAX_IMPORT_FILE,
            "host_path_resolves_on": "relay_host", "tool_timeout_seconds": MCP_TOOL_TIMEOUT_SEC})
        self.assertEqual(MCP_TOOL_TIMEOUT_SEC, 600)
        guide = operating_instructions()
        self.assertIn("imac_import_file", guide)
        self.assertIn("expected_sha256 checks the host", guide)
        self.assertIn("create_only defaults true", guide)
        edit = read_guide("edit-files")
        self.assertIn("600-second provider timeout", edit)
        self.assertIn("Ordinary writes remain limited to 16 MiB", edit)

    def test_install_updates_owned_guides_preserves_user_edits_and_is_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "old-package"
            workspace = Path(tmp) / "workspace"
            shutil.copytree(PACKAGE_ROOT, package)
            old_core = (package / "AGENTS.md").read_text().replace("imac_import_file", "old-file-tool")
            (package / "AGENTS.md").write_text(old_core)
            with mock.patch("mac_context.PACKAGE_ROOT", package): prepare_runtime(workspace)
            custom_skill = workspace / ".agents/skills/agentchat-debug-mac/SKILL.md"
            custom_skill.write_text("User's own debugging workflow.\n")
            report = prepare_runtime(workspace)
            self.assertEqual((workspace / "AGENTS.md").read_text(), read_guide("operating"))
            self.assertIn("imac_import_file", (workspace / "AGENTS.md").read_text())
            self.assertEqual(custom_skill.read_text(), "User's own debugging workflow.\n")
            self.assertIn(str(custom_skill.relative_to(workspace)), report["preserved_local_files"])
            again = prepare_runtime(workspace)
            self.assertEqual(again, report)


class OpenCodeTimeoutTests(unittest.TestCase):
    def test_registration_uses_milliseconds_caches_success_and_never_retries_failure(self):
        # agentd owns process-wide databases, flags and providers. Isolate its
        # module import instead of changing an existing relay's globals.
        code = '''
import json, os
from unittest.mock import patch
import agentd
with patch.object(agentd, "api") as api, patch.dict(os.environ, {"IMAC_TOOLS_ENABLED":"1"}):
    api.return_value = {"imac_vmtest":{"status":"connected"}}
    agentd.ensure_imac_mcp()
    agentd.ensure_imac_mcp()
    assert api.call_count == 1
    method, endpoint, body = api.call_args.args
    assert method == "POST" and endpoint == "/mcp"
    assert body["name"] == "imac_vmtest"
    assert body["config"]["timeout"] == 600000
    assert body["config"]["command"][-2:] == ["--admin-port", "18868"]
    assert api.call_args.kwargs["timeout"] == 15
    agentd.MCP_INSTALLED = False
    api.reset_mock()
    api.return_value = {"imac_vmtest":{"status":"disconnected"}}
    try: agentd.ensure_imac_mcp()
    except RuntimeError: pass
    else: raise AssertionError("Rejected server should fail registration")
    assert api.call_count == 1 and not agentd.MCP_INSTALLED
    api.reset_mock()
    with patch.dict(os.environ, {"IMAC_TOOLS_ENABLED":"0"}): agentd.ensure_imac_mcp()
    assert api.call_count == 0
assert agentd.CODEX.process is None
print(json.dumps({"test_only":True,"prompts":0,"timeout_ms":body["config"]["timeout"]}))
'''
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENT_TEST_MODE="1", AGENT_STATE_FILE=tmp + "/state.sqlite3",
                       ADMIN_PORT="18868", AGENT_WORKSPACE=tmp + "/workspace",
                       AGENT_QMP_SOCKET="", CODEX_EVENT_LOG="")
            run = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                 env=env, capture_output=True, text=True, timeout=5, check=True)
            result = json.loads(run.stdout.strip().splitlines()[-1])
            self.assertEqual(result, {"test_only": True, "prompts": 0, "timeout_ms": 600000})


if __name__ == "__main__": unittest.main()
