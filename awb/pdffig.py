"""The figures of awb pdf, standard library only: PNG and JPEG images as PDF image objects, inline SVG as PDF vector
drawing, CSS colours and the few CSS rules the writer reads.

An image becomes one image XObject. An 8-bit grey or RGB PNG, a palette PNG and a grey PNG of 1, 2 or 4 bits keep
their compressed data (Flate with the PNG predictor), a colour key of tRNS becomes a /Mask; grey with alpha, RGBA, a
palette with tRNS and every 16-bit PNG are decoded here (the five PNG filters), 16 bits cut to 8, and the alpha goes
into a soft mask (/SMask). An interlaced PNG is refused with a clear message. A JPEG keeps its data (DCTDecode).

SVG is compiled into items in its own user space (y down): ("q",), ("Q",), ("cm", m), ("clip", box), ("path",
segments, paint) and ("text", chunks). Shapes rect (rounded too), line, polyline, polygon, circle, ellipse and path
(M L H V C S Q T A Z, absolute and relative; an arc becomes cubic curves), text with tspan (x, y, dx, dy,
text-anchor, font-size, weight, style, family), g, a, switch (its first supported child), use of an id, markers at
the start and end of a line (arrow heads), transform (matrix, translate, scale, rotate, skewX, skewY), fill, stroke,
stroke-width, dash, caps and joins, fill-rule, opacity, fill-opacity and stroke-opacity, colours as hex, rgb(),
names or currentColor, a gradient as the mean of its stops, viewBox with preserveAspectRatio and width or height in
px, pt, mm, cm, in or %. The style comes from presentation attributes, simple CSS rules (tag, .class, #id, tag.class,
*, comma lists) of a style element of the SVG or of the page, and the style attribute, in that order. What it cannot
draw (foreignObject, an image inside the SVG, a pattern, a clip path, a mask, a filter, a marker in the middle,
textPath, an unknown element) is left out and counted, never printed as text.
"""
from __future__ import annotations

import math
import re
import struct
import zlib
from dataclasses import dataclass, field

MAX_PIXELS = 40_000_000
PX = 0.75                      # one CSS pixel in points

# --------------------------------------------------------------------------- errors


class FigureError(Exception):
    """An image that cannot be embedded: the one line of the refusal."""


# --------------------------------------------------------------------------- colours and CSS

NAMED = {
    "black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "gray": (128, 128, 128), "grey": (128, 128, 128), "silver": (192, 192, 192), "navy": (0, 0, 128),
    "orange": (255, 165, 0), "yellow": (255, 255, 0), "purple": (128, 0, 128), "teal": (0, 128, 128),
    "maroon": (128, 0, 0), "lime": (0, 255, 0), "aqua": (0, 255, 255), "cyan": (0, 255, 255),
    "fuchsia": (255, 0, 255), "magenta": (255, 0, 255), "olive": (128, 128, 0), "darkgray": (169, 169, 169),
    "darkgrey": (169, 169, 169), "lightgray": (211, 211, 211), "lightgrey": (211, 211, 211),
    "dimgray": (105, 105, 105), "dimgrey": (105, 105, 105), "gainsboro": (220, 220, 220),
    "whitesmoke": (245, 245, 245), "darkblue": (0, 0, 139), "darkred": (139, 0, 0), "darkgreen": (0, 100, 0),
    "lightblue": (173, 216, 230), "lightgreen": (144, 238, 144), "pink": (255, 192, 203), "brown": (165, 42, 42),
    "gold": (255, 215, 0), "steelblue": (70, 130, 180), "skyblue": (135, 206, 235), "crimson": (220, 20, 60),
    "tomato": (255, 99, 71), "coral": (255, 127, 80), "salmon": (250, 128, 114), "indigo": (75, 0, 130),
    "violet": (238, 130, 238), "slategray": (112, 128, 144), "slategrey": (112, 128, 144),
    "lightyellow": (255, 255, 224), "beige": (245, 245, 220), "ivory": (255, 255, 240),
    "darkorange": (255, 140, 0), "royalblue": (65, 105, 225), "seagreen": (46, 139, 87),
}


def color(value: str | None) -> tuple[float, float, float] | None:
    """(r, g, b) from 0 to 1 of a CSS colour: #rgb, #rrggbb, #rrggbbaa, rgb() or rgba() in numbers or %, a name;
    None for anything else."""
    if not value:
        return None
    v = value.strip().lower()
    m = re.fullmatch(r"#([0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})", v)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    m = re.fullmatch(r"rgba?\(\s*([^)]*)\)", v)
    if m:
        parts = [p for p in re.split(r"[\s,/]+", m.group(1)) if p][:3]
        if len(parts) != 3:
            return None
        out = []
        for p in parts:
            try:
                n = float(p[:-1]) * 2.55 if p.endswith("%") else float(p)
            except ValueError:
                return None
            out.append(min(max(n, 0.0), 255.0) / 255.0)
        return tuple(out)
    if v in NAMED:
        return tuple(c / 255.0 for c in NAMED[v])
    return None


