"""Offline tests for learn mode and replay, using simulated requests/responses."""
import json
import sys
from datetime import date
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import api_client  # noqa: E402
import learn  # noqa: E402
import processor  # noqa: E402

START, END = date(2022, 4, 1), date(2022, 4, 30)


def fake_rows(start: date, end: date, style: str = "camel"):
    rows = []
    for d in pd.date_range(start, end):
        for b in range(96):
            h, m = divmod(b * 15, 60)
            e_h, e_m = divmod((b + 1) * 15, 60)
            rows.append({
                "Date": d.strftime("%d-%m-%Y"),
                "TimeBlock": f"{h:02d}:{m:02d} - {e_h:02d}:{e_m:02d}",
                "PurchaseBid": 50000.0, "SellBid": 20000.0, "MCV": 10000.0,
                "FinalScheduledVolume": 9990.0, "MCP": 3000.0 + b,
            })
    return rows


def test_build_template_get_query_dmy():
    url = "https://x.example/api/dam?from=01-04-2022&to=30-04-2022&interval=15"
    tpl = learn.build_template(url, None, START, END)
    assert tpl is not None
    assert tpl[0] == "https://x.example/api/dam?from=<<START:%d-%m-%Y>>&to=<<END:%d-%m-%Y>>&interval=15"
    assert tpl[1] is None


def test_build_template_post_body_iso_and_exclusive_end():
    body = json.dumps({"fromDate": "2022-04-01", "toDate": "2022-05-01", "interval": "15"})
    tpl = learn.build_template("https://x.example/api/dam", body, START, END)
    assert tpl is not None and "END1" in tpl[2]
    rendered = api_client.render_tokens(tpl[1], date(2022, 6, 1), date(2022, 6, 30))
    assert json.loads(rendered) == {"fromDate": "2022-06-01", "toDate": "2022-07-01", "interval": "15"}


def test_build_template_encoded_slashes_after_unquote():
    url = unquote("https://x.example/api?d1=01%2F04%2F2022&d2=30%2F04%2F2022")
    tpl = learn.build_template(url, None, START, END)
    assert tpl is not None and "<<START:%d/%m/%Y>>" in tpl[0]


def test_build_template_none_when_dates_missing():
    assert learn.build_template("https://x.example/api?today=1", None, START, END) is None


def test_find_records_nested_and_toplevel():
    rows = fake_rows(date(2022, 4, 1), date(2022, 4, 1))
    assert learn.find_records({"status": "ok", "data": {"rows": rows}}) == (96, ["data", "rows"])
    assert learn.find_records(rows) == (96, [])
    assert learn.find_records({"a": 1}) is None


def test_header_mapping_api_style_keys():
    clean = processor.normalise(pd.DataFrame(fake_rows(date(2022, 4, 1), date(2022, 4, 2))), "DAM")
    assert len(clean) == 192 and clean["mcv_mw"].eq(10000.0).all() and clean["mcp"].notna().all()
    assert clean["timestamp"].iloc[0] == pd.Timestamp("2022-04-01 00:00")


def test_replay_learned_template_month_by_month(tmp_path, monkeypatch):
    import config
    # page mode (SEGMENTS[...]["query"]) takes priority; switch it off to exercise the learned route
    monkeypatch.setitem(config.SEGMENTS, "DAM", {k: v for k, v in config.SEGMENTS["DAM"].items() if k != "query"})
    monkeypatch.setattr(learn, "LEARNED_PATH", tmp_path / "learned_api.json")
    tpl_url, _, chosen = learn.build_template(
        "https://x.example/api/dam?from=01-04-2022&to=30-04-2022", None, START, END)
    learn.save("DAM", {"method": "GET", "url": tpl_url, "body": None, "headers": {},
                       "response_format": "json", "records_path": ["data"], "date_formats": chosen})
    seen = []

    class FakeResp:
        def __init__(self, text): self.text = text

    def fake_request(session, method, url, **kw):
        seen.append(url)
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        s = pd.to_datetime(q["from"], format="%d-%m-%Y").date()
        e = pd.to_datetime(q["to"], format="%d-%m-%Y").date()
        return FakeResp(json.dumps({"data": fake_rows(s, e)}))

    monkeypatch.setattr(api_client.net, "request_with_retry", fake_request)
    assert api_client.api_configured("DAM")
    df = api_client.fetch_range(None, "DAM", date(2023, 2, 1), date(2023, 2, 28))
    assert seen == ["https://x.example/api/dam?from=01-02-2023&to=28-02-2023"]
    assert len(df) == 28 * 96 and df["date"].nunique() == 28


def test_html_response_is_parsed():
    pytest.importorskip("lxml")
    html = ("<table><tr><th>Date</th><th>Time Block</th><th>MCP (Rs/MWh)</th></tr>"
            "<tr><td>01-04-2022</td><td>00:00 - 00:15</td><td>3000.5</td></tr></table>")
    clean = processor.normalise(learn.parse_response(html, "html"), "DAM")
    assert len(clean) == 1 and clean["mcp"].iloc[0] == 3000.5
