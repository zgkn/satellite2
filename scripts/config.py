"""Settings shared by the fire-map pipeline."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COASTLINE_ZIP = ROOT / "data" / "ne_10m_coastline.zip"

# Region of interest: west, south, east, north (Sumatra + Kalimantan).
REGION = (94.5, -6.5, 119.5, 7.5)

SGT = dt.timezone(dt.timedelta(hours=8))

# ---- VIIRS active fire (NOAA open data, anonymous) ----
EFIRE_PREFIX = "VIIRS_EFIRE_VIIRSI_EDR"
EFIRE_BUCKETS = {
    "NOAA-20": "noaa-nesdis-n20-pds",
    "NOAA-21": "noaa-nesdis-n21-pds",
    "S-NPP": "noaa-nesdis-snpp-pds",
}
HOTSPOT_WINDOW_H = 3  # keep granules starting this long before the newest one
DOWNLOAD_WORKERS = 8

# ---- clustering and crops ----
CLUSTER_EPS_KM = 20.0
EARTH_RADIUS_KM = 6371.0
CROP_MARGIN_KM = 50.0
CROP_MIN_KM = 300.0
KM_PER_DEG = 111.0
CROP_RES = 0.005       # deg per pixel (~0.5 km)
OVERVIEW_RES = 0.02

# ---- Himawari-9 ----
HIMAWARI_BUCKET = "noaa-himawari9"
HIMAWARI_PRODUCT = "AHI-L1b-FLDK"
SCAN_LOOKBACK_H = 3.0
N_SEGMENTS = 10
FD_HALF_HEIGHT_M = 5.5e6
GEOS_CRS = dict(proj="geos", h=35785863, lon_0=140.7, sweep="y", a=6378137, b=6356752.3)
DAY_BANDS = ("B03", "B04", "B07", "B13")
NIGHT_BANDS = ("B07", "B13")
LOW_SUN_ZENITH = 65.0
OBSCURED_BT_K = 280.0
OBSCURED_FRACTION = 0.70

# ---- drawing ----
GRID_STEP_CROP = 1.0
GRID_STEP_OVERVIEW = 5.0
WIND_SPACING_CROP = 30.0
WIND_SPACING_OVERVIEW = 45.0
WIND_LEVEL = 925
THUMB_WIDTH = 300

# ---- publishing ----
KEEP_HOURS = 48
