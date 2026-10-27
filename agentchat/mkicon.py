#!/usr/bin/env python3
"""Generate AgentChat's Finder icon family (agentchat/icon.r) with PIL.

Design: a compact beige Macintosh with a chat display, black contours,
stepped shading, and separate 32/16-pixel artwork. The silhouette mask is
independent of the white message marks, so bright details stay opaque.

Resources at id 128 (the app's own Finder icon) — the full family so the
icon shows at BOTH 32x32 (large Finder view) and 16x16 (Application-menu /
small-icon Finder views), in colour on a colour desktop:
  ICN#  32x32 1-bit art + 1-bit mask  (art bit=1 -> black ink,
                                       mask bit=1 -> opaque)
  ics#  16x16 1-bit art + 1-bit mask  (also the MASK source for ics8)
  icl8  32x32 indices into the Mac 8-bit system palette
        (standard descending RGB cube: 0=white, 215=black)
  ics8  16x16 indices into the same palette (masked by ics#)
Colour icons define no mask of their own (Inside Macintosh): icl8 borrows
the ICN# mask, ics8 borrows the ics# mask — so the colour outline must
match the b/w silhouette exactly, which it does (same `bubble()` shape).

Verify visually in the VM Finder after building.
"""
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))

from PIL import Image, ImageDraw


from icon_art import application_icon, ICON_PALETTE
PALRGB = ICON_PALETTE


def bubble(big):
    return application_icon(32 if big else 16)


def bits(im_big_bool_dummy=None):
    return None


def bits1(im):
    """1-bit image -> bytes, MSB first, rows padded to a multiple of 4 bytes.

    Convention used here: a set pixel in `im` (value != 0) writes a 1 bit.
      - art: 1 bit = black ink
      - mask: 1 bit = opaque
    """
    w, h = im.size
    px = im.load()
    rowbytes = (w + 7) // 8
    out = bytearray()
    for y in range(h):
        for bx in range(rowbytes):
            v = 0
            for bit in range(8):
                x = bx * 8 + bit
                if x < w and px[x, y]:
                    v |= (1 << (7 - bit))
            out.append(v)
    return bytes(out)


def idx8(rgb):
    """RGB image -> bytes of Mac system palette indices (nearest colour)."""
    px = rgb.load()
    w, h = rgb.size
    out = bytearray()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y]
            best, bd = 0, 1 << 30
            for i, (pr, pg, pb) in enumerate(PALRGB):
                dd = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
                if dd < bd:
                    bd, best = dd, i
            out.append(255 if best == 215 else best)
    return bytes(out)


def art_from(rgb):
    """Luminance -> 1-bit art (black ink where dark, white otherwise)."""
    return rgb.convert("L").point(lambda v: 255 if v < 120 else 0, mode="1")


def rez_flat(data, perline=8):
    lines = []
    for i in range(0, len(data), perline):
        lines.append('\t$"%s"' % " ".join(
            "%02X%02X" % (data[j], data[j + 1])
            for j in range(i, min(i + perline, len(data) - 1), 2)))
    return "\n".join(lines)


def rez_pair(first, second, perline=8):
    return "\t{\n%s,\n%s\n\t}" % (rez_flat(first, perline),
                                   rez_flat(second, perline))


rgb32, mask32 = bubble(True)
rgb16, mask16 = bubble(False)

# previews --------------------------------------------------------------
# RGBA (alpha = silhouette) so the checkerboard/behind shows through
rgba32 = rgb32.convert("RGBA")
rgba32.putalpha(mask32)
rgba32.save(os.path.join(HERE, "icon_preview.png"))
rgba32.resize((128, 128), Image.NEAREST).save(
    os.path.join(HERE, "icon_preview_big.png"))
# composited over a lavender desktop so transparency is obvious at a glance
desk = Image.new("RGBA", (128, 128), (0x8E, 0x96, 0xDC, 255))
desk.alpha_composite(rgba32.resize((128, 128), Image.NEAREST), (0, 0))
desk.convert("RGB").save(os.path.join(HERE, "icon_preview_desktop.png"))

