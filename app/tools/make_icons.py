"""Generate the Driver-app PNG icons with no third-party libraries.

iOS home-screen icons MUST be PNG (Safari ignores SVG apple-touch-icon), and
Android maskable icons want a full-bleed square. We hand-roll a tiny PNG encoder
(zlib is stdlib) and draw a full-bleed brand-colour square with a white
"forward / delivery" arrow inside the maskable safe zone.

Run:  python3 tools/make_icons.py
Outputs into ../frontend/icons/.
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import zlib
from typing import Iterable, List, Sequence, Tuple

# Defaults
ACCENT = (0x2A, 0x6B, 0xCC)
WHITE = (255, 255, 255)
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.normpath(os.path.join(HERE, "..", "frontend", "icons"))

# Normalized arrow polygon kept within central safe area
ARROW = [
    (0.50, 0.24), (0.76, 0.52), (0.605, 0.52), (0.605, 0.78),
    (0.395, 0.78), (0.395, 0.52), (0.24, 0.52),
]

def _clamp_byte(v: float) -> int:
    return max(0, min(255, int(round(v))))

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t

def _lerp_color(a: Tuple[int,int,int], b: Tuple[int,int,int], t: float) -> Tuple[int,int,int]:
    return (_clamp_byte(_lerp(a[0], b[0], t)),
            _clamp_byte(_lerp(a[1], b[1], t)),
            _clamp_byte(_lerp(a[2], b[2], t)))

def _png(size: int, pixels: bytes) -> bytes:
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)  # filter 0
        raw.extend(pixels[y * stride:(y + 1) * stride])
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    idat = zlib.compress(bytes(raw), 9)
    return sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

def _scaled_poly(poly: Sequence[Tuple[float,float]], w: int, h: int) -> List[Tuple[float,float]]:
    return [(x * w, y * h) for (x, y) in poly]

def _compute_bbox(poly: Sequence[Tuple[float,float]]) -> Tuple[int,int,int,int]:
    xs = [p[0] for p in poly]; ys = [p[1] for p in poly]
    minx = int(math.floor(min(xs))); maxx = int(math.ceil(max(xs)))
    miny = int(math.floor(min(ys))); maxy = int(math.ceil(max(ys)))
    return minx, miny, maxx, maxy

def _scanline_fill(mask: bytearray, w: int, h: int, poly: Sequence[Tuple[float,float]], value: int = 255):
    """Rasterize filled polygon into mask (bytearray length w*h), value 0..255.
    Uses scanline edge intersections per row (fast)."""
    if not poly:
        return
    # Precompute edges
    n = len(poly)
    for y in range(h):
        y_center = y + 0.5
        xs = []
        for i in range(n):
            x0,y0 = poly[i]
            x1,y1 = poly[(i+1)%n]
            # check if edge crosses scanline
            if (y0 <= y_center < y1) or (y1 <= y_center < y0):
                # compute intersection x
                t = (y_center - y0) / (y1 - y0)
                xi = x0 + t * (x1 - x0)
                xs.append(xi)
        if not xs:
            continue
        xs.sort()
        # fill between pairs
        it = iter(xs)
        for x_start, x_end in zip(it, it):
            x0i = int(math.floor(x_start + 0.5))
            x1i = int(math.floor(x_end + 0.5))
            if x1i < x0i:
                continue
            if x0i < 0: x0i = 0
            if x1i >= w: x1i = w - 1
            base = y * w
            for x in range(x0i, x1i+1):
                mask[base + x] = value

def _integral_image(src: bytearray, w: int, h: int) -> List[int]:
    """Compute integral image (as ints) of a bytearray mask."""
    ii = [0] * ((w+1)*(h+1))
    for y in range(h):
        row_sum = 0
        row_base = y * w
        for x in range(w):
            row_sum += src[row_base + x]
            ii_index = (y+1)*(w+1) + (x+1)
            ii[ii_index] = ii[ii_index - (w+1)] + row_sum
    return ii

def _box_blur_from_integral(ii: List[int], w: int, h: int, radius: int) -> bytearray:
    """Return blurred mask (0..255) via integral image for a square radius."""
    out = bytearray(w*h)
    ws = w+1
    for y in range(h):
        y0 = max(0, y - radius)
        y1 = min(h-1, y + radius)
        for x in range(w):
            x0 = max(0, x - radius)
            x1 = min(w-1, x + radius)
            # integral coordinates are +1
            A = ii[y0*ws + x0]
            B = ii[y0*ws + (x1+1)]
            C = ii[(y1+1)*ws + x0]
            D = ii[(y1+1)*ws + (x1+1)]
            s = D - B - C + A
            area = (y1 - y0 + 1) * (x1 - x0 + 1)
            out[y*w + x] = _clamp_byte(s / area)
    return out

def _composite_pixel(bg: Tuple[int,int,int], shadow_val: int, shadow_strength: float, arrow_mask_val: int, highlight: float) -> Tuple[int,int,int,int]:
    # shadow_val 0..255, shadow_strength 0..1
    dst = bg
    if shadow_val:
        a = (shadow_val / 255.0) * shadow_strength
        dst = (_clamp_byte(dst[0] * (1.0 - a)), _clamp_byte(dst[1] * (1.0 - a)), _clamp_byte(dst[2] * (1.0 - a)))
    if arrow_mask_val:
        # arrow is white; allow slight highlight mix
        if highlight > 0.01:
            # mix white over dst with highlight alpha
            h = min(0.6, highlight)
            r = _clamp_byte(WHITE[0] * h + dst[0] * (1-h))
            g = _clamp_byte(WHITE[1] * h + dst[1] * (1-h))
            b = _clamp_byte(WHITE[2] * h + dst[2] * (1-h))
            return (r,g,b,255)
        return (WHITE[0], WHITE[1], WHITE[2], 255)
    return (dst[0], dst[1], dst[2], 255)

def render(size: int, mark_scale: float = 1.0, accent: Tuple[int,int,int] = ACCENT, supersample: int = 2, shadow_strength: float = 0.20) -> bytes:
    """Render icon at target size. supersample=1..3 recommended (2 is sweet spot)."""
    ss = max(1, int(supersample))
    rw = size * ss
    rh = size * ss

    # High-res buffers
    hr_pixels = bytearray(rw * rh * 3)  # RGB only for high-res
    shadow_mask = bytearray(rw * rh)    # 0..255 mask for shadow
    arrow_mask = bytearray(rw * rh)     # 0/255 mask for arrow

    # Precompute gradient per high-res row (more depth at larger ss)
    top = _lerp_color(accent, (255,255,255), 0.14)
    mid = _lerp_color(accent, (255,255,255), 0.06)
    bot = (_clamp_byte(accent[0]*0.80), _clamp_byte(accent[1]*0.80), _clamp_byte(accent[2]*0.80))
    for y in range(rh):
        t = (y + 0.5) / rh
        if t < 0.5:
            row_rgb = _lerp_color(top, mid, t*2.0)
        else:
            row_rgb = _lerp_color(mid, bot, (t-0.5)*2.0)
        base = y * rw * 3
        for x in range(rw):
            hr_pixels[base + x*3    ] = row_rgb[0]
            hr_pixels[base + x*3 + 1] = row_rgb[1]
            hr_pixels[base + x*3 + 2] = row_rgb[2]

    # Prepare scaled polygons in high-res coords
    arrow_hr = _scaled_poly(ARROW, rw, rh)
    # shadow slightly offset downward
    shadow_offset = int(round(0.028 * mark_scale * rh))
    shadow_hr = [(x, y + shadow_offset) for (x,y) in arrow_hr]

    # Rasterize masks (fast scanline fill)
    _scanline_fill(shadow_mask, rw, rh, shadow_hr, 255)
    _scanline_fill(arrow_mask, rw, rh, arrow_hr, 255)

    # Blur shadow mask with integral image for soft shadow
    radius = max(1, int(round(ss * 2.0)))  # blur radius depends on supersample
    ii = _integral_image(shadow_mask, rw, rh)
    shadow_blur = _box_blur_from_integral(ii, rw, rh, radius)

    # Composite into final high-res RGB using shadow and arrow masks
    for y in range(rh):
        row_base = y * rw
        for x in range(rw):
            idx = row_base + x
            base_pix = (hr_pixels[(idx*3)], hr_pixels[(idx*3)+1], hr_pixels[(idx*3)+2])
            s_val = shadow_blur[idx]
            a_val = arrow_mask[idx]
            # small specular highlight: stronger near top area of arrow, derived from normalized y
            ny = (y + 0.5) / rh
            highlight = 0.0
            if a_val:
                if ny < 0.45:
                    highlight = (0.45 - ny) * 0.9
                    highlight = max(0.0, min(0.5, highlight))
            r,g,b,a = _composite_pixel(base_pix, s_val, shadow_strength, a_val, highlight)
            hr_pixels[(idx*3)    ] = r
            hr_pixels[(idx*3) + 1] = g
            hr_pixels[(idx*3) + 2] = b

    # Downscale by simple box averaging (ss x ss -> 1)
    out_buf = bytearray(size * size * 4)
    for ty in range(size):
        for tx in range(size):
            r_acc=g_acc=b_acc=0
            sx0 = tx * ss
            sy0 = ty * ss
            for yy in range(sy0, sy0 + ss):
                row_base = yy * rw * 3
                for xx in range(sx0, sx0 + ss):
                    i3 = row_base + xx*3
                    r_acc += hr_pixels[i3]
                    g_acc += hr_pixels[i3+1]
                    b_acc += hr_pixels[i3+2]
            denom = ss * ss
            r = _clamp_byte(r_acc / denom)
            g = _clamp_byte(g_acc / denom)
            b = _clamp_byte(b_acc / denom)
            o = (ty * size + tx) * 4
            out_buf[o] = r; out_buf[o+1] = g; out_buf[o+2] = b; out_buf[o+3] = 255

    return _png(size, bytes(out_buf))

def _parse_color(s: str) -> Tuple[int,int,int]:
    s = s.strip().lstrip('#')
    if len(s) != 6:
        raise ValueError('colour must be RRGGBB')
    return (int(s[0:2],16), int(s[2:4],16), int(s[4:6],16))

def main(argv: Iterable[str] = None) -> None:
    p = argparse.ArgumentParser(description='Generate beautiful, fast icons')
    p.add_argument('--out', default=OUT, help='output directory')
    p.add_argument('--accent', default=None, help='#RRGGBB accent colour')
    p.add_argument('--supersample', type=int, default=2, help='1=fast,2=high-quality,3=extra')
    p.add_argument('--shadow', type=float, default=0.20, help='shadow strength 0..1')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args(argv)

    if args.accent:
        accent = _parse_color(args.accent)
    else:
        accent = ACCENT

    outdir = os.path.abspath(args.out)
    if not args.dry_run:
        os.makedirs(outdir, exist_ok=True)

    jobs = [
        ("icon-192.png", 192, 1.0),
        ("icon-512.png", 512, 1.0),
        ("icon-maskable-512.png", 512, 0.78),
        ("apple-touch-icon.png", 180, 1.0),
        ("icon-167.png", 167, 1.0),
        ("icon-152.png", 152, 1.0),
    ]

    for name, size, scale in jobs:
        data = render(size, scale, accent=accent, supersample=args.supersample, shadow_strength=args.shadow)
        path = os.path.join(outdir, name)
        if args.dry_run:
            print('would write', path, '(size=', size, ')')
        else:
            with open(path, 'wb') as fh:
                fh.write(data)
            print('wrote', name, size)

if __name__ == '__main__':
    main()
