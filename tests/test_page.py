"""Tests for the URL-based page reader, using a local simulated Market Snapshot page.

The simulated page copies the structure seen on the real page: a Date cell merged (rowspan) across the
day, an Hour cell merged across 4 blocks, 96 rows per day, and a separate Summary table underneath.
"""
import json
import sys
import threading
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config  # noqa: E402
import page_client  # noqa: E402
import processor  # noqa: E402

HEAD = ("<thead><tr><th>Date</th><th>Hour</th><th>Time Block</th><th>Purchase Bid (MW)</th>"
        "<th>Sell Bid (MW)</th><th>MCV (MW)</th><th>Final Scheduled Volume (MW)</th>"
        "<th>MCP (Rs/MWh) *</th></tr></thead>")
SUMMARY = ("<table><tr><th>Date</th><th>Summary</th><th>Purchase Bid</th><th>MCP (Rs/MWh) *</th></tr>"
           "<tr><td>01-04-2022</td><td>Avg (MW)</td><td>43303.12</td><td>7682.60</td></tr></table>")


def day_html(d) -> str:
    rows = []
    for b in range(96):
        h, m = divmod(b * 15, 60)
        eh, em = divmod((b + 1) * 15, 60)
        cells = []
        if b == 0:
            cells.append(f'<td rowspan="96">{d:%d-%m-%Y}</td>')
        if b % 4 == 0:
            cells.append(f'<td rowspan="4">{b // 4 + 1}</td>')
        cells.append(f"<td>{h:02d}:{m:02d} - {eh:02d}:{em:02d}</td>")
        cells += ["<td>50000.50</td>", "<td>20000.25</td>", "<td>10000.00</td>", "<td>9990.00</td>",
                  f"<td>{3000 + b}.00</td>"]
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return "".join(rows)


def page_html(days: list, per_page: int | None) -> str:
    chunks = [days[i:i + per_page] for i in range(0, len(days), per_page)] if per_page else [days]
    first = "".join(day_html(d) for d in chunks[0])
    later = [["".join(day_html(d) for d in c)][0] for c in chunks[1:]]
    pager = ""
    if per_page:
        pager = ("<button aria-label='Go to next page' id='next'>next</button>"
                 f"<script>const PAGES={json.dumps(later)};let i=0;const b=document.getElementById('next');"
                 "if(!PAGES.length)b.disabled=true;"
                 "b.onclick=()=>{document.querySelector('tbody').innerHTML=PAGES[i++];"
                 "if(i>=PAGES.length)b.disabled=true;};</script>")
    return (f"<html><body><h1>Market Snapshot</h1><table>{HEAD}<tbody>{first}</tbody></table>"
            f"{pager}{SUMMARY}</body></html>")


def hourly_page_html(days: list, style: str = "number") -> str:
    """Hourly table: 24 rows per day. style 'number' = Hour column 1..24; 'block' adds '00:00 - 01:00'."""
    head = ("<thead><tr><th>Date</th><th>Hour</th>" + ("<th>Time Block</th>" if style == "block" else "") +
            "<th>Purchase Bid (MW)</th><th>Sell Bid (MW)</th><th>MCV (MW)</th>"
            "<th>Final Scheduled Volume (MW)</th><th>MCP (Rs/MWh) *</th></tr></thead>")
    rows = []
    for d in days:
        for h in range(24):
            cells = []
            if h == 0:
                cells.append(f'<td rowspan="24">{d:%d-%m-%Y}</td>')
            cells.append(f"<td>{h + 1}</td>")
            if style == "block":
                cells.append(f"<td>{h:02d}:00 - {h + 1:02d}:00</td>")
            cells += ["<td>50000.50</td>", "<td>20000.25</td>", "<td>10000.00</td>", "<td>9990.00</td>",
                      f"<td>{3000 + 4 * h + 1.5}</td>"]   # = mean of the four 15-minute MCPs of that hour
            rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"<html><body><table>{head}<tbody>{''.join(rows)}</tbody></table>{SUMMARY}</body></html>"


class Site:
    """Local stand-in for the IEX page. mode: 'full', 'paginated', 'ignore_params', 'blocked'."""

    def __init__(self, mode: str = "full", per_page: int = 10, hour_style: str = "number"):
        self.mode, self.per_page, self.hits, self.hour_style = mode, per_page, [], hour_style
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                site.hits.append(self.path)
                if site.mode == "blocked":
                    self.send_response(403)
                    self.end_headers()
                    return
                q = parse_qs(urlparse(self.path).query)
                if site.mode == "ignore_params":
                    days = [date(2026, 10, 13)]
                else:
                    s = datetime.strptime(q["fromDate"][0], "%d-%m-%Y").date()
                    e = datetime.strptime(q["toDate"][0], "%d-%m-%Y").date()
                    days = [d.date() for d in pd.date_range(s, e)]
                if q.get("interval", [""])[0] == config.INTERVALS["hourly"]:
                    body = hourly_page_html(days, site.hour_style).encode()
                else:
                    body = page_html(days, site.per_page if site.mode == "paginated" else None).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/market-snapshot"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def site_factory(monkeypatch):
    sites = []

    def make(mode="full", per_page=10, hour_style="number"):
        s = Site(mode, per_page, hour_style)
        sites.append(s)
        seg = dict(config.SEGMENTS["DAM"], page_url=s.url)
        monkeypatch.setitem(config.SEGMENTS, "DAM", seg)
        return s

    yield make
    for s in sites:
        s.close()


