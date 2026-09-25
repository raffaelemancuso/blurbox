#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pillow>=10"]
# ///
"""Draw the blurbox logo and write every file made from it.

The logo is a round subject on a dark video frame; the quarter of it inside a
yellow selection box (with the app's corner handles) is pixelated, each cell
the average colour of the subject under it. It is defined on a 64x64 grid.

Writes, relative to the repository root:
    docs/logo.svg   vector master
    docs/logo.png   256 px, for the README
    docs/logo.ico   16-256 px, for the Windows shortcut
    blurbox.py      the ICON_PNGS block: 16/32/64 px PNGs for the window icon

Usage:
    uv run --script tools/make_logo.py
"""

import base64
import functools
import io
import math
import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
TILE = (35, 42, 56)  # video frame
FRAME = (46, 54, 71)  # strip along the bottom of the frame
LIGHT = (255, 200, 128)  # subject: highlight ...
DARK = (196, 98, 38)  # ... to shadow
YELLOW = (245, 197, 24)  # selection outline, as in the app
WHITE = (255, 255, 255)
CX, CY, R = 28, 38, 18  # subject circle
HX, HY = 21, 30  # centre of its highlight
BX, BY, BW, BH = 28, 14, 24, 24  # selection box: covers the upper-right quarter
CELL = 4  # mosaic cell
RX = 14  # corner radius of the tile
HANDLES_FROM = 48  # smaller icons drop the handles, which would be a blur
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]
WINDOW_SIZES = [16, 32, 64]


def lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def subject(x, y):
    """Colour of the unpixelated scene at (x, y)."""
    if (x - CX) ** 2 + (y - CY) ** 2 > R * R:
        return FRAME if y >= 52 else TILE
    return lerp(LIGHT, DARK, min(1, math.hypot(x - HX, y - HY) / (R * 1.6)))


