#!/usr/bin/env python3
"""Generate the Driver-app PNG icons with no third-party libraries.

iOS home-screen icons MUST be PNG (Safari ignores SVG apple-touch-icon), and
Android maskable icons want a full-bleed square. We hand-roll a tiny PNG encoder
(zlib is stdlib) and draw a full-bleed brand-colour square with a white
"forward / delivery" arrow inside the maskable safe zone.

Run:  python3 tools/make_icons.py
Outputs into ../frontend/icons/.
"""
import os
import struct
import zlib

ACCENT = (0x2A, 0x6B, 0xCC)   # --accent-d
WHITE = (0xFF, 0xFF, 0xFF)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "frontend", "icons"))

# Up-arrow polygon in fractions of the canvas (kept inside the maskable safe
# zone — the central 80% — so Android's circle mask never clips it).
ARROW = [
    (0.50, 0.24), (0.76, 0.52), (0.605, 0.52), (0.605, 0.78),
    (0.395, 0.78), (0.395, 0.52), (0.24, 0.52),
]


def _point_in_poly(x, y, poly):
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _png(width, pixels):
    """pixels: bytes of RGBA, row-major. Returns PNG file bytes."""
    raw = bytearray()
    stride = width * 4
    for y in range(width):
        raw.append(0)                       # filter type 0 (None)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, width, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def render(size, mark_scale=1.0):
    buf = bytearray(size * size * 4)
    arrow = [(0.5 + (px - 0.5) * mark_scale, 0.5 + (py - 0.5) * mark_scale)
             for px, py in ARROW]
    for y in range(size):
        fy = (y + 0.5) / size
        for x in range(size):
            fx = (x + 0.5) / size
            r, g, b = WHITE if _point_in_poly(fx, fy, arrow) else ACCENT
            o = (y * size + x) * 4
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = r, g, b, 255
    return _png(size, bytes(buf))


def main():
    os.makedirs(OUT, exist_ok=True)
    jobs = [
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        # Maskable: shrink the mark so it stays inside Android's 80% safe circle.
        ("icon-maskable-512.png", 512, 0.78),
        # apple-touch-icon: iOS rounds the corners itself, so keep it full bleed.
        ("apple-touch-icon.png", 180, 1.0),
        ("icon-167.png", 167, 1.0),   # iPad Pro
        ("icon-152.png", 152, 1.0),   # iPad
    ]
    for name, size, scale in jobs:
        with open(os.path.join(OUT, name), "wb") as fh:
            fh.write(render(size, scale))
        print("wrote", name, size)


if __name__ == "__main__":
    main()
