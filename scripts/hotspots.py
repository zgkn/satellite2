"""VIIRS I-band active fire (NOAA EFIRE) download, DBSCAN clustering and crop geometry."""
from __future__ import annotations

import datetime as dt
import math
import re
import shutil
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

import config as C
from util import UserError, annotate, log, s3_client

START_RE = re.compile(r"_s(\d{14})")
# FP_FireMask classes of the Enterprise Fire product. The file also has an FP_Confidence variable, but in
# the real files it is -99 (fill) for every fire pixel, so the mask class is the usable confidence.
MASK_CONFIDENCE = {7: "low", 8: "nominal", 9: "high"}
CONF_ORDER = ("low", "nominal", "high")


@dataclass
class Hotspots:
    lat: np.ndarray
    lon: np.ndarray
    frp: np.ndarray
    conf: np.ndarray        # "low"/"nominal"/"high" or "" when the point has none
    sat: np.ndarray         # satellite name
    t: np.ndarray           # scan start, epoch seconds (UTC)
    has_conf: bool = False
    cluster: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))

    def __len__(self) -> int:
        return int(self.lat.size)

    def time(self, i: int) -> dt.datetime:
        return dt.datetime.fromtimestamp(float(self.t[i]), dt.timezone.utc)


def parse_start(key: str) -> dt.datetime | None:
    m = START_RE.search(key.rsplit("/", 1)[-1])
    if not m:
        return None
    return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)


# --------------------------------------------------------------------------- download
def list_granules(s3, now: dt.datetime) -> list[tuple[str, str, str, dt.datetime]]:
    """(satellite, bucket, key, start) for today's and yesterday's UTC prefixes, non-empty files only."""
    found = []
    for sat, bucket in C.EFIRE_BUCKETS.items():
        n_sat = 0
        for day in (now, now - dt.timedelta(days=1)):
            prefix = f"{C.EFIRE_PREFIX}/{day:%Y/%m/%d}/"
            for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
                for obj in page.get("Contents", []):
                    start = parse_start(obj["Key"])
                    if start is None or obj["Size"] == 0 or not obj["Key"].endswith((".nc", ".h5")):
                        continue
                    found.append((sat, bucket, obj["Key"], start))
                    n_sat += 1
        log(f"  {sat}: {n_sat} granules listed (today + yesterday UTC)")
    return found


def select_window(granules, now: dt.datetime, window_h: float = C.HOTSPOT_WINDOW_H):
    """Keep granules starting within ``window_h`` before the newest scan start across all satellites."""
    if not granules:
        raise UserError("No VIIRS EFIRE granules found in today's or yesterday's UTC prefixes of any bucket.")
    newest = max(g[3] for g in granules)
    cutoff = newest - dt.timedelta(hours=window_h)
    kept = [g for g in granules if g[3] >= cutoff]
    per_sat = {}
    for g in granules:
        per_sat[g[0]] = max(per_sat.get(g[0], g[3]), g[3])
    for sat, t in sorted(per_sat.items()):
        log(f"  newest {sat} scan start: {t:%Y-%m-%d %H:%M:%S} UTC")
    age = (now - newest).total_seconds() / 60
    log(f"Newest VIIRS scan start overall: {newest:%Y-%m-%d %H:%M:%S} UTC (age {age:.0f} min); "
        f"keeping {len(kept)} granules from {cutoff:%H:%M} to {newest:%H:%M} UTC")
    return kept, newest


def _download_all(s3, kept, dest: Path) -> list[tuple[str, dt.datetime, Path]]:
    dest.mkdir(parents=True, exist_ok=True)

    def fetch(g):
        sat, bucket, key, start = g
        path = dest / f"{sat}_{key.rsplit('/', 1)[1]}"
        last = None
        for attempt in range(3):
            try:
                s3.download_file(bucket, key, str(path))
                return sat, start, path
            except Exception as exc:  # noqa: BLE001 - retried, then reported loudly
                last = exc
        raise UserError(f"Hotspot download failed for s3://{bucket}/{key}: {last}")

    with ThreadPoolExecutor(max_workers=C.DOWNLOAD_WORKERS) as pool:
        return list(pool.map(fetch, kept))


