"""Central configuration. Change things here, not in the code."""
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# ---- Output / folders -------------------------------------------------------
START_DATE = date(2022, 4, 1)          # IEX 15-min block data exists from this date
RAW_DIR = BASE_DIR / "raw_data"        # cached monthly CSVs (api source)
EXPORTS_DIR = BASE_DIR / "exports"     # drop manually exported IEX files here (files source)
OUTPUT_XLSX = BASE_DIR / "iex_monthly_data.xlsx"
LOG_FILE = BASE_DIR / "scraper.log"

# ---- Politeness / reliability ----------------------------------------------
USER_AGENT = "Mozilla/5.0 (compatible; iex-monthly-collector/1.0; personal research)"
REQUEST_DELAY_SECONDS = 3.0           # pause between monthly requests
# Browser used when the site needs JavaScript (only if plain requests are not enough).
BROWSER_CHANNEL = None                 # None = Playwright's Chromium; or "chrome" / "msedge" to use an installed browser
BROWSER_EXECUTABLE = None              # or a full path to a Chrome/Chromium executable
REQUEST_TIMEOUT_SECONDS = 60
MAX_RETRIES = 4
BACKOFF_BASE_SECONDS = 2.0             # waits 2s, 4s, 8s ... (+ jitter)

# ---- Market segments --------------------------------------------------------
# Only DAM's page was verified. Add RTM / Green DAM etc. the same way once you
# have found their page URL and (via `python main.py --discover`) their endpoint.
SEGMENTS = {
    "DAM": {
        "enabled": True,
        "page_url": "https://www.iexindia.com/market-data/day-ahead-market/market-snapshot",
        # The page reads its date range from the URL (seen in a real browser session):
        #   ...market-snapshot?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false&toDate=30-04-2022&fromDate=01-04-2022
        "query": "interval={interval}&dp=SELECT_RANGE&showGraph=false"
                 "&toDate={end:%d-%m-%Y}&fromDate={start:%d-%m-%Y}",
    },
    # "RTM": {"enabled": False, "page_url": "https://www.iexindia.com/market-data/real-time-market/market-snapshot"},
}

# ---- Interval codes used in the page URL (`interval=...`) -------------------
# "15min" was seen in a real browser session. "hourly" is a GUESS following the same naming
# pattern: to confirm it, choose Interval = Hourly on the IEX page and copy the value after
# `interval=` from the address bar (or pass --hourly-interval-code on the command line).
INTERVALS = {
    "15min": "ONE_FOURTH_HOUR",
    "hourly": "ONE_HOUR",
}
HOURLY_OUTPUT_CSV = BASE_DIR / "iex_hourly_data.csv"
COMBINED_OUTPUT_CSV = BASE_DIR / "combined_dataset_with_hourly.csv"

# ---- Data endpoint (filled in AFTER running `python main.py --discover`) ----
# The IEX page loads its table with JavaScript. The network call behind it is not
# documented, so it is left blank on purpose. Fill `url` (and the rest) from
# discovery_output/discovery_log.json. Placeholders {start} and {end} are date
# objects, so formats like {start:%d-%m-%Y} work.
API = {
    "DAM": {
        "url": "",
        "method": "GET",                       # or "POST"
        "params": {},                          # e.g. {"fromDate": "{start:%d-%m-%Y}", "toDate": "{end:%d-%m-%Y}"}
        "json_body": None,                     # for POST, same placeholder rules
        "records_path": [],                    # keys leading to the list of rows, e.g. ["data", "rows"]
        "column_map": {},                      # rename JSON keys -> e.g. {"mcpValue": "MCP"}
    },
}

# ---- Weather (Open-Meteo archive API, free, no key) -------------------------
WEATHER_ENABLED = True
WEATHER_CITIES = {
    "Delhi": (28.61, 77.21),
    "Mumbai": (19.08, 72.88),
    "Chennai": (13.08, 80.27),
    "Kolkata": (22.57, 88.36),
    "Ahmedabad": (23.02, 72.57),
}
WEATHER_DAILY_VARS = [
    "temperature_2m_mean",
    "temperature_2m_max",
    "precipitation_sum",
    "shortwave_radiation_sum",
]
WEATHER_ARCHIVE_LAG_DAYS = 6           # the archive trails real time by a few days

# ---- Hourly weather for the IEX merge (Open-Meteo + NASA POWER) -------------
# Both sources are requested in UTC and converted to Indian-time hours (see weather_hourly.py).
# How a value is stamped decides the conversion:
#   instant       value at the stamped moment
#   period_end    value for the hour ENDING at the stamp   (Open-Meteo precipitation, shortwave radiation: documented)
#   period_start  value for the hour STARTING at the stamp
# Open-Meteo: api variable -> (output column, stamp kind)
OPEN_METEO_HOURLY_VARS = {
    "temperature_2m": ("om_temp_c", "instant"),
    "relative_humidity_2m": ("om_humidity_pct", "instant"),
    "wind_speed_10m": ("om_wind_ms", "instant"),          # requested in m/s
    "cloud_cover": ("om_cloud_pct", "instant"),
    "precipitation": ("om_precip_mm", "period_end"),
    "shortwave_radiation": ("om_solar_wm2", "period_end"),
}
# NASA POWER hourly: api parameter -> output column. Irradiance is reported per hour (Wh/m2 per hour is
# numerically the hourly mean W/m2). NASA_STAMP is an UNVERIFIED assumption: the run prints a solar-noon
# check; if NASA's solar peak lands outside the 11:00-13:00 hours, switch this to "period_end".
NASA_HOURLY_VARS = {
    "T2M": "np_temp_c",
    "RH2M": "np_humidity_pct",
    "WS10M": "np_wind_ms",
    "ALLSKY_SFC_SW_DWN": "np_solar_wm2",
}
NASA_STAMP = "period_start"
WEATHER_REQUEST_DELAY_SECONDS = 1.5

# ---- Data definition --------------------------------------------------------
BLOCK_HOURS = 0.25                     # 15-minute blocks; MW * 0.25 h = MWh
BLOCKS_PER_DAY = 96
