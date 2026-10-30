"""ECMWF open-data (IFS 0.25 deg) 925 hPa wind field and a streamline overlay.

Data: s3://ecmwf-forecasts/YYYYMMDD/HHz/ifs/0p25/oper/YYYYMMDDHH0000-{step}h-oper-fc.grib2 (anonymous). Every GRIB
file has a ``.index`` next to it, so only the u and v messages at the chosen level and the surface pressure are
fetched with byte-range requests (~2 MB per step instead of ~100 MB). Fetched once per run, then cropped per image.
ECMWF open data is CC-BY-4.0. Streamlines are model output, not observations.
"""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass

BUCKET = "ecmwf-forecasts"
RUN_HOURS = 6        # runs at 00/06/12/18 UTC
STEP_HOURS = 3       # 3-hourly steps for every run up to +90 h
MAX_STEP = 90
MAX_RUNS_BACK = 8    # look back at most 48 h for a published run

LINE_RGB = (255, 170, 30)     # orange: shows over dark ocean/land and over bright cloud
NIGHT_LINE_RGB = (255, 255, 255)
CASING_RGB = (0, 0, 0)
DPI = 100
SPACING_FACTOR = 0.5
MAX_LENGTH = 0.22             # longest streamline, in axes widths; shorter lines mean more arrowheads


class WindError(Exception):
    """Winds could not be fetched or drawn (the caller decides what to do about it)."""


@dataclass
class WindField:
    lon: "np.ndarray"          # 1-D ascending, degrees east
    lat: "np.ndarray"          # 1-D ascending
    u: "np.ndarray"            # m/s (lat, lon), interpolated in time to the scan
    v: "np.ndarray"
    below_ground: "np.ndarray"  # True where the pressure level is under the surface
    level: int
    init: dt.datetime
    step_lo: int
    step_hi: int

    def describe(self) -> str:
        steps = f"+{self.step_lo} h" if self.step_lo == self.step_hi else f"+{self.step_lo}/+{self.step_hi} h"
        return f"ECMWF IFS {self.level} hPa, run {self.init:%d %b %HZ}, {steps}"

    def crop(self, bbox, margin: float = 0.5) -> "WindField":
        import numpy as np

        w, s, e, n = bbox
        ix = np.where((self.lon >= w - margin) & (self.lon <= e + margin))[0]
        iy = np.where((self.lat >= s - margin) & (self.lat <= n + margin))[0]
        if ix.size < 3 or iy.size < 3:
            raise WindError("crop is too small or outside the ECMWF grid")
        sl = (slice(iy[0], iy[-1] + 1), slice(ix[0], ix[-1] + 1))
        return WindField(self.lon[sl[1]], self.lat[sl[0]], self.u[sl], self.v[sl], self.below_ground[sl],
                         self.level, self.init, self.step_lo, self.step_hi)


# ------------------------------------------------------------------------- fetching
def _key(init: dt.datetime, step: int, ext: str) -> str:
    return f"{init:%Y%m%d}/{init:%H}z/ifs/0p25/oper/{init:%Y%m%d%H}0000-{step}h-oper-fc.{ext}"


def _exists(s3, key: str) -> bool:
    """True if the object exists (listing by exact key; works where HEAD is refused)."""
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=key, MaxKeys=1)
    return any(o["Key"] == key and o["Size"] > 0 for o in resp.get("Contents", []))


