#!/usr/bin/env python3
"""Real UDP and relay-startup checks. Recording backends only; never sends P:."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from relay_discovery import DiscoveryServer, start_discovery


def free_port(kind=socket.SOCK_DGRAM):
    with socket.socket(type=kind) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.server = DiscoveryServer(('127.0.0.1', 0), 12345, 12347)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sock = socket.socket(type=socket.SOCK_DGRAM)
        self.sock.settimeout(.12)
        self.addCleanup(self.sock.close)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close()

    def send(self, message):
        self.sock.sendto(message, self.server.server_address)

    def test_exact_reply_and_unicast_source(self):
        self.send(b'AGENTCHAT_DISCOVER 1 0a12beef\n')
        data, address = self.sock.recvfrom(256)
        self.assertEqual(address, self.server.server_address)
        self.assertEqual(data, b'AGENTCHAT_RELAY 1 0a12beef 12345 12347\n')

    def test_ignore_invalid_versions_nonce_extra_fields_and_oversize(self):
        for request in [b'', b'P:never', b'GET / HTTP/1.1\r\n',
                        b'AGENTCHAT_DISCOVER 2 deadbeef\n',
                        b'AGENTCHAT_DISCOVER 1 DEADbeef\n',
                        b'AGENTCHAT_DISCOVER 1 deadbeef',
                        b'AGENTCHAT_DISCOVER 1 deadbeef\nextra',
                        b'AGENTCHAT_DISCOVER 1 deadbeef\n' + b'x' * 1024]:
            self.send(request)
            with self.assertRaises(socket.timeout): self.sock.recvfrom(256)

    def test_bounded_per_host_rate_limit(self):
        self.send(b'AGENTCHAT_DISCOVER 1 00000001\n'); self.sock.recvfrom(256)
        self.send(b'AGENTCHAT_DISCOVER 1 00000002\n')
        with self.assertRaises(socket.timeout): self.sock.recvfrom(256)
        self.assertLessEqual(len(self.server.recent), 128)

    def test_off_switch_never_opens_socket(self):
        with patch.dict(os.environ, AGENT_DISCOVERY='0'), patch('relay_discovery.DiscoveryServer') as opened:
            self.assertIsNone(start_discovery(8765))
            opened.assert_not_called()

    def test_startup_ports_and_nonfatal_bind_failure(self):
        with patch.dict(os.environ, AGENT_DISCOVERY='1', DISCOVERY_PORT=str(free_port()), UPDATE_PORT='43210'):
            server = start_discovery(43211, lambda _: None)
            self.assertIsNotNone(server)
            self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
            self.sock.sendto(b'AGENTCHAT_DISCOVER 1 12345678\n', ('127.0.0.1', server.server_address[1]))
            self.assertEqual(self.sock.recvfrom(256)[0], b'AGENTCHAT_RELAY 1 12345678 43211 43210\n')
            logs = []
            self.assertIsNone(start_discovery(43211, logs.append))
            self.assertIn('manual TCP connection still works', logs[0])

    def test_agentd_integration_enabled_and_disabled(self):
        for enabled in ('1', '0'):
            with tempfile.TemporaryDirectory() as scratch:
                chat, admin, stub, udp = free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM), free_port(socket.SOCK_STREAM), free_port()
                env = dict(os.environ, AGENT_TEST_MODE='1', AGENT_STATE_FILE=scratch + '/state.sqlite3',
                           CODEX_ENABLED='0', IMAC_TOOLS_ENABLED='0', STUB_PORT=str(stub),
                           CHAT_PORT=str(chat), ADMIN_PORT=str(admin), DISCOVERY_PORT=str(udp),
                           AGENT_DISCOVERY=enabled, OPENCODE_URL='http://127.0.0.1:%d' % stub)
                root = Path(__file__).parent
                processes = []
                try:
                    processes.append(subprocess.Popen([sys.executable, str(root / 'stub_opencode.py')], env=env,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    time.sleep(.3)
                    processes.append(subprocess.Popen([sys.executable, str(root / 'agentd.py')], env=env,
                                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                    for _ in range(60):
                        try:
                            with socket.create_connection(('127.0.0.1', admin), timeout=.1): break
                        except OSError: time.sleep(.05)
                    else: self.fail('recording relay did not start')
                    self.sock.sendto(b'AGENTCHAT_DISCOVER 1 cafe1234\n', ('127.0.0.1', udp))
                    if enabled == '1':
                        # Admin starts just before the UDP listener. Tolerate
                        # that startup race/loss exactly as a client retry does.
                        for _ in range(10):
                            try:
                                reply = self.sock.recvfrom(256)[0]; break
                            except socket.timeout:
                                self.sock.sendto(b'AGENTCHAT_DISCOVER 1 cafe1234\n', ('127.0.0.1', udp))
                        else: self.fail('recording relay did not answer discovery')
                        self.assertEqual(reply, b'AGENTCHAT_RELAY 1 cafe1234 %d 8767\n' % chat)
                    else:
                        with self.assertRaises(socket.timeout): self.sock.recvfrom(256)
                    with socket.create_connection(('127.0.0.1', chat), timeout=1) as tcp:
                        self.assertTrue(tcp.recv(128).startswith(b'H:alive\n'))
                    import json, urllib.request
                    state = json.load(urllib.request.urlopen(env['OPENCODE_URL'] + '/state'))
                    self.assertEqual(state['prompts'], [])
                finally:
                    for proc in reversed(processes):
                        proc.terminate(); proc.wait(timeout=5)


if __name__ == '__main__':
    os.environ['AGENT_TEST_MODE'] = '1'
    unittest.main()
