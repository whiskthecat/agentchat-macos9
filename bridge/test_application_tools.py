#!/usr/bin/env python3
"""Hardware inventory and cooperating application RPC framing; no inference."""
import json
from types import SimpleNamespace
import unittest
from unittest import mock

from remote_mac import MacBroker, MacError, MacTools, TOOLS


class RecordingMac:
    def __init__(self):
        self.calls = []
        self.hardware = {"source": "native Toolbox/Gestalt/Name Registry", "machine_type": 406,
            "cpu_hz": 500000000, "bus_hz": None, "physical_ram_bytes": 536870912,
            "displays": [{"width": 800, "height": 600, "pixel_bits": 32}],
            "registry": {"entries": [{"path": "/pci/display", "properties": {
                "model": {"text": "fixture-gpu", "hex": "666978747572652d67707500"},
                "VRAM,memsize": {"hex": "01000000", "be32": 16777216}}}], "bounded": False}}
        self.reply = {"error": 0, "application_error": 0, "detail": "", "elapsed": 7,
                      "reply": '{"op":"status","version":"fixture","loaded":true}'}
        self.error = None

    def request(self, session, connection, op, **args):
        self.calls.append((op, args))
        if not session.alive or session.connection_id != connection: raise MacError("Stale connection")
        if self.error: raise self.error
        if op == "hardware": return self.hardware
        if op == "application_command": return self.reply
        raise AssertionError("Unexpected native operation: " + op)


