"""Hourly IEX data, the merge into an existing 15-minute CSV, and hourly weather alignment."""
import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import combine  # noqa: E402
import config  # noqa: E402
import page_client  # noqa: E402
import processor  # noqa: E402
import weather_hourly  # noqa: E402
from test_page import hourly_page_html, session, site_factory  # noqa: E402,F401


# ---------------------------------------------------------------- helpers
def existing_csv(path: Path, days: list, seed: int = 0) -> pd.DataFrame:
    """A 15-minute file in the same style as the user's: dd-mm-yy dates, 'd-mm-yy H:MM' timestamps, BOM."""
    rows = []
    for d in days:
        for b in range(96):
            ts = pd.Timestamp(d) + pd.Timedelta(minutes=15 * b)
            rows.append({"segment": "DAM", "date": f"{ts:%d-%m-%y}", "timestamp": f"{ts:%d-%m-%y} {ts.hour}:{ts:%M}",
                         "purchase_bid_mw": "50000.5", "sell_bid_mw": "20000.25", "mcv_mw": "10000",
                         "final_scheduled_mw": "9990", "mcp": f"{3000 + b}"})
    df = pd.DataFrame(rows)
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return df


def hourly_frame(days: list) -> pd.DataFrame:
    html = hourly_page_html(days, "number")
    return page_client.to_frame([html], "DAM", days[0], days[-1], "hourly")


# ---------------------------------------------------------------- hourly table parsing
@pytest.mark.parametrize("style", ["number", "block"])
def test_normalise_hourly_both_header_styles(style):
    days = [date(2022, 4, 1), date(2022, 4, 2)]
    df = page_client.to_frame([hourly_page_html(days, style)], "DAM", days[0], days[-1], "hourly")
    assert len(df) == 48 and df["timestamp"].is_unique
    assert df["timestamp"].iloc[0] == pd.Timestamp("2022-04-01 00:00")      # hour 1 = 00:00-01:00
    assert df["timestamp"].iloc[-1] == pd.Timestamp("2022-04-02 23:00")     # hour 24 = 23:00-24:00
    assert df["mcp"].iloc[0] == 3001.5 and df["mcv_mw"].eq(10000.0).all()


def test_hourly_table_with_zero_based_hours():
    raw = pd.DataFrame({"Date": ["01-04-2022"] * 3, "Hour": [0, 1, 2], "MCP (Rs/MWh) *": [1.0, 2.0, 3.0]})
    df = processor.normalise_hourly(raw, "DAM")
    assert list(df["timestamp"].dt.hour) == [0, 1, 2]


def test_hourly_rejects_quarter_hour_data():
    html = ("<table><tr><th>Date</th><th>Time Block</th><th>MCP (Rs/MWh)</th></tr>"
            "<tr><td>01-04-2022</td><td>00:00 - 00:15</td><td>3000.5</td></tr>"
            "<tr><td>01-04-2022</td><td>00:15 - 00:30</td><td>3001.5</td></tr></table>")
    with pytest.raises(RuntimeError, match="not hourly"):
        page_client.to_frame([html], "DAM", date(2022, 4, 1), date(2022, 4, 1), "hourly")


def test_summary_table_is_not_taken_as_hourly_data():
    html = hourly_page_html([date(2022, 4, 1)], "number")
    assert len(page_client.data_tables(html, "hourly")) == 1


def test_build_url_hourly_uses_configured_code():
    url = page_client.build_url("DAM", date(2022, 4, 1), date(2022, 4, 30), "hourly")
    assert f"interval={config.INTERVALS['hourly']}&" in url and "fromDate=01-04-2022" in url


def test_page_client_fetches_hourly_from_site(site_factory, session):
    site = site_factory("full", hour_style="number")
    df = page_client.PageClient(session, "requests").fetch("DAM", date(2022, 4, 1), date(2022, 4, 30), "hourly")
    assert len(df) == 30 * 24 and df["date"].nunique() == 30
    assert f"interval={config.INTERVALS['hourly']}" in site.hits[0]


