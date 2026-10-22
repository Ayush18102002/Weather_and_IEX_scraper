"""Month-by-month collection with a local cache, plus loading of exported files."""
import logging
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import api_client
import config
from processor import normalise

log = logging.getLogger(__name__)


def month_windows(start: date, end: date):
    """Yield (window_start, window_end, month_first_day) for every month in [start, end]."""
    cur = date(start.year, start.month, 1)
    while cur <= end:
        nxt = (cur.replace(day=28) + timedelta(days=4)).replace(day=1)
        last = nxt - timedelta(days=1)
        yield max(cur, start), min(last, end), cur, last
        cur = nxt


def _read_cache(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, parse_dates=["date", "timestamp"])


def collect_segment(session, segment: str, start: date, end: date, refresh: bool = False,
                    interval: str = "15min"):
    """Fetch every month for one segment (interval: "15min" or "hourly"). Returns (frame, failures)."""
    config.RAW_DIR.mkdir(exist_ok=True)
    frames, failures = [], []
    today = date.today()
    per_day = 24 if interval == "hourly" else 96
    tag = "_hourly" if interval == "hourly" else ""
    for w_start, w_end, month_first, month_last in month_windows(start, end):
        label = f"{segment}{tag} {month_first:%Y-%m}"
        cache = config.RAW_DIR / f"{segment}{tag}_{month_first:%Y-%m}.csv"
        complete = month_last < today and w_start == month_first and w_end == month_last
        if cache.exists() and complete and not refresh:
            log.info("%s: using cache", label)
            frames.append(_read_cache(cache))
            continue
        try:
            log.info("%s: fetching %s..%s", label, w_start, w_end)
            df = api_client.fetch_range(session, segment, w_start, w_end, interval)
            expected_days = (w_end - w_start).days + 1
            got_days = int(df["date"].nunique())
            if len(df) != got_days * per_day:
                log.warning("%s: %d rows for %d days (expected %d per day)", label, len(df), got_days, per_day)
            if got_days < expected_days:
                log.warning("%s: received %d of %d days (not cached, will be retried next run)",
                            label, got_days, expected_days)
            if complete and got_days == expected_days:
                df.to_csv(cache, index=False)
            frames.append(df)
        except api_client.NotConfigured:
            raise
        except Exception as exc:  # keep going; report at the end
            log.error("%s: FAILED (%s)", label, exc)
            failures.append((label, str(exc)))
        time.sleep(config.REQUEST_DELAY_SECONDS)
    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return data, failures


def load_export_folder(folder: Path, segment: str, start: date, end: date):
    """Read .xlsx/.xls/.csv files exported from the IEX website into one clean frame."""
    files = sorted(p for p in folder.glob("*") if p.suffix.lower() in {".xlsx", ".xls", ".csv"})
    frames, failures = [], []
    for path in files:
        try:
            raw = pd.read_csv(path, header=None) if path.suffix.lower() == ".csv" else pd.read_excel(path, header=None)
            frames.append(normalise(raw, segment))
            log.info("Loaded %s", path.name)
        except Exception as exc:
            log.error("Could not read %s (%s)", path.name, exc)
            failures.append((path.name, str(exc)))
    if not frames:
        return pd.DataFrame(), failures
    data = pd.concat(frames, ignore_index=True)
    data = data.drop_duplicates(subset=["segment", "timestamp"], keep="last")
    data = data[(data["date"].dt.date >= start) & (data["date"].dt.date <= end)]
    return data.sort_values("timestamp").reset_index(drop=True), failures
