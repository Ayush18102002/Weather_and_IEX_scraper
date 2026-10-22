"""Non-JSON responses: streamed/framework payloads with embedded JSON, and HTML pages."""
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import learn  # noqa: E402
import processor  # noqa: E402
from test_learn import fake_rows  # noqa: E402
from datetime import date  # noqa: E402


def streamed_payload(rows):
    # Looks like a framework stream: several lines, JSON buried inside one of them.
    return '0:["$@1",["development"]]\n1:' + json.dumps({"ok": True, "result": {"table": rows}}) + "\n"


def test_embedded_rows_found_in_streamed_text():
    rows = fake_rows(date(2022, 4, 1), date(2022, 4, 2))
    text = streamed_payload(rows)
    assert len(learn.extract_embedded_records(text)) == 192
    fmt, path, n = learn._candidate_rows(text, "text/x-component")
    assert (fmt, n) == ("embedded", 192)
    clean = processor.normalise(learn.parse_response(text, "embedded"), "DAM")
    assert len(clean) == 192


def test_embedded_rows_with_escaped_quotes():
    rows = fake_rows(date(2022, 4, 1), date(2022, 4, 1))
    text = '2:"' + json.dumps(rows).replace('"', '\\"') + '"'
    assert len(learn.extract_embedded_records(text)) == 96


def test_no_rows_returns_empty():
    assert learn.extract_embedded_records("hello [not json] {nope}") == []
    assert learn._candidate_rows("just text", "text/plain") is None


def test_html_candidate_detected():
    pytest.importorskip("lxml")
    html = ("<html><body><table><tr><th>Date</th><th>Time Block</th><th>MCP (Rs/MWh)</th></tr>"
            "<tr><td>01-04-2022</td><td>00:00 - 00:15</td><td>3000.5</td></tr></table></body></html>")
    assert learn._candidate_rows(html, "text/html; charset=utf-8") == ("html", [], 1)
