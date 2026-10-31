#!/usr/bin/env python3
"""Fire map pipeline: VIIRS hotspots -> clusters -> Himawari-9 crops with ECMWF winds -> run data for the site.

    python scripts/run.py --outdir output [--max-images 5] [--no-winds] [--dry-run] [--datetime-utc YYYYMMDDHHMM]

Writes ``<outdir>/runs/<run_id>/`` (clusters.json, hotspots.geojson, PNGs, thumbnails) and
``<outdir>/run_meta.json`` (the summary row for runs.json; its ``status`` says what happened).
Exit status: 0 on success (including "no hotspots" and "no imagery"), 2 for expected failures
(bad input, hotspot download failed), 1 for anything unexpected.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
import warnings
from pathlib import Path

import numpy as np

warnings.filterwarnings("ignore", message="invalid value encountered in log")  # satpy BT of fill pixels
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402
import himawari as H  # noqa: E402
import hotspots as HS  # noqa: E402
import render as R  # noqa: E402
import trends as T  # noqa: E402
import winds as W  # noqa: E402
from util import UserError, annotate, log, s3_client  # noqa: E402


def iso(t: dt.datetime | None) -> str | None:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def human_age(minutes: float) -> str:
    m = int(round(abs(minutes)))
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def parse_scan_time(text: str) -> dt.datetime:
    try:
        t = dt.datetime.strptime(text.strip(), "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        raise UserError(f"datetime_utc must be YYYYMMDDHHMM (UTC), got {text!r}")
    if t.minute % 10:
        raise UserError(f"Himawari full-disk scans are every 10 min (minute ends in 0), got {text!r}")
    return t


# ----------------------------------------------------------------------- data files
def subset(hs: HS.Hotspots, bbox):
    w, s, e, n = bbox
    m = (hs.lon >= w) & (hs.lon <= e) & (hs.lat >= s) & (hs.lat <= n)
    return dict(lat=hs.lat[m], lon=hs.lon[m], frp=hs.frp[m], conf=hs.conf[m])


def write_geojson(hs: HS.Hotspots, path: Path) -> None:
    feats = []
    for i in range(len(hs)):
        props = dict(satellite=str(hs.sat[i]), time=iso(hs.time(i)), frp=round(float(hs.frp[i]), 2),
                     cluster=f"C{int(hs.cluster[i]):02d}" if len(hs.cluster) else None,
                     seen=int(hs.seen[i]) if len(hs.seen) else 1)
        if hs.has_conf and hs.conf[i]:
            props["confidence"] = str(hs.conf[i])
        feats.append(dict(type="Feature", properties=props,
                          geometry=dict(type="Point", coordinates=[round(float(hs.lon[i]), 5), round(float(hs.lat[i]), 5)])))
    path.write_text(json.dumps(dict(type="FeatureCollection", features=feats), separators=(",", ":")))


# --------------------------------------------------------------------------- captions
TIER_RGBA = {"severe": (255, 100, 90, 255), "watch": (255, 190, 60, 255)}
TREND_TEXT = {"escalating": "GETTING WORSE", "new": "NEWLY FLAGGED", "easing": "easing", "steady": "steady"}


def trend_note(c):
    """(text, rgba) for the caption/summary line about how an area changed since the earlier run, or None."""
    t = c.get("trend")
    if not t:
        return None
    when = ""
    if t.get("prev_hotspot_utc"):
        when = f" since {dt.datetime.strptime(t['prev_hotspot_utc'], '%Y-%m-%dT%H:%M:%SZ'):%d %b %H:%M} UTC"
    if t["state"] in ("escalating", "easing", "steady"):
        text = f"{TREND_TEXT[t['state']]}: {c['count']} fires (was {t['prev_count']}), {c['summed_frp']:.0f} MW (was {t['prev_frp']:.0f}){when}"
    else:
        text = f"{TREND_TEXT[t['state']]}{when}"
    return text, TIER_RGBA["severe"] if t["state"] in ("escalating", "new") else R.GREY


def caption_builder(*, title, scan, newest_hotspot, now, mode, info, zenith, wind, extra_lines=()):
    sgt = scan.astimezone(C.SGT)
    age = (now - newest_hotspot).total_seconds() / 60
    key_a = [("red = fire", (255, 90, 80, 255)), ("   blue = smoke", (110, 160, 255, 255)),
             ("   cyan = cloud", (90, 245, 245, 255))]
    key_b = [("green = vegetation", (90, 210, 100, 255)), ("   brown = ground", (190, 130, 90, 255)),
             ("   black = water", (150, 150, 160, 255))]

    def build(wind_drawn: bool):
        lines = [
            [(title, R.WHITE)],
            [(f"Himawari-9  {scan:%d %b %H:%M} UTC / {sgt:%H:%M} SGT", R.WHITE)],
            [(f"Newest hotspot {newest_hotspot:%H:%M} UTC, {human_age(age)} old at run", R.GREY)],
            [("Day Fire RGB" if mode == "day" else "Night: 3.9 um", R.GREY)]
            + ([("   OBSCURED: mostly cloud", (255, 190, 60, 255))] if info.get("obscured") else []),
        ]
        for note in extra_lines:  # text, or (text, rgba)
            text, fill = note if isinstance(note, tuple) else (note, R.GREY)
            lines.append([(text, fill)])
        if mode == "day" and zenith > C.LOW_SUN_ZENITH:
            lines.append([(f"LOW SUN: unreliable brightness (zenith {zenith:.0f}\N{DEGREE SIGN})", (255, 190, 60, 255))])
        if wind is not None and wind_drawn:
            rgb = (*(W.NIGHT_LINE_RGB if mode == "night" else W.LINE_RGB), 255)
            lines.append([(f"Streamlines: {wind.describe()}", rgb)])
        if mode == "day":
            lines.extend([key_a, key_b])
        return lines

    return build


# ------------------------------------------------------------------------- rendering
def render_job(job, files, scan, hs, wind, outdir: Path, now, has_conf):
    """Render one crop/overview; returns a dict for clusters.json."""
    bbox, res = job["bbox"], job["res"]
    lon, lat = job["centroid"]
    mode, zen = H.mode_at(scan, lon, lat)
    rgb, info = H.make_image(files, job["segments"], bbox, res, mode, scan)
    sub = subset(hs, bbox)
    is_overview = job["name"] == "overview"
    if is_overview:
        title = f"Overview: {job['n_clusters']} flagged areas ({job['n_severe']} severe, {job['n_watch']} watch)"
        extra = [f"{job['n_overflow']} flagged area(s) not individually imaged (top {job['n_imaged']} have crops)",
                 ("Labels: red = severe, amber = watch", R.GREY)]
        avoid = None
        labels = job["labels"]
        size, ring_ss, spacing, grid = 15, 2, C.WIND_SPACING_OVERVIEW, C.GRID_STEP_OVERVIEW
        newest = job["newest"]
    else:
        c = job["cluster"]
        title = f"{c['id']}  {c['tier'].upper()}  ·  {c['count']} fires  ·  {c['summed_frp']:.0f} MW"
        extra, labels = [(", ".join(c["reasons"]), TIER_RGBA[c["tier"]])], None
        if trend_note(c):
            extra.append(trend_note(c))
        avoid = (hs.lon[c["idx"]], hs.lat[c["idx"]])
        size, ring_ss, spacing, grid = 12, 3, C.WIND_SPACING_CROP, C.GRID_STEP_CROP
        newest = dt.datetime.fromtimestamp(c["newest_t"], dt.timezone.utc)
    caption = caption_builder(title=title, scan=scan, newest_hotspot=newest, now=now, mode=mode, info=info,
                              zenith=zen, wind=wind, extra_lines=extra)
    img, wstatus = R.compose(rgb, bbox, res, mode=mode, hs=sub, has_conf=has_conf, wind=wind, wind_spacing=spacing,
                             grid_step=grid, caption_fn=caption, font_size=size, ring_ss=ring_ss, labels=labels,
                             avoid_lonlat=avoid)
    name = job["name"]
    R.save_with_thumb(img, outdir / f"{name}.png", outdir / f"{name}_t.jpg")
    log(f"  {name}: {mode} mode (zenith {zen:.0f}), {img.width}x{img.height}px, cloud {info['cloud_fraction']:.0%}"
        f"{' OBSCURED' if info['obscured'] else ''}, winds {wstatus}")
    return dict(mode=mode, sun_zenith=round(zen, 1), png=f"{name}.png", thumb=f"{name}_t.jpg",
                bbox=list(bbox), size_px=[img.width, img.height], obscured=bool(info["obscured"]),
                cloud_fraction=info["cloud_fraction"], winds=wstatus)


# ---------------------------------------------------------------------------- run
def build_jobs(top, overflow, flagged, hs):
    jobs = []
    for c in top:
        jobs.append(dict(name=c["id"], cluster=c, bbox=c["crop"], res=C.CROP_RES, centroid=c["centroid"],
                         segments=H.needed_segments(c["crop"])))
    if overflow:
        w, s, e, n = C.REGION
        newest = dt.datetime.fromtimestamp(max(c["newest_t"] for c in flagged), dt.timezone.utc)
        labels = [(c["id"], *c["centroid"], TIER_RGBA[c["tier"]]) for c in flagged[:12]]
        jobs.append(dict(name="overview", bbox=C.REGION, res=C.OVERVIEW_RES, centroid=((w + e) / 2, (s + n) / 2),
                         segments=H.needed_segments(C.REGION), n_clusters=len(flagged),
                         n_severe=sum(c["tier"] == "severe" for c in flagged),
                         n_watch=sum(c["tier"] == "watch" for c in flagged),
                         n_overflow=len(overflow), n_imaged=len(top), labels=labels, newest=newest))
    return jobs


def job_summary(meta, cj, dry_run: bool) -> str:
    out = [f"## Fire map run {meta['run_id']}", ""]
    out.append(f"**Status:** {meta['status']}" + (" (dry run: no imagery, nothing published)" if dry_run else "") + "  ")
    if meta.get("scan_utc"):
        out.append(f"**Himawari scan:** {meta['scan_utc']} / {meta['scan_sgt']}  ")
    if meta.get("newest_hotspot_utc"):
        out.append(f"**Newest hotspot:** {meta['newest_hotspot_utc']} ({meta['hotspot_age_min']} min old)  ")
    out.append(f"**Fires:** {meta['n_hotspots']} distinct ({meta['n_detections_raw']} detections before merging repeat sightings), "
               f"{meta['total_frp']:,.0f} MW in total, in {meta['n_clusters']} clusters  ")
    out.append(f"**Flagged:** {meta['n_severe']} severe, {meta['n_watch']} watch, {meta['n_ignored']} ignored; winds: {meta['winds']}")
    out.append("")
    rules = cj["rules"]
    out.append(f"_Rules: dense = {rules['dense_min']}+ fires within {rules['dense_km']:g} km; intense = {rules['intense_sum_mw']:g}+ MW "
               f"in the cluster or a {rules['intense_pixel_mw']:g}+ MW pixel; severe = both, watch = one._")
    out.append("")
    rows = cj["clusters"] + cj["overflow"]
    if rows:
        out += ["| ID | Tier | Why | Fires | MW | Trend | Centroid | Image |", "|---|---|---|---|---|---|---|---|"]
        for c in rows:
            cl = c["centroid"]
            tn = trend_note(c)
            img = ("obscured" if c.get("obscured") else "ok" if c.get("png") else
                   (c.get("image_error") or ("overview only" if c in cj["overflow"] else "none")))
            out.append(f"| {c['id']} | {c['tier']} | {', '.join(c['reasons'])} | {c['count']} | {c['summed_frp']:.0f} | "
                       f"{tn[0].split(':')[0] if tn else '-'} | {cl['lat']:.2f}, {cl['lon']:.2f} | {img} |")
        if cj.get("overview"):
            out.append(f"\nOverview image: {cj['overview']['mode']} mode, {cj['n_overflow']} flagged areas not individually imaged.")
    elif meta["n_hotspots"]:
        out.append("Fires were detected, but none is dense or intense enough to flag, so no images were made.")
    return "\n".join(out) + "\n"


def run(args) -> dict:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    run_id = f"{now:%Y%m%d_%H%M}Z"
    outdir = Path(args.outdir)
    rundir = outdir / "runs" / run_id
    shutil.rmtree(rundir, ignore_errors=True)
    rundir.mkdir(parents=True)
    requested = parse_scan_time(args.datetime_utc) if args.datetime_utc.strip() else None
    max_images = max(1, min(int(args.max_images), 30))

    log(f"=== run {run_id}: max_images={max_images} winds={args.winds} dry_run={args.dry_run} "
        f"scan_override={args.datetime_utc or '-'} window={args.window_hours:g} h")
    hs, hinfo = HS.fetch_hotspots(now, window_h=args.window_hours)  # raises (loudly) if the download fails
    clusters = HS.cluster_hotspots(hs)
    top, overflow, ignored = HS.plan_crops(clusters, max_images)
    flagged = top + overflow   # severe first, then watch, each by summed FRP

    newest_hs = max((c["newest_t"] for c in clusters), default=None)
    newest_hs_dt = dt.datetime.fromtimestamp(newest_hs, dt.timezone.utc) if newest_hs else None
    if flagged:
        T.annotate_trends(flagged, T.load_previous(Path(args.previous) if args.previous else None, newest_hs_dt))
    meta = dict(run_id=run_id, generated_utc=iso(now), status="ok", scan_utc=None, scan_sgt=None,
                newest_hotspot_utc=iso(newest_hs_dt),
                hotspot_age_min=round((now - newest_hs_dt).total_seconds() / 60) if newest_hs_dt else None,
                viirs_newest_scan_utc=iso(hinfo["newest_scan_utc"]), mode=None, winds="off",
                schema=T.SCHEMA, n_hotspots=len(hs), n_detections_raw=hinfo["n_raw"], total_frp=round(float(hs.frp.sum()), 1),
                n_clusters=len(clusters), n_severe=sum(c["tier"] == "severe" for c in flagged),
                n_watch=sum(c["tier"] == "watch" for c in flagged), n_ignored=len(ignored),
                n_escalating=sum(1 for c in flagged if c["trend"] and c["trend"]["state"] == "escalating"),
                n_imaged=0, n_overflow=len(overflow), has_confidence=hs.has_conf, github_run_id=os.environ.get("GITHUB_RUN_ID"),
                window_hours=args.window_hours)

    def common(c):
        cx, cy = c["centroid"]
        return dict(id=c["id"], rank=c["rank"], tier=c["tier"], reasons=c["reasons"], dense=c["dense"], intense=c["intense"],
                    centroid=dict(lon=round(cx, 5), lat=round(cy, 5)), count=c["count"], core_fires=c["core_fires"],
                    summed_frp=round(c["summed_frp"], 1), max_frp=round(c["max_frp"], 1),
                    cluster_bbox=[round(v, 5) for v in c["cluster_bbox"]], trend=c["trend"],
                    newest_utc=iso(dt.datetime.fromtimestamp(c["newest_t"], dt.timezone.utc)),
                    satellites=c["satellites"], confidence=c["confidence"])

    def cluster_entry(c):
        return dict(common(c), bbox=list(c["crop"]), mode=None, png=None, thumb=None, obscured=False)

    cj = dict(schema=T.SCHEMA, run_id=run_id, scan_utc=None, scan_sgt=None, winds="off", has_confidence=hs.has_conf,
              rules=dict(dense_min=C.DENSE_MIN_FIRES, dense_km=C.DENSE_RADIUS_KM, intense_sum_mw=C.INTENSE_SUM_MW,
                         intense_pixel_mw=C.INTENSE_PIXEL_MW),
              clusters=[cluster_entry(c) for c in top], overview=None, n_overflow=len(overflow),
              overflow=[common(c) for c in overflow], n_ignored=len(ignored))

    def finish(status: str):
        meta["status"] = status
        write_outputs(rundir, outdir, hs, cj, meta, args.dry_run)
        return meta

    if not len(hs):
        annotate("notice", "No VIIRS hotspots in the region in the selected window: publishing a 'no hotspots' run.")
        return finish("no_hotspots")

    if not flagged:
        annotate("notice", "Fires were detected, but none is dense or intense enough to flag: no images are made.")
        return finish("no_flagged")

    jobs = build_jobs(top, overflow, flagged, hs)
    for j in jobs:
        log(f"  planned {j['name']}: bbox {j['bbox']} segments {j['segments']} res {j['res']}")
    if args.dry_run:
        return finish("dry_run")

    s3 = s3_client()
    scan, keys = H.select_scan(s3, jobs, requested)
    if scan is None:
        return finish("no_imagery")
    meta["scan_utc"], meta["scan_sgt"] = scan.strftime("%Y-%m-%d %H:%M UTC"), scan.astimezone(C.SGT).strftime("%Y-%m-%d %H:%M SGT")
    cj["scan_utc"], cj["scan_sgt"] = meta["scan_utc"], meta["scan_sgt"]
    cj["scan_iso"] = iso(scan)
    meta["scan_iso"] = iso(scan)

    work = Path(tempfile.mkdtemp(prefix="fire_", dir=args.workdir))
    old_tmp = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = str(work)  # Satpy unpacks .bz2 files into the temp dir; keep them inside workdir
    tempfile.tempdir = None
    try:
        needed = H.required_files(scan, jobs)
        files = H.download(s3, keys, needed, work / "hsd")

        wind = None
        if args.winds:
            try:
                wind = W.fetch_winds(s3, scan, C.WIND_LEVEL, C.REGION)
                meta["winds"] = wind.describe()
                log(f"Winds: {wind.describe()}")
            except Exception as exc:  # noqa: BLE001 - winds must never cost the images
                meta["winds"] = "skipped"
                meta["winds_error"] = f"{type(exc).__name__}: {exc}"
                annotate("warning", f"Wind streamlines skipped ({meta['winds_error']}); images are published without them.")
        cj["winds"] = meta["winds"]

        modes, drawn, tried = [], 0, 0
        for job in jobs:
            t0 = time.time()
            try:
                res = render_job(job, files, scan, hs, wind, rundir, now, hs.has_conf)
            except Exception as exc:  # noqa: BLE001 - one failing cluster must not lose the others
                annotate("warning", f"{job['name']}: imagery failed ({type(exc).__name__}: {exc})")
                log(traceback.format_exc())
                res = dict(image_error=f"{type(exc).__name__}: {exc}")
            if job["name"] == "overview":
                if "png" in res:
                    cj["overview"] = res
            else:
                entry = next(c for c in cj["clusters"] if c["id"] == job["name"])
                entry.update(res)
            if "png" in res:
                modes.append(res["mode"])
                meta["n_imaged"] += 0 if job["name"] == "overview" else 1
                if wind is not None:
                    tried += 1
                    drawn += res["winds"] in ("drawn", "masked")
            log(f"  {job['name']} done in {time.time() - t0:.0f} s")
        if wind is not None and tried and drawn == 0:
            meta["winds"], cj["winds"] = "skipped", "skipped"
            meta["winds_error"] = "streamline drawing failed on every image"
        meta["mode"] = "mixed" if len(set(modes)) > 1 else (modes[0] if modes else None)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if old_tmp is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = old_tmp
        tempfile.tempdir = None
    log(f"Deleted downloaded HSD files ({work})")
    if not modes:
        return finish("no_imagery")
    return finish("ok")


def write_outputs(rundir: Path, outdir: Path, hs, cj, meta, dry_run: bool) -> None:
    write_geojson(hs, rundir / "hotspots.geojson")
    (rundir / "clusters.json").write_text(json.dumps(cj, indent=1))
    (outdir / "run_meta.json").write_text(json.dumps(meta, indent=1))
    summary = job_summary(meta, cj, dry_run)
    log("\n" + summary)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as fh:
            fh.write(summary)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--outdir", default="output")
    p.add_argument("--workdir", default=None)
    p.add_argument("--max-images", default="5")
    p.add_argument("--winds", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--datetime-utc", default="")
    p.add_argument("--previous", default=None,
                   help="folder with the previous site contents (runs.json, runs/), to tell whether areas are getting worse")
    p.add_argument("--window-hours", type=float, default=C.HOTSPOT_WINDOW_H,
                   help="hotspot window before the newest VIIRS scan start (default 3)")
    args = p.parse_args(argv)
    try:
        args.max_images = int(str(args.max_images).strip() or 5)
        run(args)
        return 0
    except UserError as exc:
        annotate("error", str(exc))
        return 2
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        annotate("error", "Unexpected failure; see the log.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