def mosaic():
    """{(col, row): colour} of each cell in the box, averaged over 8x8 samples."""
    cells, n = {}, 8
    for row in range(BH // CELL):
        for col in range(BW // CELL):
            x0, y0 = BX + col * CELL, BY + row * CELL
            samples = [subject(x0 + (i + 0.5) * CELL / n, y0 + (j + 0.5) * CELL / n)
                       for i in range(n) for j in range(n)]
            cells[col, row] = tuple(round(sum(c[k] for c in samples) / len(samples))
                                    for k in range(3))
    return cells


CELLS = mosaic()
CORNERS = [(BX, BY), (BX + BW, BY), (BX, BY + BH), (BX + BW, BY + BH)]


def in_tile(x, y):
    dx = max(RX - x, 0, x - (64 - RX))
    dy = max(RX - y, 0, y - (64 - RX))
    return dx * dx + dy * dy <= RX * RX


def logo_colour(x, y, handles, half):
    """RGBA of the logo at (x, y) in grid units, or None outside it; `half` is
    half the outline width."""
    if handles:
        for hx, hy in CORNERS:
            if abs(x - hx) <= 3 and abs(y - hy) <= 3:
                return WHITE if abs(x - hx) <= 2 and abs(y - hy) <= 2 else TILE
    # Outline, centred on the box edge
    if BX - half <= x <= BX + BW + half and BY - half <= y <= BY + BH + half and (
            abs(x - BX) <= half or abs(x - BX - BW) <= half
            or abs(y - BY) <= half or abs(y - BY - BH) <= half):
        return YELLOW
    if not in_tile(x, y):
        return None
    if BX <= x < BX + BW and BY <= y < BY + BH:
        return CELLS[int((x - BX) // CELL), int((y - BY) // CELL)]
    return subject(x, y)


@functools.cache
def render(size):
    """The logo as a size x size RGBA image, supersampled."""
    k = max(4, 128 // size)
    n = size * k
    handles = size >= HANDLES_FROM
    # 2 units wide as in the SVG, but at least 2 pixels (1 at 16 px, where 2
    # would swallow the mosaic), so the small icons keep a sharp outline
    half = max(1, (32 if size <= 16 else 64) / size)
    img = Image.new("RGBA", (size, size))
    px = img.load()
    for py in range(size):
        for pxl in range(size):
            acc, hits = [0.0, 0.0, 0.0], 0
            for j in range(k):
                for i in range(k):
                    c = logo_colour((pxl * k + i + 0.5) * 64 / n,
                                    (py * k + j + 0.5) * 64 / n, handles, half)
                    if c is not None:
                        hits += 1
                        for m in range(3):
                            acc[m] += c[m]
            if hits:
                px[pxl, py] = (*(round(a / hits) for a in acc), round(255 * hits / (k * k)))
    return img


def hexc(c):
    return "#%02x%02x%02x" % tuple(round(v) for v in c)


def svg():
    cells = "\n".join(
        f'      <rect x="{BX + col * CELL}" y="{BY + row * CELL}" width="{CELL}" '
        f'height="{CELL}" fill="{hexc(c)}"/>'
        for (col, row), c in sorted(CELLS.items(), key=lambda kv: kv[0][::-1]))
    handles = "\n".join(
        f'  <rect x="{x - 2.5}" y="{y - 2.5}" width="5" height="5" fill="#ffffff" '
        f'stroke="{hexc(TILE)}" stroke-width="1"/>' for x, y in CORNERS)
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="blurbox">
  <!-- Generated by tools/make_logo.py -->
  <defs>
    <radialGradient id="subject" gradientUnits="userSpaceOnUse" cx="{HX}" cy="{HY}" r="{R * 1.6:g}">
      <stop offset="0" stop-color="{hexc(LIGHT)}"/>
      <stop offset="1" stop-color="{hexc(DARK)}"/>
    </radialGradient>
    <clipPath id="tile"><rect width="64" height="64" rx="{RX}"/></clipPath>
  </defs>
  <g clip-path="url(#tile)">
    <rect width="64" height="64" fill="{hexc(TILE)}"/>
    <rect y="52" width="64" height="12" fill="{hexc(FRAME)}"/>
    <circle cx="{CX}" cy="{CY}" r="{R}" fill="url(#subject)"/>
    <g shape-rendering="crispEdges">
{cells}
    </g>
  </g>
  <rect x="{BX}" y="{BY}" width="{BW}" height="{BH}" fill="none" stroke="{hexc(YELLOW)}" stroke-width="2"/>
{handles}
</svg>
"""


def png_bytes(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def icon_block():
    lines = ["ICON_PNGS = ("]
    for size in WINDOW_SIZES:
        b64 = base64.b64encode(png_bytes(render(size))).decode()
        lines.append(f"    # {size} px")
        lines += [f'    "{b64[i:i + 72]}"' for i in range(0, len(b64), 72)]
        lines[-1] += ","
    lines.append(")")
    return "\n".join(lines)


def main():
    docs = ROOT / "docs"
    (docs / "logo.svg").write_bytes(svg().encode())
    (docs / "logo.png").write_bytes(png_bytes(render(256)))
    icons = [render(s) for s in ICO_SIZES]
    icons[-1].save(docs / "logo.ico", sizes=[(s, s) for s in ICO_SIZES],
                   append_images=icons[:-1])
    src = ROOT / "blurbox.py"
    code = src.read_bytes().decode()
    new, n = re.subn(r"^ICON_PNGS = \(\n.*?^\)$", lambda _: icon_block(), code,
                     count=1, flags=re.M | re.S)
    if n != 1:
        raise SystemExit("ICON_PNGS block not found in blurbox.py")
    src.write_bytes(new.encode())
    print("Wrote docs/logo.svg, docs/logo.png, docs/logo.ico and ICON_PNGS in blurbox.py")


if __name__ == "__main__":
    main()
