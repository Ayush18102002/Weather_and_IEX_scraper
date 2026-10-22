"""One-time "learn" step: capture the page's own data request for a month you choose,
turn it into a template with date placeholders, and save it to learned_api.json.

After that `python main.py` replays the template for every month automatically.

Needs: pip install playwright && playwright install chromium
"""
import json
import logging
import re
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from urllib.parse import unquote

import pandas as pd

import config

log = logging.getLogger(__name__)
LEARNED_PATH = config.BASE_DIR / "learned_api.json"

# Longest / most specific formats first; bare digit formats last (they can match inside other numbers).
DATE_FORMATS = [
    "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%b-%Y", "%d %b %Y",
    "%d-%B-%Y", "%d %B %Y", "%d.%m.%Y", "%Y%m%d", "%d%m%Y",
]
SKIP_HEADERS = {"cookie", "host", "content-length", "accept-encoding", "connection", "user-agent"}


# ---------------------------------------------------------------- pure helpers
def build_template(url: str, body: str | None, start: date, end: date):
    """Replace the chosen start/end dates in url/body with <<START:fmt>> / <<END:fmt>> tokens.

    END1 means the site sends the day after the end date (exclusive end).
    Returns (url_template, body_template, chosen_formats) or None if dates were not found.
    """
    chosen: dict[str, str] = {}
    texts = [url, body or ""]
    candidates = {"START": start, "END": end, "END1": end + timedelta(days=1)}
    for token in ("START", "END", "END1"):
        if token == "END1" and "END" in chosen:
            continue
        for fmt in DATE_FORMATS:
            s = candidates[token].strftime(fmt)
            if any(s in t for t in texts):
                chosen[token] = fmt
                texts = [t.replace(s, f"<<{token}:{fmt}>>") for t in texts]
                break
    if "START" not in chosen or not ({"END", "END1"} & set(chosen)):
        return None
    return texts[0], (texts[1] if body is not None else None), chosen


def find_records(obj, path: tuple = ()):
    """Find the largest list of dicts inside a JSON object. Returns (length, path) or None."""
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
        return len(obj), list(path)
    best = None
    if isinstance(obj, dict):
        for key, value in obj.items():
            found = find_records(value, path + (key,))
            if found and (best is None or found[0] > best[0]):
                best = found
    return best


def extract_embedded_records(text: str) -> list[dict]:
    """Find the largest JSON list of objects embedded anywhere in a text response
    (for example a streamed / framework-wrapped payload that is not pure JSON)."""
    for candidate in (text, text.replace('\\"', '"')):
        decoder = json.JSONDecoder()
        best: list[dict] = []
        pos, attempts = 0, 0
        while attempts < 3000:
            pos = candidate.find("[{", pos)
            if pos == -1:
                break
            attempts += 1
            try:
                obj, end = decoder.raw_decode(candidate, pos)
            except ValueError:
                pos += 2
                continue
            if isinstance(obj, list) and len(obj) > len(best) and all(isinstance(x, dict) for x in obj[:5]):
                best = obj
            pos = end
        if best:
            return best
    return []


def parse_response(text: str, fmt: str, records_path: list[str] | None = None) -> pd.DataFrame:
    """Turn a response body (JSON, HTML table, or embedded JSON) into a raw DataFrame."""
    if fmt == "html":
        tables = pd.read_html(StringIO(text))
        return max(tables, key=len)
    if fmt == "embedded":
        return pd.DataFrame(extract_embedded_records(text))
    data = json.loads(text)
    for key in records_path or []:
        data = data[key]
    return pd.DataFrame(data)


def load(segment: str) -> dict | None:
    if not LEARNED_PATH.exists():
        return None
    return json.loads(LEARNED_PATH.read_text(encoding="utf-8")).get(segment)


def save(segment: str, entry: dict) -> None:
    data = json.loads(LEARNED_PATH.read_text(encoding="utf-8")) if LEARNED_PATH.exists() else {}
    data[segment] = entry
    LEARNED_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _candidate_rows(text: str, content_type: str):
    """Return (format, records_path, row_count) if the response looks like a data table."""
    stripped = text.lstrip()
    ctype = content_type.lower()
    try:
        if "json" in ctype or stripped[:1] in ("[", "{"):
            found = find_records(json.loads(text))
            if found:
                return ("json", found[1], found[0])
    except Exception:
        pass
    if "html" in ctype or stripped[:1] == "<":
        try:
            return ("html", [], len(parse_response(text, "html")))
        except Exception:
            pass
    rows = extract_embedded_records(text)
    if rows:
        return ("embedded", [], len(rows))
    return None


