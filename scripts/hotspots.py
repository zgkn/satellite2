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
    seen: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=int))   # passes that saw this fire

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
    n_raw = len(hs)
    log(f"{n_raw} detections inside region {C.REGION} (confidence {'available' if hs.has_conf else 'not available'})")
    hs = dedupe(hs)
    log(f"{len(hs)} distinct fires after merging repeat sightings within {C.DEDUP_KM:g} km "
        f"({n_raw - len(hs)} duplicates from other passes or satellites)")
    info = dict(newest_scan_utc=newest, n_granules=len(kept), n_raw=n_raw,
                scan_times={s: max((g[3] for g in granules if g[0] == s), default=None) for s in C.EFIRE_BUCKETS})
    return hs, info


# --------------------------------------------------------------------------- merging
CONF_RANK = {"": 0, "low": 1, "nominal": 2, "high": 3}
RANK_CONF = {v: k for k, v in CONF_RANK.items()}


def _pass_ids(hs: Hotspots, gap_s: float = 1800.0) -> np.ndarray:
    """Overpass number per detection: detections of one satellite more than 30 min apart are different passes."""
    pid = np.zeros(len(hs), dtype=int)
    nxt = 0
    for sat in np.unique(hs.sat):
        m = np.where(hs.sat == sat)[0]
        times = np.unique(hs.t[m])
        cur, last = nxt, None
        ids = {}
        for t in times:
            if last is not None and t - last > gap_s:
                cur += 1
            ids[t] = cur
            last = t
        for i in m:
            pid[i] = ids[hs.t[i]]
        nxt = cur + 1
    return pid


def dedupe(hs: Hotspots, km: float = C.DEDUP_KM) -> Hotspots:
    """Merge repeat sightings of one fire seen by another satellite or in another pass.

    Passes are taken newest first. A detection from an older pass within ``km`` of a kept fire is the same fire: it is
    dropped, but the kept fire takes the larger FRP and the higher confidence class. Detections within one pass are
    never merged (neighbouring 375 m pixels are distinct fire pixels).
    """
    from sklearn.neighbors import BallTree

    n = len(hs)
    if n == 0:
        hs.seen = np.zeros(0, dtype=int)
        return hs
    pid = _pass_ids(hs)
    order = sorted(np.unique(pid), key=lambda p: -hs.t[pid == p].max())
    rank = np.array([CONF_RANK.get(c, 0) for c in hs.conf])
    keep_idx = np.zeros(0, dtype=int)
    frp = hs.frp.copy()
    seen = np.ones(n, dtype=int)
    for p in order:
        idx = np.where(pid == p)[0]
        if keep_idx.size:
            tree = BallTree(np.radians(np.column_stack([hs.lat[keep_idx], hs.lon[keep_idx]])), metric="haversine")
            dist, nn = tree.query(np.radians(np.column_stack([hs.lat[idx], hs.lon[idx]])), k=1)
            dup = dist[:, 0] * C.EARTH_RADIUS_KM < km
            target = keep_idx[nn[dup, 0]]
            np.maximum.at(frp, target, hs.frp[idx[dup]])
            np.maximum.at(rank, target, rank[idx[dup]])
            np.add.at(seen, target, 1)
            idx = idx[~dup]
        keep_idx = np.concatenate([keep_idx, idx])
    keep_idx.sort()
    out = Hotspots(lat=hs.lat[keep_idx], lon=hs.lon[keep_idx], frp=frp[keep_idx],
                   conf=np.array([RANK_CONF[r] for r in rank[keep_idx]], dtype="U8"),
                   sat=hs.sat[keep_idx], t=hs.t[keep_idx], has_conf=hs.has_conf)
    out.seen = seen[keep_idx]
    return out


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


TIER_RANK = {"severe": 2, "watch": 1, "none": 0}