# ---------------------------------------------------------------- merge
def test_attach_hourly_keeps_original_rows_and_adds_columns(tmp_path):
    days = [date(2022, 4, 1), date(2022, 4, 2)]
    path = tmp_path / "mine.csv"
    existing_csv(path, days)
    ex = combine.load_existing(path)
    combined, stats = combine.attach_hourly(ex, hourly_frame(days))

    assert len(combined) == 192 and stats["matched_rows"] == 192 and stats["unmatched_rows"] == 0
    assert list(combined.columns[:8]) == list(ex.columns)                 # original columns first, untouched
    assert combined[list(ex.columns)].equals(ex)                          # every original value identical
    assert list(combined.columns[8:]) == combine.HOURLY_COLS
    first = combined.iloc[0]
    assert first["hour_start"] == "01-04-22 0:00" and first["h_mcp"] == "3001.5" and first["h_mcv_mw"] == "10000"
    assert combined.iloc[3]["hour_start"] == "01-04-22 0:00"              # all four blocks share the hour
    assert combined.iloc[4]["hour_start"] == "01-04-22 1:00" and combined.iloc[4]["h_mcp"] == "3005.5"

    out = tmp_path / "out.csv"
    combine.write_csv(out, combined)
    assert out.read_bytes().startswith(b"\xef\xbb\xbf")                    # BOM, opens cleanly in Excel
    again, _ = combine.attach_hourly(combine.load_existing(out), hourly_frame(days))  # re-running is idempotent
    assert again.shape == combined.shape and list(again.columns) == list(combined.columns)


def test_attach_hourly_reports_unmatched_rows_and_extra_hours(tmp_path):
    path = tmp_path / "mine.csv"
    existing_csv(path, [date(2022, 4, 1), date(2022, 4, 2)])
    hourly = hourly_frame([date(2022, 4, 1), date(2022, 4, 3)])          # hourly has 04-03, lacks 04-02
    combined, stats = combine.attach_hourly(combine.load_existing(path), hourly)
    assert stats["matched_rows"] == 96 and stats["unmatched_rows"] == 96
    assert stats["hourly_not_in_existing"] == 24
    assert (combined.iloc[96:]["h_mcp"] == "").all()                      # blank, never invented


def test_crosscheck_confirms_hourly_equals_block_average(tmp_path):
    days = [date(2022, 4, 1)]
    path = tmp_path / "mine.csv"
    existing_csv(path, days)
    res = combine.crosscheck(combine.load_existing(path), hourly_frame(days))
    assert res["mcp"]["hours_compared"] == 24 and res["mcp"]["share_close"] == 1.0
    shifted = hourly_frame(days)
    shifted["mcp"] += 500
    assert combine.crosscheck(combine.load_existing(path), shifted)["mcp"]["share_close"] == 0.0


def test_append_quarter_rows_matches_file_style(tmp_path):
    path = tmp_path / "mine.csv"
    existing_csv(path, [date(2022, 4, 1)])
    ex = combine.load_existing(path)
    new = processor.normalise(pd.DataFrame(
        [["Date", "Time Block", "Purchase Bid (MW)", "Sell Bid (MW)", "MCV (MW)", "Final Scheduled Volume (MW)", "MCP (Rs/MWh)"],
         ["02-04-2022", "00:00 - 00:15", "100.5", "50", "20000.0", "19.99", "4000.25"],
         ["02-04-2022", "09:15 - 09:30", "100.5", "50", "20", "19", "5000"],
         ["01-04-2022", "23:45 - 24:00", "1", "1", "1", "1", "1"]]), "DAM")      # overlaps the file: ignored
    out, added = combine.append_quarter_rows(ex, new)
    assert added == 2 and len(out) == 98
    assert out.iloc[96].tolist() == ["DAM", "02-04-22", "02-04-22 0:00", "100.5", "50", "20000", "19.99", "4000.25"]
    assert out.iloc[97]["timestamp"] == "02-04-22 9:15"


def test_attach_weather_by_hour_and_blank_when_missing(tmp_path):
    path = tmp_path / "mine.csv"
    existing_csv(path, [date(2022, 4, 1)])
    idx = pd.date_range("2022-04-01 00:00", "2022-04-01 11:00", freq="h")        # weather only for the first 12 hours
    weather = pd.DataFrame({"om_temp_c": np.arange(12, dtype=float) + 20.5}, index=idx)
    combined, stats = combine.attach_weather(combine.load_existing(path), weather)
    assert len(combined) == 96 and stats["om_temp_c"] == 48
    assert combined.iloc[0]["om_temp_c"] == "20.5" and combined.iloc[7]["om_temp_c"] == "21.5"
    assert combined.iloc[48]["om_temp_c"] == ""


