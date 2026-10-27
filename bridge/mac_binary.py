"""Exact-length fork body writer. Caller owns the connection's writer lock.

Any exception after the header requires closing that connection before another
line is sent: its peer may still be consuming the declared binary body.
"""
import re
import time

TRANSPORT = "raw-fork-v1"
BUFFER_BYTES = 65536


def write_body(output, token, nonce, count, chunks):
    if (not re.fullmatch(r"AC~[0-9A-F]{12}", token) or
            not re.fullmatch(r"[0-9a-f]{32}", nonce) or
            type(count) is not int or not 1 <= count <= 64 * 1024 * 1024):
        raise ValueError("Invalid binary fork frame")
    def write(raw):
        if output.write(raw) != len(raw):
            raise OSError("Short binary connection write")
    write(("B:%s\t%s\t%d\t%s\n" % (TRANSPORT, token, count, nonce)).encode("ascii"))
    sent, started = 0, time.monotonic()
    for raw in chunks:
        if not raw or len(raw) > min(BUFFER_BYTES, count - sent):
            raise ValueError("Binary source exceeded its announced length or buffer")
        if time.monotonic() - started > 540:
            raise TimeoutError("Binary fork exceeded its transfer budget")
        write(raw)
        sent += len(raw)
    if sent != count:
        raise ValueError("Binary source ended before its announced length")
    output.flush()
