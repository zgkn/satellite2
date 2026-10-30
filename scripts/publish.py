#!/usr/bin/env python3
"""Assemble the publish folder for the ``site`` branch.

    python scripts/publish.py --output output --publish pub

``pub`` already holds the previous contents of the ``site`` branch (the workflow unpacks it there, if the branch
exists). This script adds the static site files and the new run, drops runs older than 48 h, rewrites runs.json
(newest first) and LATEST.md, and leaves a folder ready to be committed as a single orphan commit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
from util import log  # noqa: E402

SITE_DIR = C.ROOT / "site"


def parse_iso(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def load_runs(pub: Path) -> list[dict]:
    try:
        data = json.loads((pub / "runs.json").read_text())
        return [r for r in data if isinstance(r, dict) and r.get("run_id")]
    except (OSError, ValueError):
        return []


def latest_md(meta: dict, clusters: dict) -> str:
    rid = meta["run_id"]
    out = ["# Latest fire map run", "",
           f"Run `{rid}` (status: **{meta['status']}**). Open the map website for the interactive view.", ""]
    if meta.get("scan_utc"):
        out.append(f"- Himawari-9 scan: {meta['scan_utc']} / {meta['scan_sgt']}")
    if meta.get("newest_hotspot_utc"):
        out.append(f"- Newest VIIRS hotspot: {meta['newest_hotspot_utc']} ({meta['hotspot_age_min']} min old at run time)")
    out.append(f"- Hotspots: {meta['n_hotspots']} in {meta['n_clusters']} clusters; "
               f"{meta['n_imaged']} imaged, {meta['n_overflow']} not individually imaged")
    out.append(f"- Winds: {meta['winds']}")
    out.append("")
    if meta["status"] == "no_hotspots":
        out.append("No VIIRS hotspots were found in the region in the selected window.")
    elif meta["status"] == "no_imagery":
        out.append("Hotspots were found, but no complete Himawari-9 scan was available; no imagery was made.")
    for c in clusters.get("clusters", []):
        out.append(f"## {c['id']}: {c['count']} hotspots, {c['summed_frp']:.0f} MW")
        out.append("")
        out.append(f"Centroid {c['centroid']['lat']:.2f}, {c['centroid']['lon']:.2f}; mode: {c.get('mode') or 'n/a'}"
                   + (" (obscured by cloud)" if c.get("obscured") else ""))
        out.append("")
        if c.get("png"):
            out.append(f"![{c['id']}](runs/{rid}/{c['png']})")
        else:
            out.append(f"_No image: {c.get('image_error') or 'not available'}_")
        out.append("")
    if clusters.get("overview"):
        out += ["## Overview", "", f"![overview](runs/{rid}/{clusters['overview']['png']})", ""]
    out += ["---", "Data: NOAA VIIRS EFIRE, JMA Himawari-9 via NOAA NODD, ECMWF open data (CC-BY-4.0). "
            "Streamlines are model output, not observations.", ""]
    return "\n".join(out)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--output", required=True, help="pipeline output dir (has run_meta.json and runs/)")
    p.add_argument("--publish", required=True, help="publish folder (previous site branch contents, updated in place)")
    args = p.parse_args()
    out, pub = Path(args.output), Path(args.publish)
    pub.mkdir(parents=True, exist_ok=True)

    meta = json.loads((out / "run_meta.json").read_text())
    rid = meta["run_id"]
    now = dt.datetime.now(dt.timezone.utc)

    for f in SITE_DIR.iterdir():  # static site: index.html, app.js, style.css
        if f.is_file():
            shutil.copy2(f, pub / f.name)
    (pub / ".nojekyll").write_text("")

    (pub / "runs").mkdir(exist_ok=True)
    dest = pub / "runs" / rid
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(out / "runs" / rid, dest)

    runs = [r for r in load_runs(pub) if r["run_id"] != rid] + [meta]
    cutoff = now - dt.timedelta(hours=C.KEEP_HOURS)
    keep = []
    for r in runs:
        try:
            ok = parse_iso(r["generated_utc"]) >= cutoff
        except (KeyError, ValueError):
            ok = False
        if ok or r["run_id"] == rid:
            keep.append(r)
    keep.sort(key=lambda r: r["generated_utc"], reverse=True)
    keep_ids = {r["run_id"] for r in keep}
    for d in (pub / "runs").iterdir():  # drop old runs, and any folder runs.json does not know
        if d.is_dir() and d.name not in keep_ids:
            shutil.rmtree(d)
            log(f"Dropped old run {d.name}")
    (pub / "runs.json").write_text(json.dumps(keep, indent=1))
    clusters = json.loads((dest / "clusters.json").read_text())
    (pub / "LATEST.md").write_text(latest_md(meta, clusters))
    log(f"Publish folder ready: {len(keep)} run(s), newest {keep[0]['run_id']}; "
        f"{sum(1 for _ in pub.rglob('*.png'))} PNGs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