# -------------------------------------------------------------------------- reading
def _find(ds, *needles: str):
    """First variable whose lower-cased name contains one of the needles (tried in order)."""
    names = list(ds.variables)
    for needle in needles:
        for n in names:
            if needle in n.lower():
                return n
    return None


def read_granule(path: Path, sat: str, start: dt.datetime, region):
    """Hotspots of one granule inside ``region`` as a dict of arrays, or None if it has no fire pixels."""
    import xarray as xr

    engine = "h5netcdf" if path.name.endswith(".h5") else "netcdf4"
    with xr.open_dataset(path, engine=engine) as ds:
        vlat = _find(ds, "fp_latitude") or _find(ds, "latitude")
        vlon = _find(ds, "fp_longitude") or _find(ds, "longitude")
        vfrp = _find(ds, "fp_power", "fireradiativepower", "radiativepower") or _find(ds, "frp", "power")
        if not (vlat and vlon and vfrp):
            return None  # a granule without fire pixels lacks these variables
        lat = np.atleast_1d(ds[vlat].values).astype("float64")
        lon = np.atleast_1d(ds[vlon].values).astype("float64")
        frp = np.atleast_1d(ds[vfrp].values).astype("float64")
        if not (lat.size == lon.size == frp.size) or lat.size == 0:
            return None
        vmask = _find(ds, "fp_firemask")
        mask = np.atleast_1d(ds[vmask].values).astype("float64") if vmask else np.full(lat.size, np.nan)
    w, s, e, n = region
    keep = (np.isfinite(frp) & (frp >= 0) & np.isfinite(lat) & np.isfinite(lon)
            & (lon >= w) & (lon <= e) & (lat >= s) & (lat <= n))
    if not keep.any():
        return None
    conf = np.array([MASK_CONFIDENCE.get(int(v), "") if np.isfinite(v) else "" for v in mask[keep]], dtype="U8")
    k = int(keep.sum())
    return dict(lat=lat[keep], lon=lon[keep], frp=frp[keep], conf=conf,
                sat=np.full(k, sat, dtype="U8"), t=np.full(k, start.timestamp()))


