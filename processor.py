"""Cleaning of IEX 15-minute block data and monthly / daily aggregation.

Cross-check against IEX's own page summary: for a day, "Total (MWh)" equals
the sum of the 96 block values in MW multiplied by 0.25 h (i.e. average MW x 24).
"""
import logging
import re

import numpy as np
import pandas as pd

import config

log = logging.getLogger(__name__)

BLOCK_PATTERN = r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$"
NUMERIC_COLS = ["purchase_bid_mw", "sell_bid_mw", "mcv_mw", "final_scheduled_mw", "mcp"]


def _map_header(col) -> str:
    h = re.sub(r"\s+", " ", str(col).replace("*", "")).strip().lower()
    c = re.sub(r"[^a-z]", "", h)  # letters only: "Time_Block", "timeBlock", "Time Block" -> "timeblock"
    if "timeblock" in c or c in ("time", "timeslot"):
        return "time_block"
    if c.startswith("date") or c in ("deliverydate", "tradedate"):
        return "date"
    if c in ("hour", "hours"):
        return "hour"
    if "purchase" in c:
        return "purchase_bid_mw"
    if "sellbid" in c or c.startswith("sell"):
        return "sell_bid_mw"
    if "final" in c:
        return "final_scheduled_mw"
    if c.startswith("mcv"):
        return "mcv_mw"
    if c.startswith("mcp"):
        return "mcp"
    return h


def _promote_header(df: pd.DataFrame, need=frozenset({"time_block", "mcp"}), any_of=frozenset()) -> pd.DataFrame:
    """Find the header row if the file has title rows above it.

    `need` columns must all be present; if `any_of` is given, at least one of those must be too.
    """
    def ok(headers) -> bool:
        found = set(headers)
        return need <= found and (not any_of or bool(found & any_of))

    if ok({_map_header(c) for c in df.columns}):
        return df
    for i in range(min(30, len(df))):
        row = df.iloc[i].tolist()
        if ok({_map_header(v) for v in row if pd.notna(v)}):
            out = df.iloc[i + 1 :].copy()
            out.columns = row
            return out.reset_index(drop=True)
    extra = f" and one of {sorted(any_of)}" if any_of else ""
    raise ValueError(f"Could not find a header row containing {sorted(need)}{extra}")


