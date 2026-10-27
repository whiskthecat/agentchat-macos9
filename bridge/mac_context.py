"""Shared, versioned operating guidance for every AgentChat backend.

No backend request, inference, guest mutation or user-config rewrite occurs here.
MCP guide reads are a portable fallback when a host cannot discover local skills.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import uuid

PACKAGE_VERSION = "1"
MCP_TOOL_TIMEOUT_SEC = 600
PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "agent_runtime"
GUIDES = {
    "install-software": "agentchat-install-software",
    "edit-files": "agentchat-edit-files",
    "debug-mac": "agentchat-debug-mac",
}


def read_guide(guide):
    if guide == "operating":
        path = PACKAGE_ROOT / "AGENTS.md"
    elif guide in GUIDES:
        path = PACKAGE_ROOT / ".agents" / "skills" / GUIDES[guide] / "SKILL.md"
    else:
        raise ValueError("Unknown guide; use operating, " + ", ".join(GUIDES))
    text = path.read_text()
    if len(text.encode()) > 24 * 1024: raise ValueError("Operating guide exceeds its context limit")
    return text


def guide_catalog():
    result = []
    for guide, skill in GUIDES.items():
        text = read_guide(guide)
        description = re.search(r"^description: (.+)$", text, re.MULTILINE)
        result.append({"guide": guide, "skill": skill, "description": description.group(1) if description else skill})
    return result


def operating_instructions():
    catalog = "\n".join("- %s: %s" % (g["guide"], g["description"]) for g in guide_catalog())
    return read_guide("operating").rstrip() + "\n\nAvailable operating guides (imac_read_guide):\n" + catalog


def host_capabilities():
    archive = bool(shutil.which("unar") and shutil.which("lsar"))
    return {"runtime_version": PACKAGE_VERSION,
            "automatic_mcp_registration": os.environ.get("IMAC_TOOLS_ENABLED", "1") == "1",
            "vm_tools_enabled": bool(os.environ.get("AGENT_QMP_SOCKET")),
            "archive_tools_available": archive,
            "raw_file_import": {"max_bytes": 64 * 1024 * 1024, "host_path_resolves_on": "relay_host",
                                "tool_timeout_seconds": MCP_TOOL_TIMEOUT_SEC},
            "desktop_capture": {"preferred_transport": "packbits-planar-xrgb", "legacy_raw_fallback": True,
                                "tool_timeout_seconds": MCP_TOOL_TIMEOUT_SEC},
            "relay_test_mode": os.environ.get("AGENT_TEST_MODE", "0") == "1",
            "archive_formats": "Installed unar/lsar recognition, including StuffIt, BinHex and MacBinary" if archive else None}


def session_instructions():
    # Facts describe configured host capabilities, never cached guest volume names.
    return operating_instructions() + "\n\nRelay capabilities at turn setup:\n" + json.dumps(host_capabilities(), sort_keys=True)


def runtime_workspace():
    configured = os.environ.get("AGENT_WORKSPACE")
    if configured: return Path(configured).expanduser().resolve()
    if os.environ.get("AGENT_TEST_MODE") == "1":
        state = os.environ.get("AGENT_STATE_FILE")
        if not state: raise ValueError("Recording runtime requires an isolated AGENT_STATE_FILE")
        return Path(state).resolve().parent / "agent-workspace"
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return (data / "agentchat" / "workspace").resolve()


def prepare_runtime(workspace=None):
    """Install package-owned files; preserve existing or locally edited guidance."""
    workspace = Path(workspace).resolve() if workspace is not None else runtime_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / ".agentchat" / "runtime.json"
    if not marker.resolve().is_relative_to(workspace): raise ValueError("Runtime marker escapes workspace")
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    old_hashes = previous.get("files", {})
    hashes = {}; preserved = []
    sources = [PACKAGE_ROOT / "AGENTS.md"] + [PACKAGE_ROOT / ".agents" / "skills" / s / "SKILL.md" for s in GUIDES.values()]
    for source in sources:
        relative = str(source.relative_to(PACKAGE_ROOT))
        target = workspace / relative
        content = source.read_bytes(); digest = hashlib.sha256(content).hexdigest()
        if not target.resolve().is_relative_to(workspace):
            preserved.append(relative); continue
        if target.exists():
            current = hashlib.sha256(target.read_bytes()).hexdigest()
            if current == digest:
                hashes[relative] = digest; continue
            if current != digest and current != old_hashes.get(relative):
                preserved.append(relative)
                if relative in old_hashes: hashes[relative] = old_hashes[relative]
                continue
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".agentchat-" + uuid.uuid4().hex)
        try:
            temporary.write_bytes(content); temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        hashes[relative] = digest
    marker.parent.mkdir(parents=True, exist_ok=True)
    report = {"version": PACKAGE_VERSION, "workspace": str(workspace), "files": hashes, "preserved_local_files": preserved}
    temporary = marker.with_name("runtime-" + uuid.uuid4().hex + ".json")
    try:
        temporary.write_text(json.dumps(report, indent=2)); temporary.replace(marker)
    finally:
        temporary.unlink(missing_ok=True)
    return report
