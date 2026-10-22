"""Hourly weather for the IEX merge: Open-Meteo archive + NASA POWER hourly, aligned to Indian-time hours.

Both sources are requested in UTC (documented and unambiguous) and converted here. An IEX hour K covers
K:00-K+1:00 IST, which is (K-5:30, K-4:30] UTC. Depending on how a source stamps a value (config):
  instant       -> the sample at UTC hour K-5 (the middle of the IEX hour)
  period_end    -> mean of the UTC stamps K-5 and K-4 (each stamp is the END of the hour it describes)
  period_start  -> mean of the UTC stamps K-6 and K-5 (each stamp is the START of the hour it describes)
Values are averaged across the configured cities (equal weights).
"""
import logging
import time
from datetime import date, timedelta

import pandas as pd

import config
import net

log = logging.getLogger(__name__)

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
NASA_URL = "https://power.larc.nasa.gov/api/temporal/hourly/point"
SOURCES = ("open-meteo", "nasa")
NASA_FILL_BELOW = -990          # NASA marks missing values with -999
UNITS: dict[str, dict] = {"open-meteo": {}, "nasa": {}}


# ---------------------------------------------------------------- fetching (UTC)
def fetch_open_meteo(session, lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
    params = {
        "latitude": lat, "longitude": lon,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": ",".join(config.OPEN_METEO_HOURLY_VARS),
        "timezone": "GMT", "wind_speed_unit": "ms",
    }
    data = net.request_with_retry(session, "GET", OPEN_METEO_URL, params=params).json()
    if "hourly" not in data:
        raise RuntimeError(f"Open-Meteo returned no hourly data: {str(data)[:200]}")
    UNITS["open-meteo"].update(data.get("hourly_units", {}))
    df = pd.DataFrame(data["hourly"])
    df["time"] = pd.to_datetime(df["time"])
    return df.set_index("time")


def fetch_nasa(session, lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
    params = {
        "parameters": ",".join(config.NASA_HOURLY_VARS), "community": "RE",
        "longitude": lon, "latitude": lat,
        "start": f"{start:%Y%m%d}", "end": f"{end:%Y%m%d}",
        "format": "JSON", "time-standard": "UTC",
    }
    data = net.request_with_retry(session, "GET", NASA_URL, params=params).json()
    try:
        block = data["properties"]["parameter"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(f"NASA POWER returned no data: {str(data)[:300]}") from exc
    for name, meta in (data.get("parameters") or {}).items():
        if isinstance(meta, dict) and "units" in meta:
            UNITS["nasa"][name] = meta["units"]
    df = pd.DataFrame(block)
    df.index = pd.to_datetime(df.index, format="%Y%m%d%H")
    df.index.name = "time"
    return df.mask(df <= NASA_FILL_BELOW)


def _fetch(source: str, session, lat, lon, start, end) -> pd.DataFrame:
    """Fetch one chunk and rename the API variables to output column names (still UTC)."""
    if source == "open-meteo":
        df = fetch_open_meteo(session, lat, lon, start, end)
        return df.rename(columns={api: col for api, (col, _kind) in config.OPEN_METEO_HOURLY_VARS.items()})
    df = fetch_nasa(session, lat, lon, start, end)
    return df.rename(columns=config.NASA_HOURLY_VARS)


def _kinds(source: str) -> dict[str, str]:
    if source == "open-meteo":
        return {col: kind for col, kind in config.OPEN_METEO_HOURLY_VARS.values()}
    return {col: config.NASA_STAMP for col in config.NASA_HOURLY_VARS.values()}


def _year_chunks(start: date, end: date):
    cur = start
    while cur <= end:
        stop = min(date(cur.year, 12, 31), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def collect_city_raw(session, source: str, city: str, lat: float, lon: float,
                     start: date, end: date, refresh: bool = False) -> pd.DataFrame:
    """UTC-hourly raw values for one city, fetched per calendar year and cached when the year is finished."""
    cache_dir = config.RAW_DIR / "weather"
    cache_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for c_start, c_end in _year_chunks(start - timedelta(days=1), end):   # one extra UTC day before the first IST hour
        cache = cache_dir / f"{source}_{city}_{c_start:%Y%m%d}_{c_end:%Y%m%d}.csv"
        finished = c_end < date.today() - timedelta(days=config.WEATHER_ARCHIVE_LAG_DAYS + 5)
        if finished and cache.exists() and not refresh:
            frames.append(pd.read_csv(cache, index_col=0, parse_dates=True))
            continue
        df = _fetch(source, session, lat, lon, c_start, c_end)
        if finished:
            df.to_csv(cache)
        frames.append(df)
        time.sleep(config.WEATHER_REQUEST_DELAY_SECONDS)
    raw = pd.concat(frames)
    return raw[~raw.index.duplicated(keep="last")].sort_index()


# ---------------------------------------------------------------- alignment (UTC -> IST hour start)
def align_to_ist(raw: pd.DataFrame, kinds: dict[str, str], start: date, end: date) -> pd.DataFrame:
    """Convert UTC-hourly values to values per Indian-time hour (index = hour start, naive IST)."""
    full = raw.reindex(pd.date_range(raw.index.min(), raw.index.max(), freq="h"))
    out = {}
    for col, kind in kinds.items():
        if col not in full.columns:
            continue
        s = full[col].astype(float)
        if kind == "instant":
            aligned = s
        elif kind == "period_end":
            aligned = (s + s.shift(-1)) / 2
        elif kind == "period_start":
            aligned = (s.shift(1) + s) / 2
        else:
            raise ValueError(f"unknown stamp kind {kind!r} for {col}")
        aligned.index = aligned.index + pd.Timedelta(hours=5)   # whole-hour part of the +5:30 offset
        out[col] = aligned
    wanted = pd.date_range(pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23), freq="h")
    return pd.DataFrame(out).reindex(wanted)


def solar_peak_hour(df: pd.DataFrame, col: str) -> int:
    """IEX-hour (IST) in which the average irradiance is highest; should be 11 or 12 for India."""
    return int(df[col].groupby(df.index.hour).mean().idxmax())


# ---------------------------------------------------------------- everything together
def build(session, sources, start: date, end: date, refresh: bool = False):
    """Hourly weather averaged across cities. Returns (wide, by_city, failures, report_lines).

    wide:    index = IST hour start, columns om_* / np_* (mean over the cities that worked)
    by_city: long table (source, city, timestamp, values) for transparency
    """
    end_eff = min(end, date.today() - timedelta(days=config.WEATHER_ARCHIVE_LAG_DAYS))
    wide_parts, long_parts, failures, report = [], [], [], []
    for source in sources:
        per_city = {}
        for city, (lat, lon) in config.WEATHER_CITIES.items():
            try:
                log.info("Hourly weather: %s / %s", source, city)
                raw = collect_city_raw(session, source, city, lat, lon, start, end_eff, refresh)
                per_city[city] = align_to_ist(raw, _kinds(source), start, end_eff)
            except Exception as exc:
                log.error("Hourly weather %s / %s FAILED (%s)", source, city, exc)
                failures.append((f"weather {source} {city}", str(exc)))
        if not per_city:
            report.append(f"{source}: no city could be downloaded - columns skipped")
            continue
        wide_parts.append(pd.concat(per_city.values()).groupby(level=0).mean())
        long = pd.concat(per_city, names=["city", "timestamp"]).reset_index()
        long.insert(0, "source", source)
        long_parts.append(long)

        report.append(f"{source}: {len(per_city)} of {len(config.WEATHER_CITIES)} cities, "
                      f"{wide_parts[-1].index.min():%d-%m-%Y} to {wide_parts[-1].index.max():%d-%m-%Y}")
        solar_cols = [c for c in wide_parts[-1].columns if c.endswith("solar_wm2")]
        if solar_cols:
            peak = solar_peak_hour(wide_parts[-1], solar_cols[0])
            note = "OK" if peak in (11, 12) else "UNEXPECTED - the hours may be shifted; see config.NASA_STAMP"
            report.append(f"{source}: average solar peaks in the IEX hour starting {peak}:00 IST ({note})")
        if UNITS.get(source):
            report.append(f"{source} units: " + ", ".join(f"{k}={v}" for k, v in UNITS[source].items()))
    wide = pd.concat(wide_parts, axis=1) if wide_parts else pd.DataFrame()
    by_city = pd.concat(long_parts, ignore_index=True) if long_parts else pd.DataFrame()
    return wide, by_city, failures, report