def _parse_dates(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return series
    s = series.astype(str).str.strip()
    out = pd.to_datetime(s, format="%d-%m-%Y", errors="coerce")
    # ISO-style values (e.g. real Excel date cells -> "2022-04-01 00:00:00") are year-first;
    # parsing them day-first would silently swap day and month.
    iso = s.str.match(r"^\d{4}-\d{2}-\d{2}") & out.isna()
    if iso.any():
        out[iso] = pd.to_datetime(s[iso].str[:10], format="%Y-%m-%d", errors="coerce")
    bad = out.isna()
    if bad.any():
        out[bad] = pd.to_datetime(s[bad], errors="coerce", dayfirst=True)
    return out


def normalise(raw: pd.DataFrame, segment: str) -> pd.DataFrame:
    """Turn a raw table (page scrape, export file or API rows) into a clean frame.

    Output columns: segment, date, timestamp, purchase_bid_mw, sell_bid_mw,
    mcv_mw, final_scheduled_mw, mcp (Rs/MWh).
    """
    df = _promote_header(raw.copy())
    df.columns = [_map_header(c) for c in df.columns]
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    for need in ("date", "time_block", "mcp"):
        if need not in df.columns:
            raise ValueError(f"Missing required column '{need}'. Found: {list(df.columns)}")

    df["date"] = df["date"].ffill()  # exports/tables merge the date cell across rows
    blocks = df["time_block"].astype(str).str.extract(BLOCK_PATTERN)
    keep = blocks[0].notna()
    df, blocks = df[keep].copy(), blocks[keep]

    date = _parse_dates(df["date"])
    df["timestamp"] = (
        date
        + pd.to_timedelta(blocks[0].astype(int), unit="h")
        + pd.to_timedelta(blocks[1].astype(int), unit="m")
    )
    df["date"] = date.dt.normalize()
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
        else:
            df[col] = np.nan

    df = df.dropna(subset=["timestamp", "mcp"])
    df["segment"] = segment
    df = df.drop_duplicates(subset=["segment", "timestamp"], keep="last").sort_values("timestamp")
    return df[["segment", "date", "timestamp", *NUMERIC_COLS]].reset_index(drop=True)


def normalise_hourly(raw: pd.DataFrame, segment: str) -> pd.DataFrame:
    """Like normalise(), but for the page's hourly table (one row per hour).

    Accepts an 'Hour' column (1..24 where hour 1 = 00:00-01:00, or 0..23) or a 'Time Block'
    column such as '00:00 - 01:00'. `timestamp` is the START of the hour. Same output columns as normalise().
    """
    df = _promote_header(raw.copy(), need=frozenset({"mcp", "date"}), any_of=frozenset({"hour", "time_block"}))
    df.columns = [_map_header(c) for c in df.columns]
    df = df.loc[:, ~pd.Index(df.columns).duplicated()]
    df["date"] = df["date"].ffill()

    blocks = df["time_block"].astype(str).str.extract(BLOCK_PATTERN) if "time_block" in df.columns else None
    if blocks is not None and blocks[0].notna().any():
        keep = blocks[0].notna()
        df, blocks = df[keep].copy(), blocks[keep]
        hours, minutes = blocks[0].astype(int), blocks[1].astype(int)
    elif "hour" in df.columns:
        num = df["hour"].astype(str).str.extract(r"(\d+)")[0]
        keep = num.notna()
        df, num = df[keep].copy(), num[keep].astype(int)
        if num.empty or num.max() > 24:
            raise ValueError("unexpected values in the Hour column")
        hours = num - (0 if num.min() == 0 else 1)  # the page numbers hours 1..24
        minutes = pd.Series(0, index=df.index)
    else:
        raise ValueError("hourly table has neither an 'Hour' nor a 'Time Block' column")

    date = _parse_dates(df["date"])
    df["timestamp"] = date + pd.to_timedelta(hours, unit="h") + pd.to_timedelta(minutes, unit="m")
    df["date"] = date.dt.normalize()
    for col in NUMERIC_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
        else:
            df[col] = np.nan
    df = df.dropna(subset=["timestamp", "mcp"])
    if (df["timestamp"].dt.minute != 0).any():
        raise ValueError("the table is not hourly (it contains 15-minute blocks); "
                         "check config.INTERVALS['hourly'] against the page's Interval setting")
    df["segment"] = segment
    df = df.drop_duplicates(subset=["segment", "timestamp"], keep="last").sort_values("timestamp")
    return df[["segment", "date", "timestamp", *NUMERIC_COLS]].reset_index(drop=True)


def _aggregate(df: pd.DataFrame, key: str) -> pd.DataFrame:
    d = df.copy()
    d["mcp_x_mcv"] = d["mcp"] * d["mcv_mw"]
    g = d.groupby(["segment", key])
    out = g.agg(
        blocks=("timestamp", "size"),
        mcp_min=("mcp", "min"),
        mcp_max=("mcp", "max"),
        mcp_avg=("mcp", "mean"),
        _mcp_x_mcv=("mcp_x_mcv", "sum"),
        _mcv_mw=("mcv_mw", "sum"),
        purchase_bid_mwh=("purchase_bid_mw", "sum"),
        sell_bid_mwh=("sell_bid_mw", "sum"),
        mcv_mwh=("mcv_mw", "sum"),
        final_scheduled_mwh=("final_scheduled_mw", "sum"),
    ).reset_index()
    out["mcp_avg_volume_weighted"] = out["_mcp_x_mcv"] / out["_mcv_mw"].replace(0, np.nan)
    out = out.drop(columns=["_mcp_x_mcv", "_mcv_mw"])
    for col in ["purchase_bid_mwh", "sell_bid_mwh", "mcv_mwh", "final_scheduled_mwh"]:
        out[col] = out[col] * config.BLOCK_HOURS  # MW per block -> MWh
    out["mcp_avg_rs_per_kwh"] = out["mcp_avg"] / 1000.0
    return out


def monthly_summary(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d["month"] = d["timestamp"].dt.to_period("M").dt.to_timestamp()
    d["day"] = d["date"]
    out = _aggregate(d, "month")
    days = d.groupby(["segment", "month"])["day"].nunique().rename("days").reset_index()
    out = out.merge(days, on=["segment", "month"])
    out["days_in_month"] = out["month"].dt.days_in_month
    out["complete_month"] = out["days"] == out["days_in_month"]
    out["block_coverage_pct"] = 100.0 * out["blocks"] / (out["days_in_month"] * config.BLOCKS_PER_DAY)
    first = ["segment", "month", "days", "days_in_month", "complete_month", "block_coverage_pct", "blocks"]
    return out[first + [c for c in out.columns if c not in first]].sort_values(["segment", "month"]).reset_index(drop=True)


def daily_summary(df: pd.DataFrame) -> pd.DataFrame:
    out = _aggregate(df.rename(columns={"date": "day"}), "day").rename(columns={"day": "date"})
    return out.sort_values(["segment", "date"]).reset_index(drop=True)
