"""Is a flagged area getting worse? Compare with an earlier published run (read from the previous site contents)."""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

import config as C
from util import log

SCHEMA = 2   # clusters.json layout with tiers; earlier runs counted repeat sightings, so they are not comparable
SKIP_STATUS = ("no_hotspots", "dry_run")


def _parse(text: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _km(lon1, lat1, lon2, lat2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * C.EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def load_previous(prev_dir: Path | None, newest_hotspot: dt.datetime | None):
    """(run meta, flagged clusters) of the newest earlier run whose newest hotspot is TREND_MIN_AGE_H older, or None."""
    if prev_dir is None or newest_hotspot is None:
        return None
    try:
        runs = json.loads((Path(prev_dir) / "runs.json").read_text())
    except (OSError, ValueError):
        return None
    limit = newest_hotspot - dt.timedelta(hours=C.TREND_MIN_AGE_H)
    cands = []
    for r in runs:
        t = _parse(r.get("newest_hotspot_utc"))
        if r.get("schema") == SCHEMA and r.get("status") not in SKIP_STATUS and t is not None and t <= limit:
            cands.append((t, r))
    if not cands:
        log("Trend: no earlier comparable run on the site yet.")
        return None
    t, meta = max(cands, key=lambda x: x[0])
    try:
        cj = json.loads((Path(prev_dir) / "runs" / meta["run_id"] / "clusters.json").read_text())
    except (OSError, ValueError):
        return None
    log(f"Trend: comparing with run {meta['run_id']} (newest hotspot {t:%d %b %H:%M} UTC)")
    return meta, cj.get("clusters", []) + cj.get("overflow", [])


def annotate_trends(flagged: list[dict], prev) -> None:
    """Set ``c['trend']`` on each flagged cluster: dict(state, prev_count, prev_frp, prev_hotspot_utc) or None."""
    if prev is None:
        return
    meta, old = prev
    for c in flagged:
        lon, lat = c["centroid"]
        best, best_d = None, C.TREND_MATCH_KM
        for o in old:
            d = _km(lon, lat, o["centroid"]["lon"], o["centroid"]["lat"])
            if d <= best_d:
                best, best_d = o, d
        if best is None:
            c["trend"] = dict(state="new", prev_hotspot_utc=meta.get("newest_hotspot_utc"))
            continue
        pc, pf = best["count"], best["summed_frp"]
        grew = (c["count"] >= pc * C.TREND_RATIO and c["count"] - pc >= C.TREND_MIN_FIRES) or \
               (c["summed_frp"] >= pf * C.TREND_RATIO and c["summed_frp"] - pf >= C.TREND_MIN_MW)
        shrank = (c["count"] <= pc / C.TREND_RATIO and pc - c["count"] >= C.TREND_MIN_FIRES) or \
                 (c["summed_frp"] <= pf / C.TREND_RATIO and pf - c["summed_frp"] >= C.TREND_MIN_MW)
        c["trend"] = dict(state="escalating" if grew else "easing" if shrank and not grew else "steady",
                          prev_count=pc, prev_frp=round(pf, 1), prev_hotspot_utc=meta.get("newest_hotspot_utc"))
    n = sum(1 for c in flagged if c["trend"] and c["trend"]["state"] == "escalating")
    log(f"Trend: {n} of {len(flagged)} flagged areas escalating, "
        f"{sum(1 for c in flagged if c['trend'] and c['trend']['state'] == 'new')} newly flagged")
