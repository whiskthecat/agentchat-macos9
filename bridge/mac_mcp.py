#!/usr/bin/env python3
"""Dependency-free stdio MCP adapter for the AgentChat loopback admin service.

Use --admin-port 8868 for the VMTEST relay, 8766 for the physical G3 relay.
For manual inspection: --call imac_get_state '{}'. No inference is submitted.
"""
import argparse
import json
import socket
import sys
from remote_mac import TOOLS
from mac_context import operating_instructions


def invoke(port, name, arguments):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        # Large files and older clients' raw screenshots use many cooperative
        # native RPC chunks, particularly while physical hardware is busy.
        # Keep the longer wait specific and bounded; never retry an import.
        connection.settimeout(600 if name in {"imac_import_file", "imac_screenshot"} else 120)
        stream = connection.makefile("rwb")
        greeting = stream.readline(4096)
        if not greeting.startswith(b"agentd admin console"):
            raise RuntimeError("Unexpected relay greeting")
        request = json.dumps({"name": name, "arguments": arguments}).encode()
        if len(request) > 24 * 1024 * 1024: raise ValueError("Tool request too large")
        stream.write(b"tool " + request + b"\n"); stream.flush()
        raw = stream.readline(24 * 1024 * 1024 + 1)
        if not raw: raise RuntimeError("Relay closed the tool connection")
        return json.loads(raw)


def serve(port):
    for line in sys.stdin:
        message = {}
        try:
            message = json.loads(line)
            if not isinstance(message, dict): raise ValueError("Invalid JSON-RPC request")
            if "id" not in message: continue
            method, params = message.get("method"), message.get("params", {})
            if method == "initialize":
                requested = params.get("protocolVersion")
                version = requested if requested in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25") else "2025-11-25"
                result = {"protocolVersion": version, "serverInfo": {"name": "agentchat-imac", "version": "1.0"},
                          "capabilities": {"tools": {}}, "instructions": operating_instructions()}
            elif method == "ping": result = {}
            elif method == "tools/list": result = {"tools": TOOLS}
            elif method == "tools/call":
                try: result = invoke(port, params["name"], params.get("arguments", {}))
                except Exception as e: result = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
            else:
                print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Method not found"}}), flush=True)
                continue
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
        except Exception as e:
            print(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": -32600, "message": str(e)}}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--admin-port", type=int, default=8766)
    parser.add_argument("--call", nargs=2, metavar=("TOOL", "JSON"))
    args = parser.parse_args()
    if args.call: print(json.dumps(invoke(args.admin_port, args.call[0], json.loads(args.call[1])), ensure_ascii=False))
    else: serve(args.admin_port)