# ---------------------------------------------------------------- weather alignment
def true_irradiance(t_utc: pd.DatetimeIndex) -> np.ndarray:
    """Smooth day curve in UTC, peaking at 06:30 UTC (= 12:00 IST) and zero at night."""
    h = np.asarray(t_utc.hour + t_utc.minute / 60, dtype=float)
    return np.clip(np.sin((h - 0.5) / 12 * np.pi), 0, None) * 900


def window_mean(starts_utc: pd.DatetimeIndex, minutes: int = 60, step: int = 1) -> np.ndarray:
    return np.array([true_irradiance(pd.date_range(s, periods=minutes // step, freq=f"{step}min")).mean()
                     for s in starts_utc])


def test_period_end_alignment_recovers_true_hourly_mean():
    # Open-Meteo style: the value stamped U is the mean over the PRECEDING hour (U-1, U].
    utc = pd.date_range("2022-04-01 00:00", "2022-04-04 00:00", freq="h")
    raw = pd.DataFrame({"om_solar_wm2": window_mean(utc - pd.Timedelta(hours=1))}, index=utc)
    out = weather_hourly.align_to_ist(raw, {"om_solar_wm2": "period_end"}, date(2022, 4, 2), date(2022, 4, 2))
    ist_start_utc = out.index - pd.Timedelta(hours=5, minutes=30)                 # the IEX hour in UTC
    truth = window_mean(ist_start_utc)
    assert out["om_solar_wm2"].notna().all() and np.abs(out["om_solar_wm2"].values - truth).max() < 20
    assert weather_hourly.solar_peak_hour(out, "om_solar_wm2") == 12


def test_period_start_and_instant_alignment():
    utc = pd.date_range("2022-04-01 00:00", "2022-04-04 00:00", freq="h")
    start_vals = pd.DataFrame({"np_solar_wm2": window_mean(utc)}, index=utc)      # stamp = START of the hour
    out = weather_hourly.align_to_ist(start_vals, {"np_solar_wm2": "period_start"}, date(2022, 4, 2), date(2022, 4, 2))
    truth = window_mean(out.index - pd.Timedelta(hours=5, minutes=30))
    assert np.abs(out["np_solar_wm2"].values - truth).max() < 20

    ramp = pd.DataFrame({"om_temp_c": np.arange(len(utc), dtype=float)}, index=utc)  # value = hours since start
    inst = weather_hourly.align_to_ist(ramp, {"om_temp_c": "instant"}, date(2022, 4, 2), date(2022, 4, 2))
    # IEX hour 00:00 IST on 04-02 is centred on 01-04 19:00 UTC... i.e. UTC stamp K-5h
    assert inst["om_temp_c"].iloc[0] == (pd.Timestamp("2022-04-01 19:00") - utc[0]) / pd.Timedelta(hours=1)


def test_wrong_stamp_kind_shifts_the_peak_and_is_detected():
    utc = pd.date_range("2022-04-01 00:00", "2022-04-20 00:00", freq="h")
    raw = pd.DataFrame({"np_solar_wm2": window_mean(utc - pd.Timedelta(hours=1))}, index=utc)   # truly period_end
    good = weather_hourly.align_to_ist(raw, {"np_solar_wm2": "period_end"}, date(2022, 4, 2), date(2022, 4, 18))
    bad = weather_hourly.align_to_ist(raw, {"np_solar_wm2": "period_start"}, date(2022, 4, 2), date(2022, 4, 18))
    assert weather_hourly.solar_peak_hour(good, "np_solar_wm2") in (11, 12)
    assert weather_hourly.solar_peak_hour(bad, "np_solar_wm2") not in (11, 12)


# ---------------------------------------------------------------- weather download (mocked HTTP)
class FakeResponse:
    def __init__(self, data): self._data = data
    def json(self): return self._data


def fake_http(calls):
    def request(session, method, url, params=None, **kw):
        calls.append((url, params))
        lat = float(params["latitude"])
        if "open-meteo" in url:
            t = pd.date_range(params["start_date"], pd.Timestamp(params["end_date"]) + pd.Timedelta(hours=23), freq="h")
            hourly = {"time": [x.strftime("%Y-%m-%dT%H:%M") for x in t]}
            for api in config.OPEN_METEO_HOURLY_VARS:
                hourly[api] = [lat if api != "shortwave_radiation" else float(v)
                               for v in window_mean(t - pd.Timedelta(hours=1))]
            return FakeResponse({"hourly_units": {"temperature_2m": "°C", "shortwave_radiation": "W/m²"}, "hourly": hourly})
        t = pd.date_range(params["start"], pd.Timestamp(params["end"]) + pd.Timedelta(hours=23), freq="h")
        block = {}
        for name in params["parameters"].split(","):
            vals = window_mean(t) if name == "ALLSKY_SFC_SW_DWN" else np.full(len(t), lat + 1)
            block[name] = {x.strftime("%Y%m%d%H"): float(v) for x, v in zip(t, vals)}
        if "2022-04-01 06:00" in {x.strftime("%Y-%m-%d %H:%M") for x in t}:
            block["T2M"][pd.Timestamp("2022-04-01 06:00").strftime("%Y%m%d%H")] = -999.0   # NASA missing-value marker
        return FakeResponse({"properties": {"parameter": block},
                             "parameters": {"ALLSKY_SFC_SW_DWN": {"units": "Wh/m^2"}}})
    return request


def test_build_hourly_weather_two_sources_cities_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "WEATHER_REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "WEATHER_CITIES", {"A": (10.0, 70.0), "B": (30.0, 80.0)})
    calls = []
    monkeypatch.setattr(weather_hourly.net, "request_with_retry", fake_http(calls))

    wide, by_city, failures, report = weather_hourly.build(
        None, ["open-meteo", "nasa"], date(2022, 4, 1), date(2022, 4, 10))
    assert not failures
    assert set(wide.columns) == {c for c, _ in config.OPEN_METEO_HOURLY_VARS.values()} | set(config.NASA_HOURLY_VARS.values())
    assert len(wide) == 10 * 24 and wide.index[0] == pd.Timestamp("2022-04-01 00:00")
    assert wide["om_temp_c"].eq(20.0).all()                                          # mean of cities 10 and 30
    assert wide["np_temp_c"].dropna().eq(21.0).all()                                 # mean of 11 and 31
    # the -999 marker (06:00 UTC = 11:30 IST) touches exactly the two IEX hours that overlap it, as blanks
    assert list(wide.index[wide["np_temp_c"].isna()]) == [pd.Timestamp("2022-04-01 11:00"), pd.Timestamp("2022-04-01 12:00")]
    assert (wide.min() > -900).all()
    assert any("Wh/m^2" in line for line in report)
    assert set(by_city["city"]) == {"A", "B"} and set(by_city["source"]) == {"open-meteo", "nasa"}
    assert any("OK" in line for line in report if "solar peaks" in line)

    n = len(calls)
    weather_hourly.build(None, ["open-meteo"], date(2022, 4, 1), date(2022, 4, 10))   # finished past period: cached
    assert len(calls) == n


def test_failed_city_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    monkeypatch.setattr(config, "WEATHER_REQUEST_DELAY_SECONDS", 0)
    monkeypatch.setattr(config, "WEATHER_CITIES", {"A": (10.0, 70.0), "B": (30.0, 80.0)})
    ok = fake_http([])

    def flaky(session, method, url, params=None, **kw):
        if float(params["latitude"]) == 30.0:
            raise RuntimeError("HTTP 429 for " + url)
        return ok(session, method, url, params=params, **kw)

    monkeypatch.setattr(weather_hourly.net, "request_with_retry", flaky)
    wide, _, failures, report = weather_hourly.build(None, ["open-meteo"], date(2022, 4, 1), date(2022, 4, 3))
    assert len(failures) == 1 and "B" in failures[0][0]
    assert wide["om_temp_c"].eq(10.0).all() and any("1 of 2 cities" in line for line in report)