class ApplicationToolsTests(unittest.TestCase):
    def setUp(self):
        self.broker = RecordingMac()
        self.session = SimpleNamespace(alive=True, connection_id="initial")
        self.mac = MacTools(self.broker, self.session)

    def command(self, command=None, **overrides):
        args = {"high": 0, "low": 4242, "command": {"op": "status"} if command is None else command}
        args.update(overrides)
        return self.mac.call("imac_application_command", args)["structuredContent"]

    def test_hardware_read_preserves_unknowns_raw_registry_properties_and_annotation(self):
        result = self.mac.call("imac_get_hardware", {})["structuredContent"]
        self.assertEqual(result, self.broker.hardware)
        self.assertIsNone(result["bus_hz"])
        self.assertEqual(result["registry"]["entries"][0]["properties"]["VRAM,memsize"]["hex"], "01000000")
        self.assertEqual(self.broker.calls, [("hardware", {"path": ""})])
        tool = next(tool for tool in TOOLS if tool["name"] == "imac_get_hardware")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["destructiveHint"])

    def test_application_command_sends_once_to_the_given_psn_and_parses_object_reply(self):
        result = self.command()
        self.assertEqual(result, {"high": 0, "low": 4242, "elapsed_ticks": 7,
            "reply": {"op": "status", "version": "fixture", "loaded": True}})
        self.assertEqual(self.broker.calls, [("application_command", {
            "offset": 0, "count": 4242, "data": b'{"op":"status"}'})])

    def test_unicode_uses_ascii_json_escapes_without_loss(self):
        command = {"op": "title", "title": "Caf\u00e9 \u6f22\U0001f642"}
        self.broker.reply["reply"] = json.dumps(command, ensure_ascii=True)
        result = self.command(command)
        wire = self.broker.calls[0][1]["data"]
        self.assertTrue(wire.isascii())
        self.assertEqual(json.loads(wire), command)
        self.assertEqual(result["reply"], command)

    def test_exact1024byte_command_is_admitted_but_larger_commands_never_reach_guest(self):
        self.command({"s": "x"*1016})
        self.assertEqual(len(self.broker.calls[0][1]["data"]), 1024)
        self.broker.calls.clear()
        for command in ({"s": "x"*1017}, {"s": "\u6f22"*170}):
            with self.subTest(command_size=len(str(command))):
                with self.assertRaisesRegex(ValueError, "1024"): self.command(command)
        self.assertEqual(self.broker.calls, [])

    def test_nonobject_nonjson_nonfinite_and_invalid_psn_arguments_never_send(self):
        for command in ([], "status", 5, {"v": object()}, {"v": {1, 2}}, {"v": float("nan")}, {"v": float("inf")}):
            with self.subTest(command=repr(command)):
                with self.assertRaises(ValueError): self.command(command)
        for fields in ({"high": -1}, {"low": 2147483648}, {"high": True}, {"low": "4242"}, {"unexpected": True}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValueError): self.command(**fields)
        self.assertEqual(self.broker.calls, [])

    def test_full_signed32bit_application_error_is_preserved_in_message(self):
        for error in (70001, -70001, 2147483647, -2147483648):
            with self.subTest(error=error):
                self.broker.reply.update(error=-10000, application_error=error, detail="Receiver error")
                with self.assertRaisesRegex(MacError, "application error " + str(error)): self.command()
        self.assertEqual(len(self.broker.calls), 4)

    def test_unhandled_timeout_and_canceled_commands_are_not_retried(self):
        for error, detail in ((-1708, "Application has no AGCT/cmnd command handler"),
                (-1712, "Application reply timed out"), (-1711, "Application wait canceled")):
            with self.subTest(error=error):
                self.broker.calls.clear()
                self.broker.reply.update(error=error, detail=detail)
                with self.assertRaises(MacError) as caught: self.command()
                self.assertIn(detail, str(caught.exception))
                if error != -1708: self.assertIn("may have executed", str(caught.exception))
                self.assertEqual(len(self.broker.calls), 1)

    def test_transport_failure_states_execution_uncertainty_and_never_retries(self):
        self.broker.error = MacError("Macintosh disconnected")
        with self.assertRaisesRegex(MacError, "may have executed"): self.command()
        self.assertEqual(len(self.broker.calls), 1)

    def test_reply_requires_bounded_ascii_object_and_strict_json_numbers(self):
        for text in ("", "x"*4097, '{"x":"\u00e9"}', '{"x":"\x00"}', "[]", "true", "null", "5", "{broken", '{"x":NaN}', '{"x":Infinity}'):
            with self.subTest(text=text[:30]):
                self.broker.reply["reply"] = text
                with self.assertRaises(MacError): self.command()
        self.assertEqual(len(self.broker.calls), 11)

    def test_exact4096byte_reply_is_admitted(self):
        self.broker.reply["reply"] = '{"s":"' + "x"*4088 + '"}'
        self.assertEqual(len(self.broker.reply["reply"]), 4096)
        self.assertEqual(len(self.command()["reply"]["s"]), 4088)

    def test_invalid_error_timing_or_detail_envelopes_are_rejected(self):
        original = dict(self.broker.reply)
        for fields in ({"error": True}, {"error": 40000}, {"application_error": 2147483648},
                {"application_error": 0.0}, {"elapsed": -1}, {"elapsed": True}, {"elapsed": 4294967296},
                {"detail": "x"*192}, {"detail": []}):
            with self.subTest(fields=fields):
                self.broker.reply = dict(original, **fields)
                with self.assertRaises(MacError): self.command()

    def test_broker_application_wait_has_margin_while_ordinary_wait_is_unchanged(self):
        session = SimpleNamespace(alive=True, connection_id="initial", send=mock.Mock())
        for op, timeout in (("application_command", 20), ("hardware", 15), ("state", 15)):
            with self.subTest(op=op), mock.patch("remote_mac.queue.Queue") as queue:
                queue.return_value.get.return_value = {"ok": True}
                broker = MacBroker()
                self.assertEqual(broker.request(session, "initial", op), {"ok": True})
                queue.return_value.get.assert_called_once_with(timeout=timeout)
                self.assertEqual(broker.pending, {})
        self.assertEqual(session.send.call_count, 3)


if __name__ == "__main__": unittest.main()
