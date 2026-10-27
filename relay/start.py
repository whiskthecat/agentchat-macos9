#!/usr/bin/env python3
"""Start OpenCode's server and the AgentChat bridge together; stop both if either stops."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[1] / 'bridge'
children = []


def stop(*_):
    for child in reversed(children):
        if child.poll() is None:
            child.terminate()


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
# The agent's working folder, with its operating guide (AGENT_WORKSPACE, or a per-user default).
prepared = subprocess.run([sys.executable, str(BRIDGE / 'prepare_runtime.py')], check=True,
                          capture_output=True, text=True)
workspace = os.environ.setdefault('AGENT_WORKSPACE', json.loads(prepared.stdout)['workspace'])
if os.environ.get('CODEX_ENABLED') is None:
    # Codex appears in the model list once you have signed in with `codex login`.
    signed_in = os.path.exists(os.path.expanduser('~/.codex/auth.json'))
    os.environ['CODEX_ENABLED'] = '1' if shutil.which('codex') and signed_in else '0'
if os.environ.get('OPENCODE_ENABLED', '1') == '1':
    children.append(subprocess.Popen(['opencode', 'serve', '--hostname', '127.0.0.1', '--port', '7777'],
                                     cwd=workspace))
    for _ in range(120):
        try:
            socket.create_connection(('127.0.0.1', 7777), timeout=1).close()
            break
        except OSError:
            if children[0].poll() is not None:
                sys.exit('OpenCode stopped during start-up')
            time.sleep(0.5)
    else:
        sys.exit('OpenCode did not start')
children.append(subprocess.Popen([sys.executable, '-u', str(BRIDGE / 'agentd.py')], cwd=workspace))
try:
    while all(child.poll() is None for child in children):
        time.sleep(1)
finally:
    stop()
    for child in children:
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
sys.exit(1)