def find_run(s3, scan: dt.datetime):
    """Newest published run that started at or before ``scan``; returns (init, step_lo, step_hi)."""
    init = scan.replace(hour=scan.hour // RUN_HOURS * RUN_HOURS, minute=0, second=0, microsecond=0)
    for _ in range(MAX_RUNS_BACK):
        hours = (scan - init).total_seconds() / 3600
        lo = int(hours // STEP_HOURS) * STEP_HOURS
        hi = lo if hours == lo else lo + STEP_HOURS
        if hi <= MAX_STEP and all(_exists(s3, _key(init, s, ext)) for s in {lo, hi} for ext in ("grib2", "index")):
            return init, lo, hi
        init -= dt.timedelta(hours=RUN_HOURS)
    raise WindError(f"no published ECMWF open-data run found within {MAX_RUNS_BACK * RUN_HOURS} h before "
                    f"{scan:%Y-%m-%d %H:%M} UTC in s3://{BUCKET}/")


def _select_records(index_text: str, level: int):
    wanted = []
    for line in index_text.splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        lev = str(rec.get("levelist", ""))
        is_wind = rec.get("levtype") == "pl" and lev == str(level) and rec.get("param") in ("u", "v")
        is_sp = rec.get("levtype") == "sfc" and rec.get("param") == "sp"
        if is_wind or is_sp:
            wanted.append(rec)
    return wanted


def _fetch_messages(s3, init: dt.datetime, step: int, level: int) -> bytes:
    index = s3.get_object(Bucket=BUCKET, Key=_key(init, step, "index"))["Body"].read().decode()
    wanted = _select_records(index, level)
    if sorted(r["param"] for r in wanted) != ["sp", "u", "v"]:
        raise WindError(f"expected u, v @ {level} hPa and sp in the ECMWF index for step {step}, "
                        f"found {sorted(r.get('param') for r in wanted)}")
    chunks = []
    for rec in sorted(wanted, key=lambda r: r["_offset"]):
        rng = f"bytes={rec['_offset']}-{rec['_offset'] + rec['_length'] - 1}"
        chunks.append(s3.get_object(Bucket=BUCKET, Key=_key(init, step, "grib2"), Range=rng)["Body"].read())
    return b"".join(chunks)


def _decode(data: bytes, bbox, margin: float):
    """GRIB messages -> ({shortName: 2-D array (lat asc, lon asc)}, lon, lat) cropped to bbox + margin."""
    import eccodes
    import numpy as np

    fields = {}
    pos = 0
    while pos < len(data):
        total = int.from_bytes(data[pos + 8:pos + 16], "big")  # message length, bytes 8-16 of a GRIB2 message
        handle = eccodes.codes_new_from_message(data[pos:pos + total])
        pos += total
        try:
            name = eccodes.codes_get(handle, "shortName")
            ni, nj = eccodes.codes_get(handle, "Ni"), eccodes.codes_get(handle, "Nj")
            vals = eccodes.codes_get_values(handle).reshape(nj, ni)
            lats = np.linspace(eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees"),
                               eccodes.codes_get(handle, "latitudeOfLastGridPointInDegrees"), nj)
            lons = (eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees")
                    + np.arange(ni) * eccodes.codes_get(handle, "iDirectionIncrementInDegrees")) % 360
        finally:
            eccodes.codes_release(handle)
        if lats[0] > lats[-1]:  # north-to-south -> south-to-north
            lats, vals = lats[::-1], vals[::-1]
        fields[name] = (vals, lons, lats)

    _, lons, lats = next(iter(fields.values()))
    w, s, e, n = bbox
    if np.any(np.diff(lons) <= 0):
        order = np.argsort(lons)
        lons = lons[order]
        fields = {k: (v[:, order], lons, lats) for k, (v, _, _) in fields.items()}
    ix = np.where((lons >= w - margin) & (lons <= e + margin))[0]
    iy = np.where((lats >= s - margin) & (lats <= n + margin))[0]
    if ix.size < 3 or iy.size < 3:
        raise WindError("bbox is too small or outside the ECMWF grid")
    crop = {k: v[np.ix_(iy, ix)].astype("float64") for k, (v, _, _) in fields.items()}
    return crop, lons[ix], lats[iy]


def fetch_winds(s3, scan: dt.datetime, level: int, bbox, margin: float = 1.0) -> WindField:
    """Winds at ``level`` hPa linearly interpolated in time to ``scan`` (UTC)."""
    import numpy as np

    init, lo, hi = find_run(s3, scan)
    weight = 0.0 if hi == lo else (scan - (init + dt.timedelta(hours=lo))).total_seconds() / (STEP_HOURS * 3600)
    stacks, lon, lat = [], None, None
    for step in sorted({lo, hi}):
        fields, lon, lat = _decode(_fetch_messages(s3, init, step, level), bbox, margin)
        stacks.append(fields)
    a, b = stacks[0], stacks[-1]

    def mix(k):
        return (1 - weight) * a[k] + weight * b[k]

    sp = np.minimum(a["sp"], b["sp"])  # mask if either bracketing step is underground
    return WindField(lon=lon, lat=lat, u=mix("u"), v=mix("v"), below_ground=sp < level * 100.0,
                     level=level, init=init, step_lo=lo, step_hi=hi)


# --------------------------------------------------------------------------- drawing
def draw_streamlines(img, wind: WindField, bbox, res: float, spacing_px: float, line_rgb=None):
    """Composite thin streamlines onto the RGBA lat/lon image ``img``; returns (image, drawn).

    ``drawn`` is False when every wind point in the crop is below ground (nothing to draw).
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from PIL import Image

    width, height = img.size
    w, _, _, n = bbox
    wind = wind.crop(bbox)
    if wind.below_ground.all():
        return img, False
    x0, x1, y1, y0 = w, w + width * res, n, n - height * res

    speed = np.hypot(wind.u, wind.v)
    cos_lat = np.cos(np.deg2rad(wind.lat))[:, None]
    # m/s components on a plate-carree canvas: scale u by 1/cos(lat) to keep the drawn direction true
    u = np.ma.masked_where(wind.below_ground, wind.u / cos_lat)
    v = np.ma.masked_where(wind.below_ground, wind.v)
    linewidth = 0.7 + 0.9 * np.clip(speed / 12.0, 0.0, 1.0)  # points; 1 pt = 1.39 px at 100 dpi
    # streamplot's mask covers the wind grid, not the axes, so size the density from the grid's pixel span
    grid_w_px = (wind.lon[-1] - wind.lon[0]) / res
    grid_h_px = (wind.lat[-1] - wind.lat[0]) / res
    # matplotlib needs about two mask cells between neighbouring lines, so use cells of half the spacing
    cell = SPACING_FACTOR * spacing_px
    density = (max(grid_w_px / (30.0 * cell), 0.3), max(grid_h_px / (30.0 * cell), 0.3))
    arrowsize = 1.5 if spacing_px <= 35 else 1.9   # arrowheads scale with the small crops

    fig = plt.figure(figsize=(width / DPI, height / DPI), dpi=DPI)
    try:
        fig.patch.set_alpha(0)
        ax = fig.add_axes([0, 0, 1, 1])
        ax.set_axis_off()
        ax.patch.set_alpha(0)
        ax.set_xlim(x0, x1)
        ax.set_ylim(y0, y1)
        for rgb, extra, alpha in ((CASING_RGB, 1.0, 0.30), (line_rgb or LINE_RGB, 0.0, 0.92)):
            sset = ax.streamplot(wind.lon, wind.lat, u, v, density=density, color=tuple(c / 255 for c in rgb),
                                 linewidth=linewidth + extra, arrowsize=arrowsize, maxlength=MAX_LENGTH,
                                 broken_streamlines=True)
            sset.lines.set_alpha(alpha)
            sset.arrows.set_alpha(alpha)
        fig.canvas.draw()
        layer = np.asarray(fig.canvas.buffer_rgba()).copy()
    finally:
        plt.close(fig)
    if layer.shape[:2] != (height, width):
        if abs(layer.shape[0] - height) > 2 or abs(layer.shape[1] - width) > 2:
            raise WindError(f"streamline layer is {layer.shape[1]}x{layer.shape[0]}, expected {width}x{height}")
        layer_img = Image.fromarray(layer, "RGBA").resize((width, height), Image.NEAREST)
    else:
        layer_img = Image.fromarray(layer, "RGBA")
    return Image.alpha_composite(img, layer_img), True