def declarations(text: str) -> dict[str, str]:
    """The declarations of a style attribute or a CSS block, names in lower case."""
    out = {}
    for part in text.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            v = re.sub(r"\s*!important\s*$", "", v.strip(), flags=re.I)
            if k.strip():
                out[k.strip().lower()] = v
    return out


_SIMPLE = re.compile(r"^(\*|[a-z][a-z0-9-]*)?((?:[.#][\w-]+)*)$", re.I)


def rules(css: str) -> list[tuple[tuple[int, int, int], int, tuple, dict[str, str]]]:
    """The simple rules of a style sheet: (specificity, order, (tag, classes, id), declarations); a rule with a
    combinator or a pseudo class is left out, an at-rule too."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    css = re.sub(r"@[^{;]*;", "", css)
    out = []
    order = 0
    depth_skip = re.compile(r"@[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", re.S)
    css = depth_skip.sub("", css)
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
        decls = declarations(m.group(2))
        for sel in m.group(1).split(","):
            sel = sel.strip()
            s = _SIMPLE.match(sel)
            if not sel or not s:
                continue
            tag = (s.group(1) or "*").lower()
            parts = re.findall(r"([.#])([\w-]+)", s.group(2))
            classes = tuple(n for k, n in parts if k == ".")
            ids = [n for k, n in parts if k == "#"]
            spec = (len(ids), len(classes), 0 if tag == "*" else 1)
            out.append((spec, order, (tag, classes, ids[0] if ids else None), decls))
            order += 1
    return out


def matched(rule_list: list, tag: str, attrs: dict) -> dict[str, str]:
    """The declarations of the rules that match an element, the more specific and the later winning."""
    classes = set((attrs.get("class") or "").split())
    ident = attrs.get("id")
    out: dict[str, str] = {}
    for _, _, (t, cls, i), decls in sorted(rule_list, key=lambda r: (r[0], r[1])):
        if t not in ("*", tag) or not set(cls) <= classes or (i is not None and i != ident):
            continue
        out.update(decls)
    return out


# --------------------------------------------------------------------------- images


@dataclass
class Image:
    """One image XObject: its pixel size, the dictionary entries and the stream; `smask` the soft mask of an alpha
    channel (an Image of DeviceGray) or None."""
    width: int
    height: int
    entries: bytes
    data: bytes
    smask: "Image | None" = None


def image(data: bytes) -> Image:
    """The image object of a PNG or JPEG file; FigureError when it cannot be embedded."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data)
    raise FigureError("an image that is not PNG or JPEG")


def _jpeg(data: bytes) -> Image:
    pos = 2
    while pos + 4 <= len(data):
        if data[pos] != 0xFF:
            raise FigureError("a broken JPEG image")
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            if pos + 10 > len(data):
                break
            bits, h, w, n = struct.unpack(">BHHB", data[pos + 4:pos + 10])
            if bits != 8 or n not in (1, 3, 4) or not w or not h:
                raise FigureError("a JPEG image of %d bits and %d channels (only 8-bit grey, RGB or CMYK)" % (bits, n))
            cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}[n]
            decode = b" /Decode [1 0 1 0 1 0 1 0]" if n == 4 and b"Adobe" in data[:pos] else b""
            return Image(w, h, b"/ColorSpace %s /BitsPerComponent 8 /Filter /DCTDecode%s" % (cs, decode), data)
        pos += 2 + length
    raise FigureError("a broken JPEG image")


def _chunks(data: bytes):
    pos = 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise FigureError("a broken PNG image")
        yield kind, body
        if kind == b"IEND":
            return
        pos += 12 + length


_MASKS: dict[int, tuple[int, int]] = {}


def _add(a: bytes, b: bytes) -> bytes:
    """a + b per byte, modulo 256 (no carry between bytes)."""
    n = len(a)
    if n not in _MASKS:
        _MASKS[n] = (int.from_bytes(b"\x7f" * n, "little"), int.from_bytes(b"\x80" * n, "little"))
    lo, hi = _MASKS[n]
    x, y = int.from_bytes(a, "little"), int.from_bytes(b, "little")
    return (((x & lo) + (y & lo)) ^ ((x ^ y) & hi)).to_bytes(n, "little")


def _sub(line: bytes, bpp: int) -> bytes:
    """The Sub filter undone: a running sum at the distance bpp, in log steps."""
    n = len(line)
    full = (1 << (8 * n)) - 1
    v = line
    shift = bpp
    while shift < n:
        moved = ((int.from_bytes(v, "little") << (8 * shift)) & full).to_bytes(n, "little")
        v = _add(v, moved)
        shift *= 2
    return v


