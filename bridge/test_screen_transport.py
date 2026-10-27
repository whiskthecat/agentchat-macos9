#!/usr/bin/env python3
"""Exact-color screenshot transport checks with native RPC fakes; no inference."""
import base64
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image
from mac_mcp import invoke
from remote_mac import (MAX_SCREEN_BYTES, SCREEN_CHUNK, SCREEN_TRANSPORT,
                        MacError, MacTools, unpack_screen_bits)


def fixture_packbits(raw):
    """Use libtiff's independent PackBits producer for the wire fixtures."""
    planar = b"".join(raw[channel::4] for channel in range(4))
    out = io.BytesIO()
    Image.frombytes("L", (len(planar), 1), planar).save(out, format="TIFF", compression="packbits")
    tiff = out.getvalue()
    with Image.open(io.BytesIO(tiff)) as image:
        offsets, lengths = image.tag_v2[273], image.tag_v2[279]
    if isinstance(offsets, int): offsets = (offsets,)
    if isinstance(lengths, int): lengths = (lengths,)
    return b"".join(tiff[offset:offset+length] for offset, length in zip(offsets, lengths))


class ScreenMac:
    def __init__(self, raw, width, height, stride, compressed=True):
        self.calls = []
        self.ended = 0
        self.payload = fixture_packbits(raw) if compressed else raw
        self.meta = {"width": width, "height": height, "stride": stride, "format": "XRGB", "token": "123"}
        if compressed:
            self.meta.update(transport=SCREEN_TRANSPORT, transport_bytes=len(self.payload))
        self.change_response = None
        self.cleanup_fails = False

    def request(self, session, connection, op, **args):
        self.calls.append((op, args))
        if not session.alive or session.connection_id != connection: raise MacError("Stale Macintosh connection")
        if op == "screen_begin":
            assert args == {"path": SCREEN_TRANSPORT}
            return dict(self.meta)
        if op == "screen_end":
            self.ended += 1
            if self.cleanup_fails: raise MacError("Cleanup failed")
            return {"released": True}
        if op == "screen_read":
            assert args["path"] == self.meta["token"] and 1 <= args["count"] <= SCREEN_CHUNK
            raw = self.payload[args["offset"]:args["offset"]+args["count"]]
            reply = {"total": len(self.payload), "data": base64.b64encode(raw).decode()}
            return self.change_response(reply) if self.change_response else reply
        raise AssertionError("Unexpected operation: " + op)


class PackBitsTests(unittest.TestCase):
    def test_literal_run_noop_and_signed_control_boundaries(self):
        vectors = [(b"\x02abc", b"abc"), (b"\xfeZ", b"ZZZ"), (b"\x80\x00A\x80", b"A"),
                   (bytes([127])+bytes(range(128)), bytes(range(128))), (b"\x81\xab", b"\xab"*128)]
        for packed, expected in vectors:
            with self.subTest(packed=packed[:4]):
                self.assertEqual(unpack_screen_bits(packed, len(expected)), expected)

    def test_truncated_literals_runs_inflation_and_incomplete_output_are_rejected(self):
        for packed, expected in ((b"\x02ab", 3), (b"\xff", 2), (b"\x81X", 10), (b"\x00A", 2), (b"\x80"*100, 1)):
            with self.subTest(packed=packed[:4]):
                with self.assertRaises(MacError): unpack_screen_bits(packed, expected)

    def test_expanded_and_packed_limits_are_enforced(self):
        for expected in (0, -1, MAX_SCREEN_BYTES+1, True, "4"):
            with self.subTest(expected=expected):
                with self.assertRaises(MacError): unpack_screen_bits(b"\x00A", expected)
        with self.assertRaises(MacError): unpack_screen_bits(bytes(MAX_SCREEN_BYTES+1), 4)


