#!/usr/bin/env python3
"""Generates docs/icon-192.png and docs/icon-512.png.

    uv run tools/make_icons.py

A ONE-OFF. The pipeline never imports or calls this — the PNGs it writes are
committed and served as static assets alongside style.css and app.js, exactly
like docs/icon.svg. Re-run it only if the mark or the brand colours change.

Written against the standard library alone: this machine has no Pillow, no
ImageMagick and no rsvg, and adding an image dependency to a project whose whole
point is to need no build step would be a poor trade for two flat images. A
valid 8-bit truecolour PNG is a header, one zlib-compressed scanline block and a
terminator, so it is cheaper to emit the bytes directly.

The mark is three bars of decreasing width — the lines of a briefing — matching
docs/icon.svg. No glyph, so no font rasterisation is needed.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"

BG = (26, 26, 26)  # #1a1a1a, the theme's ink; matches icon.svg
FG = (255, 255, 255)  # #ffffff — black and white, like the rest of the app

# Fractions of the canvas. Android crops `maskable` icons to a circle of radius
# 0.4 about the centre, so BAR_X centres the widest bar (0.34 + 0.32 = 0.66) and
# the whole mark stays about 0.22 from the centre — comfortably inside the crop.
# The narrower bars stay flush left, which is what makes the group read as lines
# of text rather than as three unrelated dashes.
BAR_X = 0.34
BAR_HEIGHT = 0.055
BARS = ((0.34, 0.32), (0.47, 0.26), (0.60, 0.18))  # (top, width)


def _chunk(tag: bytes, data: bytes) -> bytes:
    body = tag + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def write_png(path: Path, size: int) -> None:
    """Write a `size` x `size` 8-bit RGB PNG of the briefing mark."""
    rects = [
        (
            round(BAR_X * size),
            round(top * size),
            round((BAR_X + width) * size),
            round((top + BAR_HEIGHT) * size),
        )
        for top, width in BARS
    ]

    bg, fg = bytes(BG), bytes(FG)
    rows = []
    for y in range(size):
        spans = [(x0, x1) for (x0, y0, x1, y1) in rects if y0 <= y < y1]
        row = bytearray(b"\x00")  # filter byte 0: no per-scanline filtering
        for x in range(size):
            row += fg if any(x0 <= x < x1 for x0, x1 in spans) else bg
        rows.append(bytes(row))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + _chunk(b"IEND", b"")
    )
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    for size in (192, 512):
        write_png(DOCS / f"icon-{size}.png", size)
