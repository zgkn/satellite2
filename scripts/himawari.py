"""Himawari-9 AHI full-disk (HSD) access: scan selection, segment geometry and the Day Fire / Night 3.9 um images."""
from __future__ import annotations

import datetime as dt
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import config as C
from util import UserError, annotate, log

KEY_RE = re.compile(
    r"HS_H09_(?P<ymd>\d{8})_(?P<hm>\d{4})_B(?P<band>\d{2})_FLDK_R(?P<res>\d{2})"
    r"_S(?P<seg>\d{2})(?P<nseg>\d{2})\.DAT(?:\.bz2)?$"
)
_REFL, _BT = "reflectance", "brightness_temperature"


# ---------------------------------------------------------------------------- geometry
def bbox_points(bbox, n: int = 60):
    """Points along the bbox edges plus an interior grid, as (lons, lats)."""
    w, s, e, nn = bbox
    lons = np.concatenate([np.linspace(w, e, n), np.full(n, e), np.linspace(e, w, n), np.full(n, w)])
    lats = np.concatenate([np.full(n, s), np.linspace(s, nn, n), np.full(n, nn), np.linspace(nn, s, n)])
    gx, gy = np.meshgrid(np.linspace(w, e, 25), np.linspace(s, nn, 25))
    return np.concatenate([lons, gx.ravel()]), np.concatenate([lats, gy.ravel()])


def needed_segments(bbox) -> list[int]:
    """Full-disk segments (1 = north .. 10) that ``bbox`` touches, from the geostationary projection."""
    from pyproj import CRS, Transformer

    lons, lats = bbox_points(bbox)
    tr = Transformer.from_crs("EPSG:4326", CRS.from_dict(C.GEOS_CRS), always_xy=True)
    x, y = tr.transform(lons, lats)
    if not (np.isfinite(x).all() and np.isfinite(y).all()):
        raise UserError(f"bbox {bbox} is not entirely visible from Himawari-9.")
    seg_h = 2 * C.FD_HALF_HEIGHT_M / C.N_SEGMENTS
    idx = np.floor((C.FD_HALF_HEIGHT_M - y) / seg_h).astype(int) + 1
    if idx.min() < 1 or idx.max() > C.N_SEGMENTS:
        raise UserError(f"bbox {bbox} extends beyond the Himawari-9 full disk.")
    return list(range(int(idx.min()), int(idx.max()) + 1))


def sun_zenith(t: dt.datetime, lon, lat):
    from pyorbital.astronomy import sun_zenith_angle

    return sun_zenith_angle(t.replace(tzinfo=None), lon, lat)


def mode_at(t: dt.datetime, lon: float, lat: float) -> tuple[str, float]:
    """('day' | 'night', sun zenith) at a point. Zenith below 90 deg is day."""
    z = float(sun_zenith(t, lon, lat))
    return ("day" if z < 90.0 else "night"), z


def target_area(bbox, res: float):
    from pyresample.geometry import AreaDefinition

    w, s, e, n = bbox
    width, height = round((e - w) / res), round((n - s) / res)
    area = AreaDefinition("crop", "crop lat/lon", "crop",
                          {"proj": "longlat", "datum": "WGS84", "no_defs": None},
                          width, height, (w, n - height * res, w + width * res, n))
    return area, width, height


def check_loaded_coverage(area, bbox) -> None:
    """Fail clearly if the loaded segments do not cover the whole bbox."""
    from pyproj import Transformer

    lons, lats = bbox_points(bbox)
    x, y = Transformer.from_crs("EPSG:4326", area.crs, always_xy=True).transform(lons, lats)
    x0, y0, x1, y1 = area.area_extent
    px = abs(area.pixel_size_x)
    ok = np.isfinite(x) & np.isfinite(y) & (x >= x0 - px) & (x <= x1 + px) & (y >= y0 - px) & (y <= y1 + px)
    if not ok.all():
        raise UserError(f"The downloaded segments do not cover the whole bbox {bbox} "
                        f"(loaded y-extent {y0:.0f}..{y1:.0f} m, bbox needs {np.nanmin(y):.0f}..{np.nanmax(y):.0f} m).")


# ------------------------------------------------------------------------------ bucket
def scan_prefix(t: dt.datetime) -> str:
    return f"{C.HIMAWARI_PRODUCT}/{t:%Y/%m/%d/%H%M}/"