class ScreenshotTests(unittest.TestCase):
    def make_mac(self, raw, width, height, stride, compressed=True):
        broker = ScreenMac(raw, width, height, stride, compressed)
        session = SimpleNamespace(alive=True, connection_id="initial")
        return MacTools(broker, session), broker

    def image(self, mac):
        result = mac.call("imac_screenshot", {})
        content = result["content"][0]
        self.assertEqual(content["mimeType"], "image/png")
        return Image.open(io.BytesIO(base64.b64decode(content["data"]))).convert("RGB")

    def test_compressed_and_legacy_captures_preserve_colors_and_ignore_padding(self):
        width, height, stride = 12, 5, 52
        rows = []
        expected = bytearray()
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (17, 53, 199), (211, 211, 211)]
        for y, (r, g, b) in enumerate(colors):
            rows.append(bytes([y+1, r, g, b])*width + bytes([71, 72, 73, 74]))
            expected.extend(bytes([r, g, b])*width)
        raw = b"".join(rows)
        packed = fixture_packbits(raw)
        # The negotiated transport preserves the unused X and padding too.
        planar = unpack_screen_bits(packed, len(raw))
        restored = bytearray(len(raw))
        for channel in range(4): restored[channel::4] = planar[channel*(len(raw)//4):(channel+1)*(len(raw)//4)]
        self.assertEqual(restored, raw)
        for compressed in (True, False):
            with self.subTest(compressed=compressed):
                mac, broker = self.make_mac(raw, width, height, stride, compressed)
                actual = self.image(mac)
                self.assertEqual(actual.size, (width, height))
                self.assertEqual(actual.tobytes(), expected)
                self.assertEqual(broker.ended, 1)

    def test_large_flat_full_color_desktop_needs_far_fewer_rpc_reads(self):
        width, height = 800, 600
        raw = bytes([0, 240, 241, 242]) * (width*height)
        mac, broker = self.make_mac(raw, width, height, width*4)
        image = self.image(mac)
        self.assertEqual(image.tobytes(), bytes([240, 241, 242]) * (width*height))
        compressed_reads = len([op for op, _ in broker.calls if op == "screen_read"])
        raw_reads = (len(raw)+SCREEN_CHUNK-1)//SCREEN_CHUNK
        self.assertLess(compressed_reads, raw_reads//20)
        self.assertEqual(broker.ended, 1)

    def test_legacy_raw_multi_chunk_capture_uses_exact_offsets_and_lengths(self):
        raw = bytes([0, 15, 75, 150]) * (120*40)
        mac, broker = self.make_mac(raw, 120, 40, 480, compressed=False)
        self.assertEqual(self.image(mac).tobytes(), bytes([15, 75, 150])*(120*40))
        reads = [args for op, args in broker.calls if op == "screen_read"]
        self.assertEqual([args["offset"] for args in reads], [0, SCREEN_CHUNK])
        self.assertEqual([args["count"] for args in reads], [SCREEN_CHUNK, len(raw)-SCREEN_CHUNK])

    def test_explicit_raw_fallback_and_capture_transport_metrics(self):
        raw = bytes([0, 10, 20, 30])*16
        mac, broker = self.make_mac(raw, 4, 4, 16, compressed=False)
        broker.meta.update(transport="raw", transport_bytes=len(raw))
        result = mac.call("imac_screenshot", {})
        self.assertEqual(result["structuredContent"], {"width": 4, "height": 4, "stride": 16,
            "transport": "raw", "transport_bytes": 64, "unpacked_bytes": 64, "read_chunks": 1})
        self.assertEqual(broker.ended, 1)

    def test_unknown_transport_invalid_dimensions_and_metadata_always_release(self):
        cases = [{"width": 0}, {"width": True}, {"height": -1}, {"stride": 3}, {"format": "RGBA"},
                 {"width": 4096, "height": 8192, "stride": 16380}, {"transport": "unknown"},
                 {"transport_bytes": -1}, {"transport_bytes": True}, {"transport_bytes": 999}, {"token": ""}]
        for changes in cases:
            with self.subTest(changes=changes):
                mac, broker = self.make_mac(bytes([0, 1, 2, 3])*16, 4, 4, 16)
                broker.meta.update(changes)
                with self.assertRaises(MacError): self.image(mac)
                self.assertEqual(broker.ended, 1)
                self.assertNotIn("screen_read", [op for op, _ in broker.calls])

    def test_empty_oversized_inconsistent_and_malformed_chunks_release_snapshot(self):
        changes = [lambda part: dict(part, data=""), lambda part: dict(part, data="!invalid!"),
                   lambda part: dict(part, total=part["total"]+1),
                   lambda part: dict(part, data=base64.b64encode(b"x"*100).decode()),
                   lambda part: dict(part, data=base64.b64encode(b"x"*(SCREEN_CHUNK+1)).decode())]
        for change in changes:
            with self.subTest(change=change):
                mac, broker = self.make_mac(bytes([0, 1, 2, 3])*16, 4, 4, 16)
                broker.change_response = change
                with self.assertRaises(MacError): self.image(mac)
                self.assertEqual(broker.ended, 1)

    def test_malformed_packbits_releases_snapshot_after_transport_completes(self):
        mac, broker = self.make_mac(bytes([0, 1, 2, 3])*16, 4, 4, 16)
        broker.payload = b"\x81A"  # Expands to 128 bytes; frame has only 64.
        broker.meta["transport_bytes"] = len(broker.payload)
        with self.assertRaisesRegex(MacError, "declared size"): self.image(mac)
        self.assertEqual(broker.ended, 1)

    def test_cleanup_failure_does_not_hide_primary_decode_error(self):
        mac, broker = self.make_mac(bytes([0, 1, 2, 3])*16, 4, 4, 16)
        broker.payload = b"\x81A"; broker.meta["transport_bytes"] = len(broker.payload)
        broker.cleanup_fails = True
        with self.assertRaisesRegex(MacError, "declared size"): self.image(mac)
        self.assertEqual(broker.ended, 1)

    def test_disconnect_cannot_continue_capture_on_a_replacement_connection(self):
        raw = bytes([0, 10, 20, 30])*(120*40)
        mac, broker = self.make_mac(raw, 120, 40, 480, compressed=False)
        broker.change_response = lambda part: (setattr(mac.session, "connection_id", "replacement"), part)[1]
        with self.assertRaisesRegex(MacError, "Stale Macintosh"): self.image(mac)
        self.assertEqual([op for op, _ in broker.calls], ["screen_begin", "screen_read", "screen_read", "screen_end"])

    def test_capture_deadline_releases_snapshot_before_outer600second_timeout(self):
        raw = bytes([0, 10, 20, 30])*(120*40)
        mac, broker = self.make_mac(raw, 120, 40, 480, compressed=False)
        with mock.patch("remote_mac.time.monotonic", side_effect=[0, 1, 540]):
            with self.assertRaisesRegex(MacError, "540-second capture budget"): self.image(mac)
        self.assertEqual([op for op, _ in broker.calls], ["screen_begin", "screen_read", "screen_end"])
        self.assertEqual(broker.ended, 1)

    def test_screenshot_and_import_have_bounded_long_wait_other_calls_keep120seconds(self):
        for name, seconds in (("imac_screenshot", 600), ("imac_import_file", 600), ("imac_read_file", 120)):
            with self.subTest(name=name):
                connection = mock.MagicMock(); connection.__enter__.return_value = connection
                connection.makefile.return_value.readline.side_effect = [b"agentd admin console\n", b"{}\n"]
                with mock.patch("mac_mcp.socket.create_connection", return_value=connection): invoke(1, name, {})
                connection.settimeout.assert_called_once_with(seconds)


if __name__ == "__main__": unittest.main()