def cluster_hotspots(hs: Hotspots) -> list[dict]:
    """DBSCAN (haversine, 20 km, min_samples=1), then the severity rules.

    Each cluster gets: ``core_fires`` (the most distinct fire pixels within DENSE_RADIUS_KM of any one fire), ``max_frp``,
    ``dense`` / ``intense`` flags, a ``tier`` (severe = both, watch = one, none) and the ``reasons`` behind it.
    Clusters are ranked by tier, then summed FRP, then fire count; the rank gives the id (C01 = worst).
    """
    from sklearn.cluster import DBSCAN
    from sklearn.neighbors import BallTree

    if not len(hs):
        return []
    xy = np.radians(np.column_stack([hs.lat, hs.lon]))
    labels = DBSCAN(eps=C.CLUSTER_EPS_KM / C.EARTH_RADIUS_KM, min_samples=1, metric="haversine",
                    algorithm="ball_tree").fit_predict(xy)
    near = BallTree(xy, metric="haversine").query_radius(xy, r=C.DENSE_RADIUS_KM / C.EARTH_RADIUS_KM, count_only=True)
    out = []
    for lab in np.unique(labels):
        idx = np.where(labels == lab)[0]
        lat, lon = hs.lat[idx], hs.lon[idx]
        mix = Counter(c for c in hs.conf[idx] if c)
        total, peak, core = float(hs.frp[idx].sum()), float(hs.frp[idx].max()), int(near[idx].max())
        dense = core >= C.DENSE_MIN_FIRES
        intense = total >= C.INTENSE_SUM_MW or peak >= C.INTENSE_PIXEL_MW
        reasons = []
        if dense:
            reasons.append(f"{core} fires within {C.DENSE_RADIUS_KM:g} km")
        if intense:
            reasons.append(f"{total:,.0f} MW total" if total >= C.INTENSE_SUM_MW else f"{peak:.0f} MW single pixel")
        out.append(dict(
            label=int(lab), idx=idx, count=int(idx.size), summed_frp=total, max_frp=peak, core_fires=core,
            dense=bool(dense), intense=bool(intense),
            tier="severe" if dense and intense else "watch" if dense or intense else "none", reasons=reasons,
            centroid=(float(lon.mean()), float(lat.mean())),  # (lon, lat)
            cluster_bbox=(float(lon.min()), float(lat.min()), float(lon.max()), float(lat.max())),
            newest_t=float(hs.t[idx].max()),
            confidence={k: int(mix.get(k, 0)) for k in CONF_ORDER} if hs.has_conf else None,
            satellites=sorted(set(hs.sat[idx].tolist())), trend=None,
        ))
    out.sort(key=lambda c: (-TIER_RANK[c["tier"]], -c["summed_frp"], -c["count"]))
    hs.cluster = np.zeros(len(hs), dtype=int)
    for rank, c in enumerate(out, 1):
        c["rank"], c["id"] = rank, f"C{rank:02d}"
        hs.cluster[c["idx"]] = rank
    n_sev = sum(c["tier"] == "severe" for c in out)
    n_watch = sum(c["tier"] == "watch" for c in out)
    log(f"{len(out)} clusters: {n_sev} severe, {n_watch} watch, {len(out) - n_sev - n_watch} ignored. Worst: " + ", ".join(
        f"{c['id']} {c['tier']} ({c['count']} fires, {c['core_fires']} within {C.DENSE_RADIUS_KM:g} km, "
        f"{c['summed_frp']:.0f} MW)" for c in out[:5]))
    return out


def plan_crops(clusters: list[dict], max_images: int) -> tuple[list[dict], list[dict], list[dict]]:
    """Flagged clusters (severe first, then watch): the top ``max_images`` get a crop box, the rest are overflow.

    Returns (imaged, overflow, ignored); ignored clusters are neither severe nor watch and are not imaged.
    """
    flagged = [c for c in clusters if c["tier"] != "none"]
    ignored = [c for c in clusters if c["tier"] == "none"]
    top, overflow = flagged[:max_images], flagged[max_images:]
    for c in top:
        c["crop"] = crop_box(c["cluster_bbox"], C.CROP_RES)
    return top, overflow, ignored
