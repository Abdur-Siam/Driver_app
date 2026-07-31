#!/usr/bin/env python3
"""Generate the Driver-app PNG icons with no third-party libraries.

Enhancements in this patch:
- Subtle vertical gradient background (more polished than flat fill).
- Soft shadow beneath the arrow (gives depth / makes the mark pop).
- 2x2 supersample anti-aliasing for much smoother edges.
- Small CLI so sizes / out dir / accent colour can be adjusted.
- Keep defaults and file names unchanged so behavior is backwards compatible.

Run:  python3 tools/make_icons.py
Outputs into ../frontend/icons/ by default.
"""
import argparse
import math
import os
import struct
import zlib
from typing import Iterable, List, Sequence, Tuple

# Default brand colours (can be overridden via CLI)
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


def _point_in_poly(x: float, y: float, poly: Sequence[Tuple[float, float]]) -> bool:
    """Return True if point (x,y) is inside polygon `poly` using ray-casting.

    This implementation is robust against horizontal edges and avoids
    division-by-zero by only evaluating the intersection when the scanline
    crosses the edge.
    """
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        # Check if edge crosses horizontal ray at y
        intersects = ((yi > y) != (yj > y))
        if intersects:
            # Safe to compute intersection because yi != yj here
            x_at_y = xi + (xj - xi) * (y - yi) / (yj - yi)
            if x < x_at_y:
                inside = not inside
        j = i
    return inside


def _png(size: int, pixels: bytes) -> bytes:
    """pixels: bytes of RGBA, row-major. Returns PNG file bytes (square)."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)                       # filter type 0 (None)
        raw.extend(pixels[y * stride:(y + 1) * stride])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    # IHDR: width, height, bit depth, color type, compression, filter, interlace
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8-bit RGBA
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _clamp_byte(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_color(a: Tuple[int, int, int], b: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    return (_clamp_byte(_lerp(a[0], b[0], t)),
            _clamp_byte(_lerp(a[1], b[1], t)),
            _clamp_byte(_lerp(a[2], b[2], t)))


def _blend_over(dst_rgb: Tuple[int, int, int], src_rgb: Tuple[int, int, int], src_a: float) -> Tuple[int, int, int]:
    """Alpha-blend src over dst. alpha in 0..1"""
    inv_a = 1.0 - src_a
    return (_clamp_byte(src_rgb[0] * src_a + dst_rgb[0] * inv_a),
            _clamp_byte(src_rgb[1] * src_a + dst_rgb[1] * inv_a),
            _clamp_byte(src_rgb[2] * src_a + dst_rgb[2] * inv_a))


def _darken(c: Tuple[int, int, int], factor: float) -> Tuple[int, int, int]:
    return (_clamp_byte(c[0] * factor), _clamp_byte(c[1] * factor), _clamp_byte(c[2] * factor))


def render(size: int, mark_scale: float = 1.0, accent: Tuple[int, int, int] = ACCENT) -> bytes:
    """Render a square icon with a gradient background, a soft shadow and a white
    arrow mark inside the central safe zone.

    Improvements over the previous implementation:
    - 2x2 supersampling anti-aliasing (4 samples per pixel) for smooth edges.
    - Vertical gradient background (subtle) for a more modern look.
    - Soft offset shadow for depth so the white mark pops.
    """
    buf = bytearray(size * size * 4)

    # Prepare the scaled polygon for the mark
    arrow = [(0.5 + (px - 0.5) * mark_scale, 0.5 + (py - 0.5) * mark_scale)
             for px, py in ARROW]

    # Gradient: slightly lighter at the top, darker at the bottom
    light_accent = _lerp_color(accent, (255, 255, 255), 0.12)
    dark_accent = _darken(accent, 0.85)

    # Shadow offset in normalized coordinates (small subtle offset)
    shadow_offset = 0.025 * mark_scale
    shadow_alpha = 0.18
    shadow_poly = [(x, y + shadow_offset) for x, y in arrow]

    # Supersampling offsets (2x2)
    samples = [(0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75)]

    for py in range(size):
        fy = (py + 0.5) / size
        for px in range(size):
            fx = (px + 0.5) / size

            r_acc = g_acc = b_acc = a_acc = 0.0
            for sx, sy in samples:
                sx_pos = (px + sx) / size
                sy_pos = (py + sy) / size

                # Background gradient t value (0..1): top -> bottom
                t = sy_pos
                bg_rgb = _lerp_color(light_accent, dark_accent, t)

                sample_rgb = bg_rgb

                # Shadow: if sample is inside the shifted polygon -> composite shadow
                if _point_in_poly(sx_pos, sy_pos, shadow_poly) and not _point_in_poly(sx_pos, sy_pos, arrow):
                    sample_rgb = _blend_over(sample_rgb, (0, 0, 0), shadow_alpha)

                # Arrow: white mark over background
                if _point_in_poly(sx_pos, sy_pos, arrow):
                    sample_rgb = WHITE

                r_acc += sample_rgb[0]
                g_acc += sample_rgb[1]
                b_acc += sample_rgb[2]
                a_acc += 255.0  # fully opaque output

            # Average samples
            r = int(round(r_acc / len(samples)))
            g = int(round(g_acc / len(samples)))
            b = int(round(b_acc / len(samples)))
            a = int(round(a_acc / len(samples)))

            o = (py * size + px) * 4
            buf[o], buf[o + 1], buf[o + 2], buf[o + 3] = r, g, b, a

    return _png(size, bytes(buf))


def _parse_color(s: str) -> Tuple[int, int, int]:
    """Parse a hex colour like #RRGGBB or RRGGBB."""
    s = s.strip().lstrip('#')
    if len(s) != 6:
        raise ValueError("colour must be RRGGBB")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def main(argv: Iterable[str] = None) -> None:
    p = argparse.ArgumentParser(description="Generate app icons")
    p.add_argument("--out", default=OUT, help="output directory")
    p.add_argument("--accent", default=None, help="#RRGGBB accent colour")
    p.add_argument("--dry-run", action="store_true", help="don't write files")
    args = p.parse_args(argv)

    outdir = os.path.abspath(args.out)
    if not args.dry_run:
        os.makedirs(outdir, exist_ok=True)

    accent = ACCENT
    if args.accent:
        accent = _parse_color(args.accent)

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
        data = render(size, scale, accent=accent)
        path = os.path.join(outdir, name)
        if args.dry_run:
            print("would write", path, "(size=", size, ")")
        else:
            with open(path, "wb") as fh:
                fh.write(data)
            print("wrote", name, size)


if __name__ == "__main__":
    main()
