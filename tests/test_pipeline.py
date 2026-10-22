"""Offline tests using synthetic data shaped like the IEX Market Snapshot export."""
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import processor  # noqa: E402
import scraper  # noqa: E402


def make_export(days: list[date], seed: int = 0) -> pd.DataFrame:
    """Rows like the site table: date/hour only on the first row of each group, title rows above."""
    rng = np.random.default_rng(seed)
    rows = [["Market Snapshot", None, None, None, None, None, None, None],
            ["Date", "Hour", "Time Block", "Purchase Bid (MW)", "Sell Bid (MW)", "MCV (MW)",
             "Final Scheduled Volume (MW)", "MCP (Rs/MWh) *"]]
    for d in days:
        for b in range(96):
            h, m = divmod(b * 15, 60)
            end_h, end_m = divmod((b + 1) * 15, 60)
            rows.append([
                d.strftime("%d-%m-%Y") if b == 0 else None,
                (h + 1) if m == 0 else None,
                f"{h:02d}:{m:02d} - {end_h:02d}:{end_m:02d}",
                f"{50000 + rng.integers(0, 9000)}.5", 20000.25, 10000.0, 9990.0,
                f"{2000 + rng.integers(0, 8000)}.0",
            ])
    return pd.DataFrame(rows)


def test_normalise_and_monthly_totals():
    days = [date(2022, 4, d) for d in range(1, 31)] + [date(2022, 5, 1), date(2022, 5, 2)]
    clean = processor.normalise(make_export(days), "DAM")
    assert len(clean) == 32 * 96
    assert clean["timestamp"].is_unique

    monthly = processor.monthly_summary(clean).set_index("month")
    apr = monthly.loc[pd.Timestamp("2022-04-01")]
    assert apr["days"] == 30 and bool(apr["complete_month"])
    assert apr["blocks"] == 30 * 96 and apr["block_coverage_pct"] == 100.0
    assert apr["mcv_mwh"] == 10000.0 * 0.25 * 30 * 96          # sum(MW) x 0.25 h
    assert apr["purchase_bid_mwh"] > 0 and apr["mcp_min"] <= apr["mcp_avg"] <= apr["mcp_max"]
    may = monthly.loc[pd.Timestamp("2022-05-01")]
    assert may["days"] == 2 and not bool(may["complete_month"])

    apr_raw = clean[clean["timestamp"].dt.month == 4]
    assert abs(apr["mcp_avg"] - apr_raw["mcp"].mean()) < 1e-9
    assert abs(apr["mcp_avg_rs_per_kwh"] - apr["mcp_avg"] / 1000) < 1e-12


def test_daily_summary_matches_page_definition():
    clean = processor.normalise(make_export([date(2022, 4, 1)]), "DAM")
    day = processor.daily_summary(clean).iloc[0]
    # IEX page: Total (MWh) == average MW x 24
    assert abs(day["mcv_mwh"] - 10000.0 * 24) < 1e-6


def test_month_windows():
    wins = list(scraper.month_windows(date(2022, 4, 1), date(2022, 6, 10)))
    assert [(w[0], w[1]) for w in wins] == [
        (date(2022, 4, 1), date(2022, 4, 30)),
        (date(2022, 5, 1), date(2022, 5, 31)),
        (date(2022, 6, 1), date(2022, 6, 10)),
    ]


def test_excel_datetime_dates_and_csv_headers():
    df = make_export([date(2022, 4, 1)])
    df[0] = df[0].astype(object)  # Excel files give real datetime cells, not strings
    df.iloc[2:, 0] = df.iloc[2:, 0].map(lambda v: pd.Timestamp(2022, 4, 1) if isinstance(v, str) else v)
    clean = processor.normalise(df, "DAM")
    assert clean["date"].iloc[0] == pd.Timestamp("2022-04-01")