def fetch_hotspots(now: dt.datetime, workdir: Path | None = None,
                   window_h: float = C.HOTSPOT_WINDOW_H) -> tuple[Hotspots, dict]:
    """Download the newest VIIRS granules and return the hotspots inside the region plus run info."""
    s3 = s3_client()
    log("Listing VIIRS EFIRE granules:")
    granules = list_granules(s3, now)
    kept, newest = select_window(granules, now, window_h)
    work = Path(tempfile.mkdtemp(prefix="efire_", dir=workdir))
    try:
        files = _download_all(s3, kept, work)
        log(f"Downloaded {len(files)} granules ({sum(p.stat().st_size for _, _, p in files) / 1e6:.0f} MB)")
        parts, n_open_fail = [], 0
        # HDF5/netCDF4 is not thread-safe: opening files from several threads segfaults, so open serially.
        for sat, start, path in files:
            if path.stat().st_size == 0:
                continue
            try:
                part = read_granule(path, sat, start, C.REGION)
            except Exception as exc:  # noqa: BLE001
                n_open_fail += 1
                log(f"  could not read {path.name}: {type(exc).__name__}: {exc}")
                continue
            if part:
                parts.append(part)
        if n_open_fail > max(2, len(files) // 10):
            raise UserError(f"{n_open_fail} of {len(files)} EFIRE granules could not be read; refusing to publish partial data.")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if parts:
        cat = {k: np.concatenate([p[k] for p in parts]) for k in parts[0]}
    else:
        cat = dict(lat=np.zeros(0), lon=np.zeros(0), frp=np.zeros(0), conf=np.zeros(0, "U8"),
                   sat=np.zeros(0, "U8"), t=np.zeros(0))
    hs = Hotspots(**cat, has_conf=bool((cat["conf"] != "").any()))
    info = dict(newest_scan_utc=newest, n_granules=len(kept),
                scan_times={s: max((g[3] for g in granules if g[0] == s), default=None) for s in C.EFIRE_BUCKETS})
    log(f"{len(hs)} hotspots inside region {C.REGION} (confidence {'available' if hs.has_conf else 'not available'})")
    return hs, info


# ------------------------------------------------------------------------ clustering
def deg_per_km_lon(lat: float) -> float:
    return 1.0 / (C.KM_PER_DEG * max(math.cos(math.radians(lat)), 0.2))


def snap_box(box, res: float, limit=C.REGION):
    """Snap ``box`` outwards to the ``res`` grid and clip it to ``limit``; returns exact (w, s, e, n)."""
    w, s, e, n = box
    iw, ie = math.floor(w / res + 1e-9), math.ceil(e / res - 1e-9)
    is_, in_ = math.floor(s / res + 1e-9), math.ceil(n / res - 1e-9)
    lw, ls, le, ln = (round(v / res) for v in limit)
    iw, ie, is_, in_ = max(iw, lw), min(ie, le), max(is_, ls), min(in_, ln)
    return tuple(round(v * res, 6) for v in (iw, is_, ie, in_))


def crop_box(cluster_bbox, res: float):
    """Cluster bbox + 50 km margin, at least 300 km square centred on the cluster, clipped to the region."""
    w, s, e, n = cluster_bbox
    clat, clon = (s + n) / 2, (w + e) / 2
    dlat = max((n - s) + 2 * C.CROP_MARGIN_KM / C.KM_PER_DEG, C.CROP_MIN_KM / C.KM_PER_DEG)
    dlon = max((e - w) + 2 * C.CROP_MARGIN_KM * deg_per_km_lon(clat), C.CROP_MIN_KM * deg_per_km_lon(clat))
    return snap_box((clon - dlon / 2, clat - dlat / 2, clon + dlon / 2, clat + dlat / 2), res)


def cluster_hotspots(hs: Hotspots) -> list[dict]:
    """DBSCAN (haversine, 20 km, min_samples=1). Returns clusters ranked by count, then summed FRP."""
    from sklearn.cluster import DBSCAN

    if not len(hs):
        return []
    xy = np.radians(np.column_stack([hs.lat, hs.lon]))
    labels = DBSCAN(eps=C.CLUSTER_EPS_KM / C.EARTH_RADIUS_KM, min_samples=1, metric="haversine",
                    algorithm="ball_tree").fit_predict(xy)
    out = []
    for lab in np.unique(labels):
        idx = np.where(labels == lab)[0]
        lat, lon = hs.lat[idx], hs.lon[idx]
        mix = Counter(c for c in hs.conf[idx] if c)
        out.append(dict(
            label=int(lab), idx=idx, count=int(idx.size), summed_frp=float(hs.frp[idx].sum()),
            centroid=(float(lon.mean()), float(lat.mean())),  # (lon, lat)
            cluster_bbox=(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())),
            newest_t=float(hs.t[idx].max()),
            confidence={k: int(mix.get(k, 0)) for k in CONF_ORDER} if hs.has_conf else None,
            satellites=sorted(set(hs.sat[idx].tolist())),
        ))
    out.sort(key=lambda c: (-c["count"], -c["summed_frp"]))
    hs.cluster = np.zeros(len(hs), dtype=int)
    for rank, c in enumerate(out, 1):
        c["rank"], c["id"] = rank, f"C{rank:02d}"
        hs.cluster[c["idx"]] = rank
    log(f"{len(out)} clusters; largest: " + ", ".join(
        f"{c['id']} ({c['count']} hs, {c['summed_frp']:.0f} MW)" for c in out[:5]))
    return out


def plan_crops(clusters: list[dict], max_images: int) -> tuple[list[dict], list[dict]]:
    """Top ``max_images`` clusters get a crop box; the rest are overflow."""
    top, overflow = clusters[:max_images], clusters[max_images:]
    for c in top:
        c["crop"] = crop_box(c["cluster_bbox"], C.CROP_RES)
    return top, overflow
