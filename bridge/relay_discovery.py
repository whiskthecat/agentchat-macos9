"""Small IPv4 LAN discovery responder; never touches a backend or the Mac.

Request: AGENTCHAT_DISCOVER 1 <8 lowercase hex digits>\n
Reply:   AGENTCHAT_RELAY 1 <same nonce> <chat TCP port> <update HTTP port>\n
The sender address is the relay address. No identity, credentials or models.
"""
from collections import OrderedDict
import os
import re
import socketserver
import threading
import time

PROBE = re.compile(rb"AGENTCHAT_DISCOVER 1 ([0-9a-f]{8})\n\Z")


class DiscoveryServer(socketserver.UDPServer):
    # One bounded, sequential worker: no thread spawned per broadcast packet.
    max_packet_size = 128

    def __init__(self, address, chat_port, update_port):
        if not 1 <= chat_port <= 65535 or not 1 <= update_port <= 65535:
            raise ValueError("Discovery requires valid chat/update ports")
        self.chat_port, self.update_port = chat_port, update_port
        self.recent = OrderedDict()
        super().__init__(address, DiscoveryHandler)


class DiscoveryHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        match = PROBE.fullmatch(data)
        if not match:
            return
        peer = self.client_address[0]
        # Never reflect to an unspecified, multicast or limited-broadcast IP.
        if not 0 < int(peer.split('.')[0]) < 224:
            return
        now = time.monotonic()
        recent = self.server.recent
        if now - recent.get(peer, -float('inf')) < 0.75:
            return
        recent[peer] = now
        recent.move_to_end(peer)
        if len(recent) > 128:
            recent.popitem(last=False)
        reply = b"AGENTCHAT_RELAY 1 %s %d %d\n" % (
            match[1], self.server.chat_port, self.server.update_port)
        try:
            sock.sendto(reply, self.client_address)
        except OSError:
            pass  # A disappearing client must not stop discovery.


def start_discovery(chat_port, log=print):
    """Return a running server, or None when disabled/unavailable.

    Defaults to the chat port's UDP counterpart, keeping recording relays on
    their own lane. Public host networking fixes DISCOVERY_PORT=8765 even when
    CHAT_PORT changes. Failure is nonfatal: explicit TCP settings still work.
    """
    if os.environ.get('AGENT_DISCOVERY', '1') != '1':
        return None
    try:
        port = int(os.environ.get('DISCOVERY_PORT', str(chat_port)))
        if not 1 <= port <= 65535:
            raise ValueError('DISCOVERY_PORT must be 1..65535')
        server = DiscoveryServer(('0.0.0.0', port), chat_port,
                                 int(os.environ.get('UPDATE_PORT', '8767')))
    except (OSError, ValueError) as error:
        log('LAN discovery unavailable: %s (manual TCP connection still works)' % error)
        return None
    threading.Thread(target=server.serve_forever, daemon=True,
                     name='relay-discovery').start()
    log('LAN discovery UDP :%d -> chat :%d' % (port, chat_port))
    return server
