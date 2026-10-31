# satellite2: fire map for Sumatra and Kalimantan

A manually triggered GitHub Actions workflow that

1. downloads the latest **VIIRS** active-fire hotspots (NOAA EFIRE, 375 m) over Sumatra and Kalimantan,
2. clusters them and flags the areas most likely to turn into a serious haze problem (**severe** and **watch**),
3. makes a cropped **Himawari-9** image for each cluster (Day Fire RGB by day, 3.9 µm by night) with **ECMWF** wind streamlines,
4. publishes everything to a static, mobile-friendly map on **GitHub Pages**.

Hotspots are only the pointer (where to look). The product is the latest Himawari-9 image over each cluster;
hotspots are a light overlay. Everything runs in GitHub Actions. There is no local PC, no API key and no notification service.

## Run it (from the GitHub mobile app)

1. Open the repo, go to **Actions**, pick **Fire map**, tap **Run workflow**.
2. Set the inputs (or leave the defaults) and start it. Locally a 5-cluster run plus overview took about 3 minutes of processing, plus the dependency install.
3. Open the site (below). The run's job summary lists the clusters and links to it.

| Input | Default | Meaning |
|---|---|---|
| `max_images` | `5` | How many flagged areas (severe first, then watch) get their own image. The rest go into one overview image. Areas that are not flagged get no image. |
| `winds` | on | Overlay ECMWF 925 hPa streamlines. If the wind data cannot be used, images are still published without them (the site says "winds: skipped"). |
| `dry_run` | off | List the clusters and planned crops in the job summary. No imagery, nothing published. |
| `datetime_utc` | blank | Testing: use this Himawari scan (`YYYYMMDDHHMM`, minute ending in 0). Blank = newest complete scan. A missing or incomplete scan fails the run. |
| `hotspot_window_hours` | `3` | Extra (not in the original spec): keep hotspots from this many hours before the newest VIIRS scan. See the note below. |

**About the 3-hour window.** The newest VIIRS scan is global and usually only minutes old, while the three satellites cross
Indonesia only around 05-08 and 17-20 UTC (13-16 and 01-04 SGT). So a run made a few hours after a pass finds **no hotspots**
and publishes a "no hotspots" run. That is expected. Raise `hotspot_window_hours` (for example `9`) to look further back.

## The website

`https://zgkn.github.io/satellite2/`, served from the `site` branch.

**Enable Pages once:** Settings > Pages > Build and deployment > **Deploy from a branch** > branch `site`, folder `/ (root)` > Save.
The `site` branch appears after the first successful run.

* GitHub Pages sites are **public**, even for a private repository. On a private repository Pages also needs a paid plan.
* `LATEST.md` on the `site` branch is a plain fallback of the newest run.

The page is a full-screen Leaflet map (Esri World Imagery, or OpenStreetMap) with, per run:

* each cluster's Himawari image placed by its exact bounding box, plus a toggleable overview image (off by default),
* VIIRS hotspots as circles (size and colour by FRP; popup with satellite, time, FRP, confidence), cluster outlines, optional country borders,
* a layer control and an opacity slider for the selected overlay,
* a header strip (scan time in UTC and SGT, hotspot age, cluster count, mode, winds run),
* cluster cards under the map: tap a card to zoom to it, tap its thumbnail for the full-size PNG,
* a run selector (last 48 hours; your choice is remembered in the browser).

## What is published

On the `site` branch (one orphan commit, force-pushed every run, so history never grows):

```
index.html  app.js  style.css  LATEST.md  runs.json
runs/<run_id>/clusters.json      clusters, PNG bounds, mode, confidence mix, overview entry
runs/<run_id>/hotspots.geojson   every hotspot: satellite, scan time, FRP, confidence, cluster
runs/<run_id>/C01.png, C01_t.jpg, ...  overview.png, overview_t.jpg
```

Runs older than 48 hours are dropped. A concurrency group makes sure only one run publishes at a time.

## How it works

* **Hotspots** (`scripts/hotspots.py`): the anonymous NOAA buckets `noaa-nesdis-{n20,n21,snpp}-pds`, prefix `VIIRS_EFIRE_VIIRSI_EDR/YYYY/MM/DD/`
  (today and yesterday, paginated). The newest scan start across the three satellites is found from the `_s<14 digits>` in the file names;
  granules starting within the window before it are kept. NaN or negative FRP and points outside the region
  (lon 94.5 to 119.5, lat -6.5 to 7.5) are dropped. Files are downloaded in 8 threads but **opened one at a time**
  (netCDF4/HDF5 crashes when opened from several threads).
* **Merging repeat sightings**: the same fire is often seen by two or three satellites, or twice by one. A detection from an older pass within
  0.45 km of a fire already kept is the same fire: it is counted once and the fire keeps its strongest FRP. Detections within one pass are never merged.
  In a real run this removed about a third of the detections.
