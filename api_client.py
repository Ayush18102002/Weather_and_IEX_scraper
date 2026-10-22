"""Fetch IEX data for a date range.

Ways to get the data, in priority order:
  1. config.API[segment]["url"]      - a JSON endpoint filled in by hand
  2. config.SEGMENTS[segment]["query"] - page mode: load the public page with the dates in the URL (default)
  3. learned_api.json                - written by `python main.py --learn`
"""
import logging
import re
from datetime import date, timedelta

import pandas as pd

import config
import learn
import net
import page_client
from processor import normalise

log = logging.getLogger(__name__)
TOKEN = re.compile(r"<<(START|END|END1):([^>]*)>>")
_browser_fetcher = None
_page_client = None


class NotConfigured(RuntimeError):
    """Raised when no data endpoint has been configured for a segment."""


def set_browser_fetcher(fetcher) -> None:
    global _browser_fetcher
    _browser_fetcher = fetcher


def set_page_client(client) -> None:
    global _page_client
    _page_client = client


def render_tokens(text: str | None, start: date, end: date) -> str | None:
    if text is None:
        return None
    values = {"START": start, "END": end, "END1": end + timedelta(days=1)}
    return TOKEN.sub(lambda m: values[m.group(1)].strftime(m.group(2)), text)


def _render(obj, fmt: dict):
    if isinstance(obj, str):
        return obj.format(**fmt)
    if isinstance(obj, dict):
        return {k: _render(v, fmt) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_render(v, fmt) for v in obj]
    return obj


def uses_page_mode(segment: str) -> bool:
    return bool(config.SEGMENTS.get(segment, {}).get("query")) and not config.API.get(segment, {}).get("url")


def api_configured(segment: str) -> bool:
    return (
        bool(config.API.get(segment, {}).get("url"))
        or bool(config.SEGMENTS.get(segment, {}).get("query"))
        or learn.load(segment) is not None
    )


def _fetch_learned(session, cfg: dict, start: date, end: date) -> pd.DataFrame:
    url = render_tokens(cfg["url"], start, end)
    body = render_tokens(cfg.get("body"), start, end)
    headers = cfg.get("headers") or {}
    method = cfg.get("method", "GET")
    try:
        if _browser_fetcher is not None:
            text = _browser_fetcher.fetch(method, url, body, headers)
        else:
            text = net.request_with_retry(session, method, url, data=body, headers=headers).text
    except RuntimeError as exc:
        if "HTTP 40" in str(exc) and _browser_fetcher is None:
            raise RuntimeError(f"{exc}. The site rejected a plain request; try: python main.py --transport browser") from exc
        raise
    df = learn.parse_response(text, cfg.get("response_format", "json"), cfg.get("records_path"))
    return df.rename(columns=cfg.get("column_map") or {})


def _fetch_manual(session, cfg: dict, start: date, end: date) -> pd.DataFrame:
    fmt = {"start": start, "end": end}
    kwargs = {"params": _render(cfg.get("params") or {}, fmt)}
    if cfg.get("json_body") is not None:
        kwargs["json"] = _render(cfg["json_body"], fmt)
    resp = net.request_with_retry(session, cfg.get("method", "GET"), cfg["url"], **kwargs)
    data = resp.json()
    for key in cfg.get("records_path") or []:
        data = data[key]
    return pd.DataFrame(data).rename(columns=cfg.get("column_map") or {})


def fetch_range(session, segment: str, start: date, end: date, interval: str = "15min") -> pd.DataFrame:
    """Download data for [start, end] (interval: "15min" or "hourly") and return a normalised frame."""
    global _page_client
    manual = config.API.get(segment, {})
    learned = learn.load(segment)
    if interval != "15min" and not uses_page_mode(segment):
        raise NotConfigured("Hourly download needs page mode (SEGMENTS[...]['query'] in config.py).")
    if manual.get("url"):
        df = _fetch_manual(session, manual, start, end)
    elif uses_page_mode(segment):
        if _page_client is None:
            _page_client = page_client.PageClient(session, transport="requests")
        return _page_client.fetch(segment, start, end, interval)  # already normalised
    elif learned:
        df = _fetch_learned(session, learned, start, end)
    else:
        raise NotConfigured(
            f"No data source known for {segment}. Set SEGMENTS[...]['query'] in config.py, "
            "run `python main.py --learn`, or use --source files with manually exported data."
        )
    if df.empty:
        raise RuntimeError(f"{segment}: no rows returned for {start}..{end}")
    return normalise(df, segment)


def learned_transport(segment: str) -> str:
    entry = learn.load(segment)
    return entry.get("transport", "requests") if entry else "requests"
