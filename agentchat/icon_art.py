"""Pixel-fitted Classic Mac artwork; independent opaque silhouettes."""
from PIL import Image, ImageDraw
import math

NAVY = (0, 0, 0)
BLUE = (51, 102, 204)
LIGHT_BLUE = (153, 204, 255)
WHITE = (255, 255, 255)
PAPER = (255, 255, 255)
SILVER = (204, 204, 204)
GRAY = (153, 153, 153)
GREEN = (51, 153, 102)
MINT = (153, 204, 153)

# The Classic icon palette starts with white, then the descending 6x6x6 RGB
# cube. Its final black corner is at index 255: index 215 starts the red ramp.
# Generators map that corner explicitly; the other cube indices stay 0..214.
ICON_PALETTE = [(r, g, b) for r in (255, 204, 153, 102, 51, 0)
                for g in (255, 204, 153, 102, 51, 0)
                for b in (255, 204, 153, 102, 51, 0)]


def canvas(size):
    return Image.new("RGB", (size, size), WHITE), Image.new("L", (size, size), 0)


def shape(rgb, mask, points, fill):
    ImageDraw.Draw(mask).polygon(points, fill=255)
    d = ImageDraw.Draw(rgb)
    d.polygon(points, fill=fill)
    d.line(points + [points[0]], fill=NAVY, width=1)


def application_icon(size):
    rgb, mask = canvas(size)
    d = ImageDraw.Draw(rgb)
    beige, shade = (255, 255, 204), (153, 153, 102)
    if size == 32:
        # Compact Mac: an asymmetric case and stepped shadow, separately
        # fitted screen marks. Original artwork, studied from OS 9 references.
        shape(rgb, mask, [(7, 3), (24, 3), (28, 7), (28, 27), (25, 30),
                          (5, 30), (3, 27), (3, 7)], GRAY)
        shape(rgb, mask, [(7, 3), (24, 3), (24, 26), (21, 28),
                          (4, 28), (3, 26), (3, 7)], beige)
        d.line((7, 4, 23, 4), fill=WHITE)
        d.line((4, 8, 4, 26), fill=WHITE)
        d.polygon([(25, 6), (27, 8), (27, 26), (25, 28)], fill=shade)
        d.rectangle((6, 7, 21, 20), fill=NAVY)
        d.rectangle((7, 8, 20, 19), fill=SILVER)
        d.rectangle((8, 9, 20, 18), fill=WHITE)
        d.rectangle((9, 10, 18, 11), fill=BLUE)
        d.line((9, 13, 17, 13), fill=(102, 102, 102))
        d.line((9, 15, 15, 15), fill=(102, 102, 102))
        d.line((17, 17, 19, 17), fill=BLUE)
        d.line((13, 23, 21, 23), fill=NAVY)
        d.line((14, 24, 21, 24), fill=WHITE)
        d.rectangle((6, 24, 8, 25), fill=BLUE)
        d.line((6, 29, 23, 29), fill=NAVY)
    else:
        shape(rgb, mask, [(3, 1), (12, 1), (14, 3), (14, 13),
                          (12, 15), (2, 15), (1, 13), (1, 3)], GRAY)
        shape(rgb, mask, [(3, 1), (11, 1), (11, 13), (2, 13), (1, 12), (1, 3)], beige)
        d.line((3, 2, 10, 2), fill=WHITE)
        d.rectangle((3, 3, 9, 9), fill=NAVY)
        d.rectangle((4, 4, 8, 8), fill=WHITE)
        d.line((4, 5, 8, 5), fill=BLUE)
        d.line((4, 7, 7, 7), fill=(102, 102, 102))
        d.line((6, 11, 10, 11), fill=NAVY)
        d.point((3, 11), fill=BLUE)
    return rgb, mask


def navigation_icon(kind):
    rgb, mask = canvas(32)
    d = ImageDraw.Draw(rgb)
    if kind == "connect":
        shape(rgb, mask, [(5, 15), (5, 10), (8, 6), (13, 4), (20, 4),
                          (20, 1), (28, 7), (20, 13), (20, 10), (14, 10),
                          (11, 11), (10, 15)], MINT)
        d.line([(7, 14), (7, 10), (9, 8), (13, 6), (20, 6)], fill=WHITE)
        shape(rgb, mask, [(26, 17), (26, 22), (23, 26), (18, 28), (11, 28),
                          (11, 31), (3, 25), (11, 19), (11, 22), (17, 22),
                          (20, 21), (21, 17)], GREEN)
        d.line([(24, 18), (24, 22), (21, 25), (17, 26), (11, 26)], fill=MINT)
    elif kind == "new":
        shape(rgb, mask, [(6, 2), (21, 2), (27, 8), (27, 29), (6, 29)], GRAY)
        shape(rgb, mask, [(4, 1), (20, 1), (25, 7), (25, 27), (4, 27)], PAPER)
        d.polygon([(20, 2), (20, 7), (24, 7)], fill=SILVER)
        d.line((20, 2, 20, 7, 24, 7), fill=NAVY)
        for y, right in ((10, 20), (13, 20), (16, 18), (19, 16)):
            d.line((7, y, right, y), fill=GRAY)
        shape(rgb, mask, [(11, 26), (13, 19), (26, 6), (30, 10), (17, 23)], (255, 204, 51))
        d.line((15, 20, 27, 8), fill=WHITE)
        d.line((17, 22, 29, 10), fill=(153, 102, 0))
        d.polygon([(11, 26), (13, 20), (17, 23)], fill=(255, 204, 153))
        d.line((11, 26, 13, 24), fill=NAVY)
        d.line((25, 7, 28, 10), fill=NAVY)
    else:
        # A Platinum control panel, as in the system's own Finder icons.
        shape(rgb, mask, [(4, 6), (29, 6), (29, 29), (4, 29)], GRAY)
        shape(rgb, mask, [(2, 3), (27, 3), (27, 26), (2, 26)], SILVER)
        d.line((3, 4, 26, 4), fill=WHITE)
        d.line((3, 5, 3, 25), fill=WHITE)
        for y in (5, 7): d.line((9, y, 25, y), fill=GRAY)
        d.rectangle((5, 5, 7, 7), fill=WHITE, outline=NAVY)
        d.rectangle((5, 10, 24, 23), fill=WHITE, outline=GRAY)
        for x in (9, 19):
            d.line((x, 12, x, 21), fill=NAVY)
            d.line((x+1, 12, x+1, 21), fill=SILVER)
        for x,y in ((7, 14), (17, 18)):
            d.rectangle((x, y, x+4, y+3), fill=SILVER, outline=NAVY)
            d.line((x+1, y+1, x+3, y+1), fill=WHITE)
    return rgb, mask
