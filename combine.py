"""Merge scraped hourly IEX data and hourly weather into an existing 15-minute IEX dataset (CSV).

The existing rows are never rewritten: they are read as text and written back unchanged, with new
columns appended on the right (and, optionally, new 15-minute rows appended at the bottom, formatted in
the same style as the file, e.g. date 01-04-22 and timestamp 01-04-22 0:00).
"""
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

NUM_COLS = ["purchase_bid_mw", "sell_bid_mw", "mcv_mw", "final_scheduled_mw", "mcp"]
REQUIRED = ["segment", "date", "timestamp", *NUM_COLS]
HOURLY_COLS = ["hour_start", *[f"h_{c}" for c in NUM_COLS]]
TS_FORMATS = ["%d-%m-%y %H:%M", "%d-%m-%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
              "%d/%m/%Y %H:%M", "%d-%m-%y %H:%M:%S"]


# ---------------------------------------------------------------- reading / formatting
def load_existing(path) -> pd.DataFrame:
    """Read the existing dataset as text so that every original value is written back unchanged."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"{path} is missing columns {missing}; found {list(df.columns)}")
    return df


def parse_timestamps(series: pd.Series) -> pd.Series:
    for fmt in TS_FORMATS:
        parsed = pd.to_datetime(series, format=fmt, errors="coerce")
        if parsed.notna().all():
            return parsed
    raise ValueError("Could not parse the 'timestamp' column (expected values like 01-04-22 0:00)")


def fmt_ts(ts: pd.Series) -> pd.Series:
    """Format like the existing file: dd-mm-yy H:MM (hour without a leading zero)."""
    out = ts.dt.strftime("%d-%m-%y ") + ts.dt.hour.astype("Int64").astype(str) + ts.dt.strftime(":%M")
    return out.where(ts.notna(), "")


def fmt_num(values: pd.Series) -> pd.Series:
    return values.map(lambda v: "" if pd.isna(v) else f"{float(v):.10g}")


def write_csv(path, df: pd.DataFrame) -> None:
    df.to_csv(Path(path), index=False, encoding="utf-8-sig")  # BOM so Excel reads it cleanly


# ---------------------------------------------------------------- hourly IEX columns
def attach_hourly(existing: pd.DataFrame, hourly: pd.DataFrame):
    """Left-join the scraped hourly rows onto every 15-minute row (matched on segment + hour start).

    Adds hour_start and h_<column> for each IEX field. Returns (combined, stats).
    """
    ex = existing.drop(columns=[c for c in HOURLY_COLS if c in existing.columns]).copy()
    ts = parse_timestamps(ex["timestamp"])

    h = hourly.drop_duplicates(subset=["segment", "timestamp"], keep="last")
    h = h.rename(columns={c: f"h_{c}" for c in NUM_COLS}).rename(columns={"timestamp": "hour_start"})
    h_cols = [f"h_{c}" for c in NUM_COLS]

    left = ex.assign(hour_start=ts.dt.floor("h"))
    merged = left.merge(h[["segment", "hour_start", *h_cols]], on=["segment", "hour_start"],
                        how="left", validate="many_to_one")
    if len(merged) != len(ex):
        raise RuntimeError("merge changed the number of rows; refusing to continue")

    matched = int(merged["h_mcp"].notna().sum())
    ex_hours = set(ts.dt.floor("h"))
    stats = {
        "rows": len(ex),
        "matched_rows": matched,
        "unmatched_rows": len(ex) - matched,
        "hourly_rows": len(h),
        "hourly_not_in_existing": int((~h["hour_start"].isin(ex_hours)).sum()),
        "existing_range": (ts.min(), ts.max()),
        "hourly_range": (h["hour_start"].min(), h["hour_start"].max()) if len(h) else (None, None),
    }
    merged["hour_start"] = fmt_ts(merged["hour_start"])
    for col in h_cols:
        merged[col] = fmt_num(merged[col])
    return merged, stats


def crosscheck(existing: pd.DataFrame, hourly: pd.DataFrame) -> dict:
    """Compare IEX's hourly figures with the plain average of the four 15-minute blocks of that hour.

    Tells you whether the hourly page is just an average of the blocks or uses another definition.
    """
    ts = parse_timestamps(existing["timestamp"])
    num = existing[NUM_COLS].apply(pd.to_numeric, errors="coerce")
    num["hour_start"] = ts.dt.floor("h")
    sizes = num.groupby("hour_start").size()
    avg = num.groupby("hour_start")[NUM_COLS].mean()
    avg = avg[sizes.reindex(avg.index) == 4]                      # complete hours only
    hourly_idx = hourly.drop_duplicates("timestamp").set_index("timestamp")[NUM_COLS]
    joined = avg.join(hourly_idx, how="inner", lsuffix="_15", rsuffix="_h")
    result = {}
    for col in NUM_COLS:
        a, b = joined[f"{col}_15"], joined[f"{col}_h"]
        diff = (a - b).abs()
        close = diff <= (0.001 * b.abs()).clip(lower=0.5)
        result[col] = {
            "hours_compared": int(len(joined)),
            "share_close": float(close.mean()) if len(joined) else float("nan"),
            "max_abs_diff": float(diff.max()) if len(joined) else float("nan"),
        }
    return result


# ---------------------------------------------------------------- weather columns
def attach_weather(existing: pd.DataFrame, weather: pd.DataFrame):
    """Left-join hourly weather (index = Indian-time hour start) onto every 15-minute row."""
    cols = list(weather.columns)
    ex = existing.drop(columns=[c for c in cols if c in existing.columns]).copy()
    key = parse_timestamps(ex["timestamp"]).dt.floor("h")
    w = weather.copy()
    w.index.name = "_key"
    merged = ex.assign(_key=key).merge(w.reset_index(), on="_key", how="left", validate="many_to_one")
    merged = merged.drop(columns="_key")
    if len(merged) != len(ex):
        raise RuntimeError("weather merge changed the number of rows; refusing to continue")
    for col in cols:
        merged[col] = fmt_num(merged[col])
    stats = {col: int((merged[col] != "").sum()) for col in cols}
    return merged, stats


# ---------------------------------------------------------------- extending the 15-minute data
def append_quarter_rows(existing: pd.DataFrame, new: pd.DataFrame):
    """Append 15-minute rows later than the last existing row, formatted like the existing file."""
    last = parse_timestamps(existing["timestamp"]).max()
    add = new[new["timestamp"] > last]
    if add.empty:
        return existing, 0
    rows = pd.DataFrame({
        "segment": add["segment"].values,
        "date": add["timestamp"].dt.strftime("%d-%m-%y").values,
        "timestamp": fmt_ts(add["timestamp"]).values,
        **{c: fmt_num(add[c]).values for c in NUM_COLS},
    })
    for col in existing.columns:
        if col not in rows.columns:
            rows[col] = ""
    out = pd.concat([existing, rows[list(existing.columns)]], ignore_index=True)
    return out, len(rows)