# resources -------------------------------------------------------------
# Complete Finder icon family for a single file (all id 128), per Inside
# Macintosh: Macintosh Toolbox Essentials, "Creating Icons for the Finder":
#   'ICN#'  = 32x32 b/w icon (art+mask). Colour icons get NO mask of their
#             own: the BIG colour icon (icl8) borrows the 'ICN#' mask, and the
#             SMALL colour icon (ics8) borrows the 'ics#' mask.
#   'ics#'  = 16x16 b/w icon (art+mask)  -- ALSO the mask source for ics8.
#   'icl8'  = 32x32 8-bit colour  (uses ICN# mask)
#   'ics8'  = 16x16 8-bit colour  (uses ics# mask)  <-- REQUIRED for the
#             periwinkle to show in the 16x16 Application-menu / small-icon
#             views.  Without ics8 the menu-bar slot has no colour data and
#             falls back to the near-blank 1-bit 'ics#' art (the light
#             periwinkle body contributes no ink), which is why the old
#             menu-bar icon looked like the generic blank one.
art32 = art_from(rgb32)
art16 = art_from(rgb16)
icl = idx8(rgb32)
ics8 = idx8(rgb16)
assert len(icl) == 32 * 32, len(icl)
assert len(ics8) == 16 * 16, len(ics8)

# small colour preview (16x16) so we can eyeball ics8 before building
rgba16 = rgb16.convert("RGBA")
rgba16.putalpha(mask16)
rgba16.resize((160, 160), Image.NEAREST).save(
    os.path.join(HERE, "icon_preview_small.png"))

with open(os.path.join(HERE, "icon.r"), "w") as f:
    f.write("/* AgentChat Finder icons, generated by mkicon.py — DO NOT EDIT. */\n")
    f.write('#include "Icons.r"\n\n')
    f.write("resource 'ICN#' (128, purgeable) {\n%s\n};\n\n"
            % rez_pair(bits1(art32), bits1(mask32)))
    f.write("resource 'ics#' (128, purgeable) {\n%s\n};\n\n"
            % rez_pair(bits1(art16), bits1(mask16)))
    f.write("resource 'icl8' (128, purgeable) {\n%s\n};\n\n" % rez_flat(icl))
    f.write("resource 'ics8' (128, purgeable) {\n%s\n};\n\n" % rez_flat(ics8))
    # BNDL + FREF: the Finder resolves an application / creator icon through
    # these.  With a *fresh* (deleted) Desktop DB and a valid ICN# family, the
    # file STILL showed the generic document icon on the VM — because there was
    # no BNDL tying signature 'AGCH' -> icon 128 for the 'APPL' file type.
    # Compiled BNDL layout (Inside Macintosh, "The Bundle Resource"):
    #   signature(4) | sigResID(2)=0 | arrayCount(2)=2
    #   'ICN#'(4) | iconFamilyCount(2) | (localID(2) | iconResID(2)) * n
    #   'FREF'(4)  | frefCount(2)     | (localID(2) | frefResID(2)) * n
    CREATOR = b"AGCH"
    bndl = (CREATOR + struct.pack(">h", 0) + struct.pack(">h", 2)
            + b"ICN#" + struct.pack(">h", 1) + struct.pack(">hh", 0, 128)
            + b"FREF" + struct.pack(">h", 1) + struct.pack(">hh", 0, 128))
    # FREF: file type 'APPL' (uppercase — what Finder matches for apps),
    # local ID 0 (-> icon family 0 -> ICN#/… id 128), empty comment PString.
    fref = b"APPL" + struct.pack(">h", 0) + b"\x00"
    f.write("data 'BNDL' (128, purgeable) {\n$\"%s\"\n};\n\n"
            % bndl.hex().upper())
    f.write("data 'FREF' (128, purgeable) {\n$\"%s\"\n};\n"
            % fref.hex().upper())
print("icon.r written: ICN# %dB, ics# %dB, icl8 %dB, ics8 %dB, BNDL %dB, FREF %dB"
      % (len(bits1(art32)) * 2, len(bits1(art16)) * 2, len(icl), len(ics8),
         len(bndl), len(fref)))