# ---------------------------------------------------------------- browser step
def run(segment: str, start: date, end: date, seconds: int = 240) -> Path | None:
    from playwright.sync_api import sync_playwright

    page_url = config.SEGMENTS[segment]["page_url"]
    best: dict | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page(user_agent=config.USER_AGENT)

        def on_response(resp):
            nonlocal best
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document"):
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            ctype_req = req.headers.get("content-type", "")
            url_txt = unquote(resp.url)
            body_txt = unquote(body) if body and "x-www-form-urlencoded" in ctype_req else body
            tpl = build_template(url_txt, body_txt, start, end)
            if tpl is None:
                return
            try:
                text = resp.text()
            except Exception:
                return
            cand = _candidate_rows(text, resp.headers.get("content-type", ""))
            if not cand or cand[2] < 50:
                return
            if best is None or cand[2] > best["rows"]:
                best = {
                    "tpl": tpl, "method": req.method, "headers": dict(req.headers),
                    "fmt": cand[0], "records_path": cand[1], "rows": cand[2], "text": text,
                }
                print(f"  captured a matching request: {req.method} {resp.url[:110]}  ({cand[2]} rows)")

        page.on("response", on_response)
        page.goto(page_url, wait_until="networkidle", timeout=config.REQUEST_TIMEOUT_SECONDS * 1000)
        print("\n=== In the browser window that just opened ===")
        print("  1. Interval  -> 15-Min-Block")
        print(f"  2. Delivery Period -> {start:%d-%m-%Y} to {end:%d-%m-%Y}  (exactly this range)")
        print("  3. Click 'Update Report' (do NOT click Export)")
        print(f"Waiting up to {seconds}s ...\n")
        waited = 0
        while waited < seconds and best is None:
            page.wait_for_timeout(1000)
            waited += 1
        if best is not None:
            page.wait_for_timeout(4000)  # let a bigger response arrive if there is one
        browser.close()

    if best is None:
        print("Nothing matched. Make sure you selected exactly that date range and clicked Update Report.")
        print("If it still fails, run `python main.py --discover` and send me discovery_output/discovery_log.json.")
        return None

    url_tpl, body_tpl, chosen = best["tpl"]
    headers = {k: v for k, v in best["headers"].items() if k.lower() not in SKIP_HEADERS and not k.startswith(":")}
    entry = {
        "learned_at": datetime.now().isoformat(timespec="seconds"),
        "learned_range": [start.isoformat(), end.isoformat()],
        "method": best["method"], "url": url_tpl, "body": body_tpl, "headers": headers,
        "response_format": best["fmt"], "records_path": best["records_path"],
        "date_formats": chosen, "transport": "requests",
    }
    save(segment, entry)
    print(f"\nSaved {LEARNED_PATH.name}. Date formats found: {chosen}")
    print("Note: that file may contain request headers (e.g. tokens). Do not share it publicly.")

    # Validate that the captured rows parse into clean 15-minute data.
    try:
        import processor
        raw = parse_response(best["text"], best["fmt"], best["records_path"])
        clean = processor.normalise(raw, segment)
        print(f"Validation OK: {len(clean)} blocks, {clean['date'].nunique()} days, "
              f"{clean['timestamp'].min()} -> {clean['timestamp'].max()}")
    except Exception as exc:
        print(f"WARNING: rows captured but could not be mapped to the expected columns ({exc}).")
        try:
            print("Columns seen:", list(parse_response(best["text"], best["fmt"], best["records_path"]).columns))
        except Exception:
            pass
        print("Add a column_map to learned_api.json (e.g. {\"someKey\": \"MCP\"}) and re-run.")
        return LEARNED_PATH

    # Check whether a plain HTTP replay works; otherwise fall back to browser transport.
    try:
        import api_client
        import net
        again = api_client.fetch_range(net.make_session(), segment, start, end)
        ok = len(again) >= 0.9 * len(clean)
        print("Replay without a browser:", "OK" if ok else "returned fewer rows than expected")
        if not ok:
            raise RuntimeError("row count mismatch")
    except Exception as exc:
        entry["transport"] = "browser"
        save(segment, entry)
        print(f"Plain HTTP replay failed ({exc}); set transport=browser (uses a headless browser).")
    return LEARNED_PATH
