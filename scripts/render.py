"""Compose the final PNG: image, graticule, wind streamlines, coastlines, hotspot rings, caption.

The PNG is exactly the lat/lon grid (no margins or title bars): pixel x = (lon - west) / res and
pixel y = (north - lat) / res, with pixel centres at +0.5, so the website can place it on a map by its bbox.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import config as C
import winds as winds_mod
from util import annotate, log

WHITE = (255, 255, 255, 255)
GREY = (205, 205, 205, 255)
COAST_DAY = (255, 60, 255, 255)      # magenta: absent from the Day Fire colour key
COAST_NIGHT = (90, 255, 150, 255)
CONF_RGB = {"low": (255, 235, 110), "nominal": (255, 255, 255), "high": (110, 235, 255)}
DAY_KEY = (("fire", (255, 70, 60)), ("smoke", (90, 150, 255)), ("cloud", (80, 245, 245)),
           ("vegetation", (70, 200, 90)), ("bare ground", (170, 110, 70)), ("water", (140, 140, 150)))


def font(size: int):
    for name in ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "LiberationSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


# ------------------------------------------------------------------------ graticule
def draw_graticule(img, bbox, res: float, step: float):
    """Faint lat/lon lines every ``step`` degrees, labelled on the top (lon) and left (lat) edges."""
    w, s, e, n = bbox
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fnt = font(max(10, round(min(img.height, 0.56 * img.width) / 55)))
    label, halo, line = (255, 255, 255, 150), (0, 0, 0, 120), (255, 255, 255, 46)

    def ticks(lo, hi):
        k = math.ceil(lo / step - 1e-9)
        while k * step <= hi + 1e-9:
            yield k * step
            k += 1

    for lon in ticks(w, e):
        x = (lon - w) / res
        d.line([(x, 0), (x, img.height)], fill=line, width=1)
        d.text((x + 3, 2), f"{abs(lon):g}\N{DEGREE SIGN}{'E' if lon >= 0 else 'W'}", font=fnt, fill=label,
               stroke_width=1, stroke_fill=halo)
    for lat in ticks(s, n):
        y = (n - lat) / res
        d.line([(0, y), (img.width, y)], fill=line, width=1)
        txt = f"{abs(lat):g}\N{DEGREE SIGN}{'N' if lat > 0 else 'S'}" if lat != 0 else "0\N{DEGREE SIGN}"
        d.text((3, y + 2), txt, font=fnt, fill=label, stroke_width=1, stroke_fill=halo)
    return Image.alpha_composite(img, layer)


# ------------------------------------------------------------------------ coastlines
_COAST_SHAPES: list | None = None


def _coast_shapes():
    """Natural Earth coastline parts that touch the study region, read once (pyshp)."""
    global _COAST_SHAPES
    if _COAST_SHAPES is None:
        import shapefile

        w, s, e, n = C.REGION
        shapes = []
        with shapefile.Reader(str(C.COASTLINE_ZIP)) as reader:
            for shp in reader.iterShapes():
                x0, y0, x1, y1 = shp.bbox
                if x1 < w - 1 or x0 > e + 1 or y1 < s - 1 or y0 > n + 1:
                    continue
                parts = list(shp.parts) + [len(shp.points)]
                shapes.extend(shp.points[a:b] for a, b in zip(parts[:-1], parts[1:]) if b - a > 1)
        _COAST_SHAPES = shapes
    return _COAST_SHAPES


def draw_coastline(img, bbox, res: float, width_px: float, rgba):
    """Natural Earth 1:10m coastlines drawn at 2x and downscaled for smooth 1.5 px lines."""
    w, s, e, n = bbox
    ss = 2
    mask = Image.new("L", (img.width * ss, img.height * ss), 0)
    draw = ImageDraw.Draw(mask)
    lw = max(1, round(width_px * ss))
    for pts in _coast_shapes():
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if max(xs) < w or min(xs) > e or max(ys) < s or min(ys) > n:
            continue
        xy = [((lon - w) / res * ss - 0.5, (n - lat) / res * ss - 0.5) for lon, lat in pts]
        draw.line(xy, fill=255, width=lw, joint="curve")
    mask = mask.resize(img.size, Image.LANCZOS)
    img.paste(Image.new("RGBA", img.size, tuple(rgba)), mask=mask)
    return img


# --------------------------------------------------------------------- hotspot rings
def ring_colour(conf: str, frp: float, has_conf: bool):
    if has_conf and conf in CONF_RGB:
        return CONF_RGB[conf]
    return (255, 235, 110) if frp < 10 else (255, 165, 60) if frp < 50 else (255, 90, 90)


def to_px(lon, lat, bbox, res):
    """Continuous pixel coordinates (pixel centre at +0.5) of lon/lat on the lat/lon grid."""
    return (np.asarray(lon) - bbox[0]) / res, (bbox[3] - np.asarray(lat)) / res


def draw_hotspots(img, bbox, res, hs, has_conf: bool, diameter: float = 6.0, ss: int = 3):
    """Small hollow circles (~``diameter`` px) at the VIIRS hotspots, light so the imagery shows through."""
    if hs is None or len(hs["lat"]) == 0:
        return img
    layer = Image.new("RGBA", (img.width * ss, img.height * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    px, py = to_px(hs["lon"], hs["lat"], bbox, res)
    r = diameter / 2 * ss
    for x, y, conf, frp in zip(px, py, hs["conf"], hs["frp"]):
        if x < -4 or y < -4 or x > img.width + 4 or y > img.height + 4:
            continue
        cx, cy = x * ss - 0.5, y * ss - 0.5
        d.ellipse([cx - r - ss, cy - r - ss, cx + r + ss, cy + r + ss], outline=(0, 0, 0, 110), width=max(1, ss))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(*ring_colour(conf, frp, has_conf), 235), width=max(1, ss))
    return Image.alpha_composite(img, layer.resize(img.size, Image.LANCZOS))


def draw_labels(img, bbox, res, labels, size: int):
    """Text labels (e.g. cluster IDs) next to given (lon, lat) points."""
    if not labels:
        return img
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    fnt = font(size)
    for label in labels:  # (text, lon, lat) or (text, lon, lat, rgba)
        text, lon, lat = label[:3]
        x, y = to_px(lon, lat, bbox, res)
        d.text((float(x) + 7, float(y) - size * 0.5), text, font=fnt, fill=label[3] if len(label) > 3 else WHITE,
               stroke_width=2, stroke_fill=(0, 0, 0, 200))
    return Image.alpha_composite(img, layer)


# ------------------------------------------------------------------------- caption
def draw_caption(img, lines, size: int, avoid_xy):
    """Semi-transparent dark box with ``lines`` (each a list of (text, rgba) pieces) in the corner farthest
    from the points ``avoid_xy`` (pixel coordinates of the cluster's hotspots)."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    while True:  # shrink the text until the box fits inside the image (narrow, edge-clipped crops)
        fnt, small = font(size), font(round(size * 0.85))
        pad, gap = max(5, size // 2), max(2, size // 5)
        m = max(4, size // 3)

        def piece_font(i):
            return fnt if i < 2 else small

        widths, heights = [], []
        for i, pieces in enumerate(lines):
            f = piece_font(i)
            widths.append(sum(d.textlength(t, font=f) for t, _ in pieces))
            heights.append(max(d.textbbox((0, 0), t, font=f)[3] for t, _ in pieces))
        bw = int(max(widths) + 2 * pad)
        bh = int(sum(heights) + gap * (len(lines) - 1) + 2 * pad)
        if (bw + 2 * m <= img.width and bh + 2 * m <= img.height) or size <= 7:
            break
        size -= 1
    corners = {
        "bottom-right": (img.width - bw - m, img.height - bh - m),
        "bottom-left": (m, img.height - bh - m),
        "top-right": (img.width - bw - m, m),
        "top-left": (m, m),
    }
    best, best_score = "bottom-right", -1.0
    pts = np.asarray(avoid_xy, dtype="float64").reshape(-1, 2)
    for name, (x0, y0) in corners.items():
        if len(pts):
            dx = np.maximum(np.maximum(x0 - pts[:, 0], pts[:, 0] - (x0 + bw)), 0)
            dy = np.maximum(np.maximum(y0 - pts[:, 1], pts[:, 1] - (y0 + bh)), 0)
            score = float(np.hypot(dx, dy).min())
        else:
            score = 0.0
        if score > best_score + 1e-6:
            best, best_score = name, score
    x0, y0 = corners[best]
    d.rectangle([x0, y0, x0 + bw, y0 + bh], fill=(0, 0, 0, 150))
    y = y0 + pad
    for i, pieces in enumerate(lines):
        f = piece_font(i)
        x = x0 + pad
        for text, fill in pieces:
            d.text((x, y - d.textbbox((0, 0), text, font=f)[1]), text, font=f, fill=fill)
            x += d.textlength(text, font=f)
        y += heights[i] + gap
    return Image.alpha_composite(img, layer), best


# --------------------------------------------------------------------------- compose
def compose(rgb, bbox, res, *, mode, hs, has_conf, wind, wind_spacing, grid_step, caption_fn,
            font_size, ring_ss=3, labels=None, avoid_lonlat=None, coast_px=1.5):
    """Draw every layer on ``rgb`` and return (PIL RGB image, wind_status).

    ``caption_fn(wind_drawn)`` returns the caption lines, so the streamline line only appears if lines were drawn.
    wind_status is 'drawn', 'off', 'masked' (all below ground in this crop) or 'failed: ...'.
    """
    img = Image.fromarray(rgb, "RGB").convert("RGBA")
    if grid_step > 0:
        img = draw_graticule(img, bbox, res, grid_step)
    status = "off"
    if wind is not None:
        try:
            img, drawn = winds_mod.draw_streamlines(
                img, wind, bbox, res, wind_spacing, winds_mod.NIGHT_LINE_RGB if mode == "night" else None)
            status = "drawn" if drawn else "masked"
        except Exception as exc:  # noqa: BLE001 - winds must never cost the image
            status = f"failed: {type(exc).__name__}: {exc}"
            annotate("warning", f"Drawing wind streamlines failed ({status}); this image has none.")
    img = draw_coastline(img, bbox, res, coast_px, COAST_NIGHT if mode == "night" else COAST_DAY)
    img = draw_hotspots(img, bbox, res, hs, has_conf, ss=ring_ss)
    img = draw_labels(img, bbox, res, labels, font_size + 1)
    if avoid_lonlat is not None and len(avoid_lonlat[0]):
        ax, ay = to_px(avoid_lonlat[0], avoid_lonlat[1], bbox, res)
        avoid = np.column_stack([ax, ay])
    else:
        avoid = np.zeros((0, 2))
    img, corner = draw_caption(img, caption_fn(status == "drawn"), font_size, avoid)
    return img.convert("RGB"), status


def save_with_thumb(img, png: Path, thumb: Path, width: int = C.THUMB_WIDTH) -> None:
    png.parent.mkdir(parents=True, exist_ok=True)
    img.save(png, optimize=True)
    t = img.copy()
    t.thumbnail((width, width * img.height // img.width + 1), Image.LANCZOS)
    t.save(thumb, quality=80, optimize=True)