def list_scan(s3, t: dt.datetime) -> dict[tuple[str, int], str]:
    """{(band, segment): key} of the non-empty 10-segment files of scan ``t`` for the bands we use."""
    found = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=C.HIMAWARI_BUCKET, Prefix=scan_prefix(t)):
        for obj in page.get("Contents", []):
            m = KEY_RE.search(obj["Key"])
            if not m or int(m["nseg"]) != C.N_SEGMENTS or obj["Size"] == 0:
                continue
            band = f"B{m['band']}"
            if band in C.DAY_BANDS:
                found[(band, int(m["seg"]))] = obj["Key"]
    return found


def required_files(t: dt.datetime, jobs: list[dict]) -> set[tuple[str, int]]:
    """(band, segment) pairs needed at scan time ``t``: day jobs need 4 bands, night jobs 2."""
    need = set()
    for job in jobs:
        lon, lat = job["centroid"]
        mode, _ = mode_at(t, lon, lat)
        for band in (C.DAY_BANDS if mode == "day" else C.NIGHT_BANDS):
            need.update((band, seg) for seg in job["segments"])
    return need


def select_scan(s3, jobs: list[dict], requested: dt.datetime | None = None):
    """Newest scan (walking back in 10 min steps for up to 3 h) with every needed file present.

    Returns (scan_time, {(band, seg): key}) or (None, None) if there is none. An explicit ``requested``
    scan that is missing or incomplete is an error.
    """
    if requested is not None:
        found = list_scan(s3, requested)
        miss = sorted(required_files(requested, jobs) - set(found))
        if miss:
            hint = (" (the 0240 and 1440 UTC scans are always missing from the bucket)"
                    if f"{requested:%H%M}" in ("0240", "1440") else "")
            raise UserError(f"Scan {requested:%Y-%m-%d %H%M} UTC is incomplete or missing{hint}: "
                            f"{len(miss)} files absent, e.g. {miss[:4]}.")
        return requested, found
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(minute=now.minute // 10 * 10, second=0, microsecond=0)
    oldest = now - dt.timedelta(hours=C.SCAN_LOOKBACK_H)
    checked = 0
    while t >= oldest:
        found = list_scan(s3, t)
        checked += 1
        miss = required_files(t, jobs) - set(found)
        if not miss:
            log(f"Newest complete Himawari scan: {t:%Y-%m-%d %H%M} UTC (checked {checked} scans)")
            return t, found
        t -= dt.timedelta(minutes=10)
    annotate("warning", f"No complete Himawari-9 scan found in the last {C.SCAN_LOOKBACK_H:g} h "
                        f"({checked} scans checked); imagery is skipped.")
    return None, None


def download(s3, keys: dict[tuple[str, int], str], needed: set[tuple[str, int]], dest: Path) -> dict[tuple[str, int], Path]:
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {k: keys[k] for k in needed}

    def fetch(item):
        k, key = item
        path = dest / key.rsplit("/", 1)[1]
        s3.download_file(C.HIMAWARI_BUCKET, key, str(path))
        return k, path

    with ThreadPoolExecutor(max_workers=6) as pool:
        out = dict(pool.map(fetch, wanted.items()))
    log(f"Downloaded {len(out)} Himawari files ({sum(p.stat().st_size for p in out.values()) / 1e6:.0f} MB)")
    return out


# --------------------------------------------------------------------------- imagery
def _scale(x, vmin, vmax, gamma=1.0):
    return np.power(np.clip((x - vmin) / (vmax - vmin), 0.0, 1.0), 1.0 / gamma)


def _load(files: dict[tuple[str, int], Path], segments, bands, bbox, res):
    """Resample ``bands`` of ``segments`` to the lat/lon grid of ``bbox``: {band: float32 (H, W)}."""
    import dask
    from satpy import Scene

    dask.config.set({"array.chunk-size": "64MiB"})
    area, width, height = target_area(bbox, res)
    paths = [str(files[(b, s)]) for b in bands for s in segments]
    scn = Scene(filenames=paths, reader="ahi_hsd")
    refl = [b for b in bands if b in ("B03", "B04")]
    bt = [b for b in bands if b in ("B07", "B13")]
    # plain albedo / brightness temperature: no Rayleigh or sun correction from Satpy (no modifiers)
    if refl:
        scn.load(refl, calibration=_REFL)
    if bt:
        scn.load(bt, calibration=_BT)
    check_loaded_coverage(scn[bands[0]].attrs["area"], bbox)
    out_scn = scn.resample(area, resampler="nearest")
    return {b: np.asarray(dask.compute(out_scn[b].data)[0], dtype="float32") for b in bands}, width, height


def zenith_grid(scan: dt.datetime, bbox, res: float, width: int, height: int):
    w, _, _, n = bbox
    lons = np.broadcast_to((w + (np.arange(width) + 0.5) * res)[None, :], (height, width))
    lats = np.broadcast_to((n - (np.arange(height) + 0.5) * res)[:, None], (height, width))
    return sun_zenith(scan, lons, lats)


def day_fire_rgb(ch, scan, bbox, res):
    """NOAA/CIRA Day Fire RGB on AHI bands, sun-corrected, with the cold-cloud grey cue. uint8 (H, W, 3)."""
    b7, b4, b3, b13 = ch["B07"], ch["B04"], ch["B03"], ch["B13"]
    h, w = b7.shape
    zen = zenith_grid(scan, bbox, res, w, h)
    cos_floor = np.cos(np.radians(80.0))
    corr = (1.0 / np.maximum(np.cos(np.radians(zen)), cos_floor)).astype("float32")
    r = np.nan_to_num(_scale(b7, 300.15, 333.15, 0.4), nan=0.0)
    g = np.nan_to_num(_scale(b4 * corr, 0.0, 100.0, 1.0), nan=0.0)
    b = np.nan_to_num(_scale(b3 * corr, 0.0, 100.0, 1.0), nan=0.0)
    rgb = np.stack([r, g, b], axis=-1)
    weight = np.nan_to_num(np.clip((290.0 - b13) / (290.0 - 280.0), 0.0, 1.0), nan=0.0)
    weight = (weight * (1.0 - np.clip((r - 0.35) / 0.30, 0.0, 1.0)))[..., None]  # never grey out a hotspot
    grey = np.maximum(g, b)[..., None]
    out = (1.0 - weight) * rgb + weight * grey
    return np.round(out * 255.0).astype("uint8")


# dark base -> deep red -> red -> orange -> yellow -> white
_HOT_STOPS = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
_HOT_RGB = np.array([[8, 8, 14], [110, 10, 10], [230, 40, 10], [255, 160, 20], [255, 255, 200]], dtype="float64")


def night_rgb(ch):
    """3.9 um brightness temperature 290..340 K on a hot colormap; B13 below 280 K shown as faint grey."""
    b7, b13 = ch["B07"], ch["B13"]
    t = np.nan_to_num(_scale(b7, 290.0, 340.0, 1.0), nan=0.0)
    hot = np.stack([np.interp(t, _HOT_STOPS, _HOT_RGB[:, i]) for i in range(3)], axis=-1) / 255.0
    cold = np.nan_to_num(np.clip((C.OBSCURED_BT_K - b13) / 40.0, 0.0, 1.0), nan=0.0)
    cloud_on = np.nan_to_num((b13 < C.OBSCURED_BT_K).astype("float32"), nan=0.0)
    grey_level = (0.16 + 0.22 * cold) * cloud_on
    grey = np.stack([grey_level] * 3, axis=-1)
    base = np.array([8, 8, 14]) / 255.0
    cloud_img = np.where(cloud_on[..., None] > 0, grey, base)
    alpha = np.clip(t * 2.2, 0.0, 1.0)[..., None]  # faint warm ground stays the dark base
    out = (1.0 - alpha) * cloud_img + alpha * hot
    return np.round(np.clip(out, 0.0, 1.0) * 255.0).astype("uint8")


def make_image(files, segments, bbox, res, mode: str, scan: dt.datetime) -> tuple[np.ndarray, dict]:
    """Return (uint8 RGB array, info) for one crop. ``info`` has the cloudy fraction and band statistics."""
    bands = C.DAY_BANDS if mode == "day" else C.NIGHT_BANDS
    ch, width, height = _load(files, segments, bands, bbox, res)
    nan_frac = float(np.isnan(ch["B07"]).mean())
    if nan_frac > 0.01:
        annotate("warning", f"{nan_frac:.1%} of pixels have no data")
    b13 = ch["B13"]
    valid = np.isfinite(b13)
    cloud_frac = float(((b13 < C.OBSCURED_BT_K) & valid).sum() / max(int(valid.sum()), 1))
    rgb = day_fire_rgb(ch, scan, bbox, res) if mode == "day" else night_rgb(ch)
    info = dict(cloud_fraction=round(cloud_frac, 3), obscured=cloud_frac > C.OBSCURED_FRACTION,
                b07_max_k=float(np.nanmax(ch["B07"])) if np.isfinite(ch["B07"]).any() else None)
    return rgb, info