* **Clusters and severity**: DBSCAN (haversine, 20 km, `min_samples=1`) groups fires. Two warning signs are then checked per cluster
  (all numbers are in `scripts/config.py`):
  * **Dense** (fires packed tight, possibly out of control): some fire has at least **30** distinct fires within **10 km**.
  * **Intense** (a lot of heat, so a lot of smoke): the cluster's summed FRP is at least **500 MW**, or any single pixel is at least **100 MW**.
  * **Severe** = both, **watch** = one, anything else is ignored (no image). Clusters are ranked severe first, then by summed FRP; `C01` is the worst.
  * **Getting worse**: each flagged area is compared with the newest earlier run on the site whose newest hotspot is at least 2 h older
    (centroids within 30 km). It is marked *getting worse* if fires or FRP grew by 1.5x and by at least 10 fires or 100 MW,
    *easing* if they fell likewise, *newly flagged* if there was nothing flagged there before, otherwise *steady*. Without an earlier comparable run there is no trend.
  * Each crop is the cluster bbox plus 50 km, at least 300 km square centred on the cluster, clipped to the region.
* **Himawari-9** (`scripts/himawari.py`): `s3://noaa-himawari9/AHI-L1b-FLDK/`. The newest scan, walking back in 10 min steps for up to 3 h, where
  every needed file exists. Needed segments come from the geostationary projection (the region needs 5-6, a crop usually 1-2).
  Satpy `ahi_hsd` reader, plain reflectance / brightness temperature (no modifiers), nearest-neighbour resampling to a lat/lon grid
  (0.005° for crops, 0.02° for the overview). The 0240 and 1440 UTC scans are always missing from the bucket and are simply skipped.
  * **Day** (sun zenith at the cluster centroid below 90°): Day Fire RGB. R = B07 300.15-333.15 K gamma 0.4, G = B04, B = B03
    (reflectance divided by cos zenith per pixel, zenith capped at 80°), cold cloud (B13 below ~290 K) blended to grey except over hotspots.
    Above 65° zenith the caption adds "LOW SUN".
  * **Night**: B07 290-340 K on a hot colormap, B13 below 280 K as faint grey. The caption says "Night: 3.9 um".
  * A crop with more than ~70 % of pixels colder than 280 K at 10.4 µm is labelled **obscured**.
* **Winds** (`scripts/winds.py`): ECMWF open data, IFS 0.25°, 925 hPa u/v and surface pressure, fetched **once per run** with byte-range
  requests from the GRIB index (about 2 MB per step). The newest run that started at or before the scan and is published; linear interpolation
  between the two 3-hourly steps. Where surface pressure is below 925 hPa (mountains) there are no streamlines. Orange by day, white at night.
* **Images** (`scripts/render.py`): layers in this order: image, graticule, streamlines, coastlines (Natural Earth 1:10m, 1.5 px), hotspot
  circles, caption. Each PNG is exactly the lat/lon grid (no margins), so the site can place it by its bbox.

## Notes and assumptions

* **Confidence.** The EFIRE files contain `FP_Confidence`, but it is the fill value `-99` for every fire pixel, so it is ignored.
  Confidence comes from `FP_FireMask` instead: 7 = low, 8 = nominal, 9 = high. Markers are coloured by it
  (yellow = low, white = nominal, cyan = high); if it were missing they would be coloured by FRP.
* **Persistent heat sources** (flaring, volcanoes: `FP_PersistentAnomalyCategory`) are not filtered out.
* **Thresholds are a starting point.** 30 fires / 10 km and 500 MW / 100 MW were chosen from one day of data (3 severe and 5 watch out of 155 clusters on a busy
  daytime sample, 1 severe on a quiet night-pass sample). Tune them in `scripts/config.py` after a few runs.
* **A run can have nothing to show**: `no hotspots` (nothing in the window), `nothing flagged` (fires, but none severe or watch: no images, the hotspots are still on the map) or `no imagery`.
* **Age of the newest hotspot** in the captions and the header is measured at run time, not at scan time.
* **Crops at the region edge** are clipped to the region (not shifted), so they can be smaller than 300 km on one side.
* **Overview** uses a 5° graticule (1° is too busy at that size) and labels the 12 worst flagged areas (red = severe, amber = watch). Its mode follows the sun at the region centre.
* **Himawari 3.9 µm and 10.4 µm pixels are 2 km**, so the red channel and night images look blocky. This is expected.
* If no complete Himawari scan is found in 3 h, the run still publishes the hotspots and clusters, without imagery.
  If one cluster's image fails, the others are still made. If the hotspot download fails, the run fails loudly and publishes nothing.
* `matplotlib` is in `requirements.txt` although it was not in the original list: the streamlines need it.

## Data sources and attribution

* VIIRS Enterprise active fire (EFIRE), NOAA NESDIS, via the NOAA Open Data Dissemination (NODD) program.
* Himawari-9 AHI L1b, JMA, via NOAA NODD.
* **ECMWF open data (IFS), licensed [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).** Streamlines are model output, not observations,
  so they can disagree with what the clouds show.
* Coastlines: Natural Earth (public domain), `data/ne_10m_coastline.zip`.
* Basemaps: Esri World Imagery and OpenStreetMap contributors. Maps by Leaflet.

This is not an official fire product.

## Run the pipeline locally (optional)

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt            # also needs the bzip2 command (used by Satpy)
python scripts/run.py --outdir output --dry-run --window-hours 9
python scripts/run.py --outdir output --window-hours 9 --datetime-utc 202610300600
```