@pytest.fixture
def session():
    s = requests.Session()
    s.trust_env = False  # never go through a proxy for localhost
    return s


def test_build_url_matches_real_page_format():
    url = page_client.build_url("DAM", date(2022, 4, 1), date(2022, 4, 30))
    assert url == ("https://www.iexindia.com/market-data/day-ahead-market/market-snapshot"
                   "?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false"
                   "&toDate=30-04-2022&fromDate=01-04-2022")


def test_rowspan_table_is_expanded_and_summary_ignored():
    html = page_html([date(2022, 4, 1), date(2022, 4, 2)], None)
    assert len(page_client.data_tables(html)) == 1            # the Summary table is not picked
    df = page_client.to_frame([html], "DAM", date(2022, 4, 1), date(2022, 4, 2))
    assert len(df) == 192 and df["date"].nunique() == 2
    assert df["timestamp"].is_unique and df["timestamp"].iloc[-1] == pd.Timestamp("2022-04-02 23:45")
    assert df["mcv_mw"].eq(10000.0).all()


def test_out_of_range_dates_raise_and_extra_days_are_clipped():
    html = page_html([date(2026, 10, 13)], None)
    with pytest.raises(RuntimeError, match="none inside"):
        page_client.to_frame([html], "DAM", date(2022, 4, 1), date(2022, 4, 30))
    html = page_html([date(2022, 3, 31), date(2022, 4, 1)], None)
    df = page_client.to_frame([html], "DAM", date(2022, 4, 1), date(2022, 4, 30))
    assert df["date"].nunique() == 1


def test_requests_transport_reads_full_month(site_factory, session):
    site = site_factory("full")
    client = page_client.PageClient(session, "requests")
    df = client.fetch("DAM", date(2022, 4, 1), date(2022, 4, 30))
    assert len(df) == 30 * 96 and df["date"].nunique() == 30
    assert "fromDate=01-04-2022" in site.hits[0] and "toDate=30-04-2022" in site.hits[0]


def test_requests_transport_rejects_ignored_params(site_factory, session):
    site_factory("ignore_params")
    with pytest.raises(RuntimeError, match="--transport browser"):
        page_client.PageClient(session, "requests").fetch("DAM", date(2022, 4, 1), date(2022, 4, 30))


def test_blocked_site_gives_clear_error(site_factory, session):
    site_factory("blocked")
    with pytest.raises(RuntimeError, match="403"):
        page_client.PageClient(session, "requests").fetch("DAM", date(2022, 4, 1), date(2022, 4, 30))


# ---------------------------------------------------------------- real browser tests
@pytest.fixture
def browser_ok():
    pytest.importorskip("playwright.sync_api")
    import os
    from playwright.sync_api import sync_playwright
    exe = os.environ.get("TEST_CHROMIUM_PATH")
    try:
        with sync_playwright() as p:
            p.chromium.launch(executable_path=exe).close()
    except Exception as exc:
        pytest.skip(f"no usable Chromium here: {str(exc)[:80]}")
    if exe:
        config.BROWSER_EXECUTABLE = exe
    yield
    config.BROWSER_EXECUTABLE = None


def test_auto_falls_back_to_browser_and_follows_pagination(site_factory, session, browser_ok):
    site_factory("paginated", per_page=10)
    with page_client.PageClient(session, "auto") as client:
        df = client.fetch("DAM", date(2022, 4, 1), date(2022, 4, 30))   # plain request sees only 10 days
        assert client.transport == "browser"
        assert df["date"].nunique() == 30 and len(df) == 30 * 96
        df2 = client.fetch("DAM", date(2022, 5, 1), date(2022, 5, 31))  # next month reuses the browser
        assert df2["date"].nunique() == 31


def test_browser_transport_directly(site_factory, session, browser_ok):
    site_factory("paginated", per_page=7)
    with page_client.PageClient(session, "browser") as client:
        df = client.fetch("DAM", date(2022, 6, 1), date(2022, 6, 30))
    assert df["date"].nunique() == 30 and df["timestamp"].is_unique