def _unfilter(raw: bytes, rowbytes: int, height: int, bpp: int) -> bytes:
    out = bytearray()
    prev = bytes(rowbytes)
    stride = rowbytes + 1
    for r in range(height):
        f = raw[r * stride]
        line = raw[r * stride + 1:(r + 1) * stride]
        if f == 0:
            cur = line
        elif f == 1:
            cur = _sub(line, bpp)
        elif f == 2:
            cur = _add(line, prev)
        elif f == 3:
            c = bytearray(line)
            for i in range(rowbytes):
                left = c[i - bpp] if i >= bpp else 0
                c[i] = (c[i] + ((left + prev[i]) >> 1)) & 255
            cur = bytes(c)
        elif f == 4:
            c = bytearray(line)
            for i in range(rowbytes):
                a = c[i - bpp] if i >= bpp else 0
                b = prev[i]
                d = prev[i - bpp] if i >= bpp else 0
                p = a + b - d
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - d)
                c[i] = (c[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else d)) & 255
            cur = bytes(c)
        else:
            raise FigureError("a broken PNG image (filter %d)" % f)
        out += cur
        prev = cur
    return bytes(out)


def _png(data: bytes) -> Image:
    ihdr, plte, trns, idat = None, None, None, []
    for kind, body in _chunks(data):
        if kind == b"IHDR":
            ihdr = body
        elif kind == b"PLTE":
            plte = body
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
    if ihdr is None or len(ihdr) != 13 or not idat:
        raise FigureError("a broken PNG image")
    w, h, depth, ctype, comp, filt, interlace = struct.unpack(">IIBBBBB", ihdr)
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or comp or filt or not w or not h or depth not in (1, 2, 4, 8, 16):
        raise FigureError("a broken PNG image")
    if w * h > MAX_PIXELS:
        raise FigureError("a PNG image of more than %d pixels" % MAX_PIXELS)
    if interlace:
        raise FigureError("an interlaced PNG image; save it without interlacing")
    if ctype == 3 and (plte is None or depth == 16):
        raise FigureError("a broken PNG image (palette)")
    if ctype in (2, 4, 6) and depth < 8:
        raise FigureError("a broken PNG image (depth)")
    stream = b"".join(idat)
    cs = b"/DeviceRGB" if ctype in (2, 6) else b"/DeviceGray"
    if ctype == 3:
        cs = b"[/Indexed /DeviceRGB %d <%s>]" % (len(plte) // 3 - 1, plte[:len(plte) // 3 * 3].hex().encode())
    colors = 3 if ctype in (2, 6) else 1
    passthrough = depth <= 8 and (ctype in (0, 2) or (ctype == 3 and trns is None))
    if passthrough:
        mask = b""
        if trns is not None and ctype == 0 and len(trns) >= 2:
            v = struct.unpack(">H", trns[:2])[0]
            mask = b" /Mask [%d %d]" % (v, v)
        elif trns is not None and ctype == 2 and len(trns) >= 6:
            r, g, b = struct.unpack(">HHH", trns[:6])
            mask = b" /Mask [%d %d %d %d %d %d]" % (r, r, g, g, b, b)
        return Image(w, h, b"/ColorSpace %s /BitsPerComponent %d /Filter /FlateDecode /DecodeParms << /Predictor 15 "
                     b"/Colors %d /BitsPerComponent %d /Columns %d >>%s" % (cs, depth, colors, depth, w, mask),
                     stream)
    # decode: alpha, a palette with tRNS or 16 bits
    bits = depth * channels
    rowbytes = (w * bits + 7) // 8
    expected = h * (rowbytes + 1)
    try:
        dec = zlib.decompressobj()
        raw = dec.decompress(stream, expected)
    except zlib.error:
        raise FigureError("a broken PNG image") from None
    if len(raw) < expected:
        raise FigureError("a broken PNG image (short data)")
    px = _unfilter(raw, rowbytes, h, max(1, bits // 8))
    if depth == 16:
        px = px[0::2]
    alpha = None
    if ctype == 3:
        table = bytes(trns[i] if i < len(trns) else 255 for i in range(256))
        if depth == 8:
            index = px
        else:
            per = 8 // depth
            unpack = [bytes((b >> (8 - depth * (k + 1))) & ((1 << depth) - 1) for k in range(per)) for b in range(256)]
            index = b"".join(b"".join(unpack[b] for b in px[r * rowbytes:(r + 1) * rowbytes])[:w] for r in range(h))
            px = b"".join(px[r * rowbytes:(r + 1) * rowbytes] for r in range(h))
        alpha = index.translate(table)
        color_data, bpc = px, depth
    elif ctype in (4, 6):
        n = channels
        alpha = px[n - 1::n]
        if colors == 1:
            color_data = px[0::n]
        else:
            c = bytearray(w * h * 3)
            c[0::3], c[1::3], c[2::3] = px[0::n], px[1::n], px[2::n]
            color_data = bytes(c)
        bpc = 8
    else:
        color_data, bpc = px, 8
    mask = None
    if alpha is not None and alpha.count(255) != len(alpha):
        mask = Image(w, h, b"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(alpha, 6))
    return Image(w, h, b"/ColorSpace %s /BitsPerComponent %d /Filter /FlateDecode" % (cs, bpc),
                 zlib.compress(color_data, 6), mask)


# --------------------------------------------------------------------------- SVG


@dataclass
class Node:
    """An element of an inline SVG: tag and attributes in lower case (as the HTML parser gives them), children
    nodes and text."""
    tag: str
    attrs: dict
    children: list = field(default_factory=list)


@dataclass
class Paint:
    fill: tuple | None
    stroke: tuple | None
    width: float
    fill_alpha: float
    stroke_alpha: float
    evenodd: bool
    dash: tuple
    cap: int
    join: int


@dataclass
class Svg:
    """A compiled SVG: its size in points, the viewBox (x, y, w, h), whether the aspect ratio is kept, the drawing
    items, the texts it shows and what was left out by kind."""
    width: float
    height: float
    box: tuple
    keep_ratio: bool
    items: list
    texts: list
    skipped: dict


INHERITED = ("fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "fill-rule",
             "fill-opacity", "stroke-opacity", "font-size", "font-family", "font-weight", "font-style",
             "text-anchor", "color", "visibility", "stroke-dashoffset")
OWN = ("opacity", "display", "transform", "marker-start", "marker-end", "marker-mid", "clip-path", "mask", "filter")
QUIET = {"title", "desc", "metadata", "defs", "style", "script", "lineargradient", "radialgradient", "stop", "marker",
         "symbol", "clippath", "mask", "filter", "pattern"}
SHAPES = {"rect", "line", "polyline", "polygon", "circle", "ellipse", "path"}
GROUPS = {"g", "a", "svg"}
UNITS = {"px": 1.0, "pt": 1 / PX, "pc": 16.0, "mm": 96 / 25.4, "cm": 96 / 2.54, "in": 96.0, "em": 16.0, "ex": 8.0}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def length(value, default: float = 0.0, ref: float = 0.0) -> float:
    """A length in user units (px); % of `ref`."""
    if value is None:
        return default
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*(px|pt|pc|mm|cm|in|em|ex|%)?\s*", str(value))
    if not m:
        return default
    n = float(m.group(1))
    unit = m.group(2)
    if unit == "%":
        return n * ref / 100.0
    return n * UNITS.get(unit or "px", 1.0)


def numbers(text: str | None) -> list[float]:
    return [float(x) for x in _NUM.findall(text or "")]


def transform(text: str | None) -> list[tuple]:
    """The matrices of a transform attribute, in the order they are written."""
    out = []
    for name, args in re.findall(r"(matrix|translate|scale|rotate|skewX|skewY)\s*\(([^)]*)\)", text or "", re.I):
        n = numbers(args)
        name = name.lower()
        if name == "matrix" and len(n) == 6:
            out.append(tuple(n))
        elif name == "translate" and n:
            out.append((1, 0, 0, 1, n[0], n[1] if len(n) > 1 else 0))
        elif name == "scale" and n:
            out.append((n[0], 0, 0, n[1] if len(n) > 1 else n[0], 0, 0))
        elif name == "rotate" and n:
            a = math.radians(n[0])
            c, s = math.cos(a), math.sin(a)
            if len(n) >= 3:
                out.append((1, 0, 0, 1, n[1], n[2]))
                out.append((c, s, -s, c, 0, 0))
                out.append((1, 0, 0, 1, -n[1], -n[2]))
            else:
                out.append((c, s, -s, c, 0, 0))
        elif name == "skewx" and n:
            out.append((1, 0, math.tan(math.radians(n[0])), 1, 0, 0))
        elif name == "skewy" and n:
            out.append((1, math.tan(math.radians(n[0])), 0, 1, 0, 0))
    return out


def _arc(x1, y1, rx, ry, phi, large, sweep, x2, y2) -> list[tuple]:
    """Cubic curves of an SVG arc (endpoint form), at most a quarter turn each."""
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry:
        return [("L", x2, y2)]
    cp, sp = math.cos(math.radians(phi)), math.sin(math.radians(phi))
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(num / den, 0)) if den else 0
    if large == sweep:
        co = -co
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cp * cxp - sp * cyp + (x1 + x2) / 2
    cy = sp * cxp + cp * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

    t1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = angle((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    n = max(1, int(math.ceil(abs(dt) / (math.pi / 2) - 1e-9)))
    step = dt / n
    k = 4 / 3 * math.tan(step / 4)
    out = []

    def point(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return cp * x - sp * y + cx, sp * x + cp * y + cy

    def deriv(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return cp * x - sp * y, sp * x + cp * y

    t = t1
    for _ in range(n):
        p0, p3 = point(t), point(t + step)
        d0, d3 = deriv(t), deriv(t + step)
        out.append(("C", p0[0] + k * d0[0], p0[1] + k * d0[1], p3[0] - k * d3[0], p3[1] - k * d3[1], p3[0], p3[1]))
        t += step
    out[-1] = out[-1][:5] + (x2, y2)
    return out


def path(d: str | None) -> list[tuple]:
    """Absolute segments ("M", x, y), ("L", x, y), ("C", x1, y1, x2, y2, x, y) and ("Z",) of path data."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", d or "")
    out: list[tuple] = []
    i, cmd = 0, None
    x = y = sx = sy = 0.0
    last_c = last_q = None

    def nums(k):
        nonlocal i
        vals = []
        while len(vals) < k:
            if i >= len(tokens) or tokens[i].isalpha():
                raise ValueError
            vals.append(float(tokens[i]))
            i += 1
        return vals

    def flag():
        # arc flags may be written together with no separator: "a1 1 0 00 5 5"
        nonlocal i
        t = tokens[i]
        if t.isalpha():
            raise ValueError
        if t in ("0", "1"):
            i += 1
            return int(t)
        if re.fullmatch(r"[01]\d*(?:\.\d*)?", t) and "." not in t[:2]:
            tokens[i] = t[1:]
            return int(t[0])
        i += 1
        return 1 if float(t) else 0

    try:
        while i < len(tokens):
            if tokens[i].isalpha():
                cmd = tokens[i]
                i += 1
                if cmd in "Zz":
                    out.append(("Z",))
                    x, y = sx, sy
                    last_c = last_q = None
                    continue
            elif cmd is None or cmd in "Zz":
                break
            rel = cmd.islower()
            c = cmd.upper()
            ox, oy = (x, y) if rel else (0.0, 0.0)
            if c == "M":
                a, b = nums(2)
                x, y = ox + a, oy + b
                sx, sy = x, y
                out.append(("M", x, y))
                cmd = "l" if rel else "L"
                last_c = last_q = None
            elif c == "L":
                a, b = nums(2)
                x, y = ox + a, oy + b
                out.append(("L", x, y))
                last_c = last_q = None
            elif c == "H":
                (a,) = nums(1)
                x = (x if rel else 0) + a
                out.append(("L", x, y))
                last_c = last_q = None
            elif c == "V":
                (b,) = nums(1)
                y = (y if rel else 0) + b
                out.append(("L", x, y))
                last_c = last_q = None
            elif c in "CS":
                if c == "C":
                    x1, y1, x2, y2, a, b = nums(6)
                    x1, y1 = ox + x1, oy + y1
                else:
                    x2, y2, a, b = nums(4)
                    x1, y1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
                x2, y2 = ox + x2, oy + y2
                x, y = ox + a, oy + b
                out.append(("C", x1, y1, x2, y2, x, y))
                last_c, last_q = (x2, y2), None
            elif c in "QT":
                if c == "Q":
                    qx, qy, a, b = nums(4)
                    qx, qy = ox + qx, oy + qy
                else:
                    a, b = nums(2)
                    qx, qy = (2 * x - last_q[0], 2 * y - last_q[1]) if last_q else (x, y)
                nx, ny = ox + a, oy + b
                out.append(("C", x + 2 / 3 * (qx - x), y + 2 / 3 * (qy - y), nx + 2 / 3 * (qx - nx),
                            ny + 2 / 3 * (qy - ny), nx, ny))
                x, y = nx, ny
                last_q, last_c = (qx, qy), None
            elif c == "A":
                rx, ry, phi = nums(3)
                large, sweep = flag(), flag()
                a, b = nums(2)
                nx, ny = ox + a, oy + b
                out.extend(_arc(x, y, rx, ry, phi, large, sweep, nx, ny))
                x, y = nx, ny
                last_c = last_q = None
    except (ValueError, IndexError):
        pass                    # an error in path data ends the path where it stands, as a browser does
    if out and out[0][0] != "M":
        out.insert(0, ("M", 0.0, 0.0))
    return out


def _ellipse(cx, cy, rx, ry) -> list[tuple]:
    k = 0.5522847498
    return [("M", cx + rx, cy), ("C", cx + rx, cy + k * ry, cx + k * rx, cy + ry, cx, cy + ry),
            ("C", cx - k * rx, cy + ry, cx - rx, cy + k * ry, cx - rx, cy),
            ("C", cx - rx, cy - k * ry, cx - k * rx, cy - ry, cx, cy - ry),
            ("C", cx + k * rx, cy - ry, cx + rx, cy - k * ry, cx + rx, cy), ("Z",)]


def _rect(x, y, w, h, rx, ry) -> list[tuple]:
    if rx <= 0 and ry <= 0:
        return [("M", x, y), ("L", x + w, y), ("L", x + w, y + h), ("L", x, y + h), ("Z",)]
    rx = rx if rx > 0 else ry
    ry = ry if ry > 0 else rx
    rx, ry = min(rx, w / 2), min(ry, h / 2)
    k = 0.5522847498
    return [("M", x + rx, y), ("L", x + w - rx, y),
            ("C", x + w - rx + k * rx, y, x + w, y + ry - k * ry, x + w, y + ry), ("L", x + w, y + h - ry),
            ("C", x + w, y + h - ry + k * ry, x + w - rx + k * rx, y + h, x + w - rx, y + h), ("L", x + rx, y + h),
            ("C", x + rx - k * rx, y + h, x, y + h - ry + k * ry, x, y + h - ry), ("L", x, y + ry),
            ("C", x, y + ry - k * ry, x + rx - k * rx, y, x + rx, y), ("Z",)]


def _ends(segs: list[tuple]) -> tuple | None:
    """((x, y, angle) at the start, (x, y, angle) at the end) of the segments, angles in degrees."""
    pts = []
    for s in segs:
        if s[0] == "C":
            pts.append([(s[1], s[2]), (s[3], s[4]), (s[5], s[6])])
        elif s[0] in "ML":
            pts.append([(s[1], s[2])])
    flat = [p for group in pts for p in group]
    if len(flat) < 2:
        return None

    def direction(a, b):
        return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))

    start = flat[0]
    nxt = next((p for p in flat[1:] if p != start), None)
    end = flat[-1]
    prv = next((p for p in reversed(flat[:-1]) if p != end), None)
    if nxt is None or prv is None:
        return None
    return (start[0], start[1], direction(start, nxt)), (end[0], end[1], direction(prv, end))


class Compiler:
    """Compiles one SVG tree into drawing items; `css` are the rules of the page around it."""

    def __init__(self, root: Node, css: list) -> None:
        self.root = root
        self.ids: dict[str, Node] = {}
        sheet = []
        self._index(root, sheet)
        self.css = list(css) + rules("\n".join(sheet))
        self.items: list = []
        self.texts: list[str] = []
        self.skipped: dict[str, int] = {}
        self.depth = 0

    def _index(self, node: Node, sheet: list) -> None:
        if node.attrs.get("id"):
            self.ids.setdefault(node.attrs["id"], node)
        if node.tag == "style":
            sheet.extend(c for c in node.children if isinstance(c, str))
        for c in node.children:
            if isinstance(c, Node):
                self._index(c, sheet)

    def skip(self, what: str) -> None:
        self.skipped[what] = self.skipped.get(what, 0) + 1

    def style(self, node: Node, parent: dict) -> dict:
        st = {k: v for k, v in parent.items() if k in INHERITED}
        for k in INHERITED + OWN:
            if k in node.attrs and k != "transform":
                st[k] = node.attrs[k]
        st.update(matched(self.css, node.tag, node.attrs))
        if node.attrs.get("style"):
            st.update(declarations(node.attrs["style"]))
        st["_alpha"] = parent.get("_alpha", 1.0) * _float(st.get("opacity"), 1.0)
        st.pop("opacity", None)
        return st

    def paint_color(self, value: str | None, st: dict):
        if value is None:
            return None
        v = value.strip()
        if v.lower() in ("none", "transparent"):
            return None
        if v.lower() == "currentcolor":
            return color(st.get("color")) or (0.0, 0.0, 0.0)
        m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)\s*(.*)$", v)
        if m:
            node = self.ids.get(m.group(1))
            if node is not None and node.tag in ("lineargradient", "radialgradient"):
                stops = self._stops(node)
                if stops:
                    return tuple(sum(c[i] for c in stops) / len(stops) for i in range(3))
            self.skip("pattern or paint server")
            return self.paint_color(m.group(2), st) if m.group(2) else None
        return color(v)

    def _stops(self, node: Node, seen: int = 0) -> list:
        stops = []
        for c in node.children:
            if isinstance(c, Node) and c.tag == "stop":
                st = self.style(c, {})
                col = color(st.get("stop-color") or c.attrs.get("stop-color") or "black")
                if col:
                    stops.append(col)
        href = node.attrs.get("href") or node.attrs.get("xlink:href")
        if not stops and href and href.startswith("#") and seen < 4 and href[1:] in self.ids:
            return self._stops(self.ids[href[1:]], seen + 1)
        return stops

    def paint(self, st: dict) -> Paint:
        fill = self.paint_color(st.get("fill", "black"), st)
        stroke = self.paint_color(st.get("stroke"), st)
        a = st.get("_alpha", 1.0)
        dash = tuple(x for x in numbers(st.get("stroke-dasharray")) if x >= 0)
        if dash and not any(dash):
            dash = ()
        return Paint(fill, stroke, max(length(st.get("stroke-width"), 1.0), 0.0),
                     a * _float(st.get("fill-opacity"), 1.0), a * _float(st.get("stroke-opacity"), 1.0),
                     (st.get("fill-rule") or "").strip() == "evenodd", dash,
                     {"round": 1, "square": 2}.get((st.get("stroke-linecap") or "").strip(), 0),
                     {"round": 1, "bevel": 2}.get((st.get("stroke-linejoin") or "").strip(), 0))

    def compile(self) -> None:
        st = self.style(self.root, {"_alpha": 1.0})
        self.children(self.root, st)

    def children(self, node: Node, st: dict) -> None:
        for c in node.children:
            if isinstance(c, Node):
                self.element(c, st)

    def element(self, node: Node, parent: dict, extra: list | None = None) -> None:
        tag = node.tag
        if tag in QUIET:
            return
        self.depth += 1
        try:
            if self.depth > 64:
                self.skip("nesting")
                return
            st = self.style(node, parent)
            if (st.get("display") or "").strip() == "none":
                return
            for k, what in (("clip-path", "clip path"), ("mask", "mask"), ("filter", "filter")):
                if st.get(k) and st[k].strip() != "none":
                    self.skip(what)
            matrices = (extra or []) + transform(node.attrs.get("transform"))
            if tag in ("g", "a"):
                self.wrap(matrices, lambda: self.children(node, st))
            elif tag == "switch":
                first = next((c for c in node.children if isinstance(c, Node) and c.tag in SHAPES | GROUPS
                              | {"text", "use", "switch"}), None)
                if first is not None:
                    self.wrap(matrices, lambda: self.element(first, st))
            elif tag == "svg":
                self.nested(node, st, matrices)
            elif tag == "use":
                self.use(node, st, matrices)
            elif tag in SHAPES:
                self.wrap(matrices, lambda: self.shape(node, st))
            elif tag == "text":
                self.wrap(matrices, lambda: self.text(node, st))
            elif tag == "foreignobject":
                self.skip("foreignObject")
            elif tag == "image":
                self.skip("image inside SVG")
            else:
                self.skip(tag)
        finally:
            self.depth -= 1

    def wrap(self, matrices: list, draw) -> None:
        self.items.append(("q",))
        for m in matrices:
            self.items.append(("cm", m))
        draw()
        self.items.append(("Q",))

    def nested(self, node: Node, st: dict, matrices: list) -> None:
        x, y = length(node.attrs.get("x")), length(node.attrs.get("y"))
        w = length(node.attrs.get("width"), 0.0)
        h = length(node.attrs.get("height"), 0.0)
        box = numbers(node.attrs.get("viewbox"))
        m = list(matrices) + [(1, 0, 0, 1, x, y)]
        if len(box) == 4 and box[2] > 0 and box[3] > 0 and w > 0 and h > 0:
            s = min(w / box[2], h / box[3])
            m.append((s, 0, 0, s, -box[0] * s, -box[1] * s))
        self.wrap(m, lambda: self.children(node, st))

    def use(self, node: Node, st: dict, matrices: list) -> None:
        href = node.attrs.get("href") or node.attrs.get("xlink:href") or ""
        target = self.ids.get(href[1:]) if href.startswith("#") else None
        if target is None or self.depth > 32:
            self.skip("use")
            return
        m = list(matrices) + [(1, 0, 0, 1, length(node.attrs.get("x")), length(node.attrs.get("y")))]
        if target.tag == "symbol":
            self.wrap(m, lambda: self.children(target, self.style(target, st)))
        else:
            self.wrap(m, lambda: self.element(target, st))

    def shape(self, node: Node, st: dict) -> None:
        a, tag = node.attrs, node.tag
        if tag == "rect":
            w, h = length(a.get("width")), length(a.get("height"))
            if w <= 0 or h <= 0:
                return
            segs = _rect(length(a.get("x")), length(a.get("y")), w, h, length(a.get("rx"), -1), length(a.get("ry"), -1))
        elif tag == "circle":
            r = length(a.get("r"))
            if r <= 0:
                return
            segs = _ellipse(length(a.get("cx")), length(a.get("cy")), r, r)
        elif tag == "ellipse":
            rx, ry = length(a.get("rx")), length(a.get("ry"))
            if rx <= 0 or ry <= 0:
                return
            segs = _ellipse(length(a.get("cx")), length(a.get("cy")), rx, ry)
        elif tag == "line":
            segs = [("M", length(a.get("x1")), length(a.get("y1"))), ("L", length(a.get("x2")), length(a.get("y2")))]
        elif tag in ("polyline", "polygon"):
            n = numbers(a.get("points"))
            pts = list(zip(n[0::2], n[1::2]))
            if len(pts) < 2:
                return
            segs = [("M",) + pts[0]] + [("L",) + p for p in pts[1:]] + ([("Z",)] if tag == "polygon" else [])
        else:
            segs = path(a.get("d"))
        if not segs:
            return
        p = self.paint(st)
        if tag in ("line", "polyline"):
            p.fill = None if tag == "line" else p.fill
        self.items.append(("path", segs, p))
        self.markers(segs, st, p)

    def markers(self, segs: list, st: dict, p: Paint) -> None:
        if st.get("marker-mid") and st["marker-mid"].strip() != "none":
            self.skip("marker-mid")
        want = [(k, st.get(k)) for k in ("marker-start", "marker-end") if st.get(k) and st[k].strip() != "none"]
        if not want:
            return
        ends = _ends(segs)
        if ends is None:
            return
        for k, value in want:
            m = re.match(r"url\(\s*['\"]?#([^)'\"]+)['\"]?\s*\)", value.strip())
            node = self.ids.get(m.group(1)) if m else None
            if node is None or node.tag != "marker" or self.depth > 32:
                self.skip("marker")
                continue
            x, y, ang = ends[0] if k == "marker-start" else ends[1]
            a = node.attrs
            orient = (a.get("orient") or "0").strip()
            if orient == "auto-start-reverse" and k == "marker-start":
                ang += 180
            elif orient not in ("auto", "auto-start-reverse"):
                ang = (numbers(orient) or [0.0])[0]
            scale = p.width if (a.get("markerunits") or "strokeWidth") != "userSpaceOnUse" else 1.0
            mw, mh = length(a.get("markerwidth"), 3.0), length(a.get("markerheight"), 3.0)
            box = numbers(a.get("viewbox"))
            sx = sy = 1.0
            if len(box) == 4 and box[2] > 0 and box[3] > 0:
                sx = sy = min(mw / box[2], mh / box[3])
            rx, ry = length(a.get("refx")), length(a.get("refy"))
            c, s = math.cos(math.radians(ang)), math.sin(math.radians(ang))
            mats = [(1, 0, 0, 1, x, y), (c, s, -s, c, 0, 0), (scale * sx, 0, 0, scale * sy, 0, 0),
                    (1, 0, 0, 1, -rx, -ry)]
            mst = self.style(node, {"_alpha": st.get("_alpha", 1.0)})
            self.wrap(mats, lambda: self.children(node, mst))

    def text(self, node: Node, st: dict) -> None:
        """Pieces [x or None (after the piece before), dx, y, text, font, size, colour, alpha, anchor] in chunks; a
        tspan with its own x or y opens a new chunk, which text-anchor places as one."""
        xs, ys = numbers(node.attrs.get("x")), numbers(node.attrs.get("y"))
        state = {"x": xs[0] if xs else 0.0, "y": ys[0] if ys else 0.0, "dx": 0.0, "fresh": True}
        chunks: list[list] = [[]]

        def place(n: Node, sst: dict, top: bool) -> None:
            if not top:
                nx, ny = numbers(n.attrs.get("x")), numbers(n.attrs.get("y"))
                if nx or ny:
                    chunks.append([])
                    state["fresh"] = True
                if nx:
                    state["x"] = nx[0]
                if ny:
                    state["y"] = ny[0]
            dx, dy = numbers(n.attrs.get("dx")), numbers(n.attrs.get("dy"))
            state["dx"] += dx[0] if dx else 0.0
            state["y"] += dy[0] if dy else 0.0
            for c in n.children:
                if isinstance(c, str):
                    t = re.sub(r"\s+", " ", c)
                    if state["fresh"]:
                        t = t.lstrip()
                    if not t:
                        continue
                    state["fresh"] = t.endswith(" ")
                    chunks[-1].append([state["x"], state["dx"], state["y"], t, _font(sst),
                                       length(sst.get("font-size"), 16.0, 16.0),
                                       self.paint_color(sst.get("fill", "black"), sst),
                                       sst.get("_alpha", 1.0) * _float(sst.get("fill-opacity"), 1.0),
                                       (sst.get("text-anchor") or "start").strip()])
                    state["x"], state["dx"] = None, 0.0
                elif isinstance(c, Node) and c.tag in ("tspan", "a"):
                    cst = self.style(c, sst)
                    if (cst.get("display") or "").strip() != "none":
                        place(c, cst, False)
                elif isinstance(c, Node) and c.tag == "textpath":
                    self.skip("textPath")
                elif isinstance(c, Node) and c.tag not in QUIET:
                    self.skip(c.tag)

        place(node, st, True)
        chunks = [c for c in chunks if c]
        if chunks:
            chunks[-1][-1][3] = chunks[-1][-1][3].rstrip() or chunks[-1][-1][3]
            self.texts.extend(p[3] for c in chunks for p in c)
            self.items.append(("text", chunks))


def _font(st: dict) -> str:
    fam = (st.get("font-family") or "").lower()
    if "mono" in fam or "courier" in fam or "consolas" in fam:
        return "c"
    weight = (st.get("font-weight") or "").strip().lower()
    if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
        return "b"
    if (st.get("font-style") or "").strip().lower() in ("italic", "oblique"):
        return "i"
    return "r"


def _float(value, default: float) -> float:
    if value is None:
        return default
    v = str(value).strip()
    try:
        n = float(v[:-1]) / 100 if v.endswith("%") else float(v)
    except ValueError:
        return default
    return min(max(n, 0.0), 1.0)


def svg(root: Node, css: list, text_width: float) -> Svg:
    """The compiled SVG of a tree whose root is the svg element; `text_width` in points for a width in %."""
    a = root.attrs
    box = numbers(a.get("viewbox"))
    ref = text_width / PX
    w = length(a.get("width"), 0.0, ref) if a.get("width") and a.get("width").strip() != "auto" else 0.0
    h = length(a.get("height"), 0.0, ref) if a.get("height") and a.get("height").strip() != "auto" else 0.0
    if not (len(box) == 4 and box[2] > 0 and box[3] > 0):
        box = [0.0, 0.0, w or 300.0, h or 150.0]
    if not w and not h:
        w, h = box[2], box[3]
    elif not w:
        w = h * box[2] / box[3]
    elif not h:
        h = w * box[3] / box[2]
    if "%" in (a.get("height") or ""):
        h = w * box[3] / box[2]
    keep = not (a.get("preserveaspectratio") or "").strip().startswith("none")
    c = Compiler(root, css)
    c.compile()
    return Svg(w * PX, h * PX, tuple(box), keep, c.items, c.texts, c.skipped)
