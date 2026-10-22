"""Daily weather from the Open-Meteo archive API (free, no key), aggregated monthly."""
import logging
from datetime import date, timedelta

import pandas as pd

import config
import net

log = logging.getLogger(__name__)
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def fetch_daily(session, city: str, lat: float, lon: float, start: date, end: date) -> pd.DataFrame:
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": ",".join(config.WEATHER_DAILY_VARS),
        "timezone": "Asia/Kolkata",
    }
    resp = net.request_with_retry(session, "GET", ARCHIVE_URL, params=params)
    df = pd.DataFrame(resp.json()["daily"])
    df["time"] = pd.to_datetime(df["time"])
    df["city"] = city
    return df


def fetch_weather(session, start: date, end: date):
    """Return (daily frame for all cities, failures)."""
    end = min(end, date.today() - timedelta(days=config.WEATHER_ARCHIVE_LAG_DAYS))
    frames, failures = [], []
    for city, (lat, lon) in config.WEATHER_CITIES.items():
        try:
            log.info("Weather: %s", city)
            frames.append(fetch_daily(session, city, lat, lon, start, end))
        except Exception as exc:
            log.error("Weather %s FAILED (%s)", city, exc)
            failures.append((f"weather {city}", str(exc)))
    daily = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return daily, failures


def monthly_weather(daily: pd.DataFrame) -> pd.DataFrame:
    """Per-city monthly stats, averaged across cities into one row per month."""
    if daily.empty:
        return pd.DataFrame()
    d = daily.copy()
    d["month"] = d["time"].dt.to_period("M").dt.to_timestamp()
    per_city = d.groupby(["city", "month"]).agg(
        temp_mean_c=("temperature_2m_mean", "mean"),
        temp_max_c=("temperature_2m_max", "mean"),
        precip_total_mm=("precipitation_sum", "sum"),
        solar_mj_m2_day=("shortwave_radiation_sum", "mean"),
    ).reset_index()
    overall = per_city.drop(columns="city").groupby("month").mean().add_prefix("avg_").reset_index()
    wide = per_city.pivot(index="month", columns="city", values="temp_mean_c").add_prefix("temp_mean_c_").reset_index()
    return overall.merge(wide, on="month")
