"""Record every network call the IEX page makes (needs: pip install playwright && playwright install chromium).

Opens the page in a real browser and keeps listening for `seconds`. During that time, in the
browser window: set Interval = 15-Min-Block, choose a Delivery Period (e.g. 01-04-2022 to 30-04-2022)
and click "Update Report". Everything the page requests is saved to discovery_output/.
"""
import json
import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)
DATA_WORDS = ("mcp", "mcv")


def run(segment: str, seconds: int = 60, headed: bool = True) -> Path:
    from playwright.sync_api import sync_playwright

    url = config.SEGMENTS[segment]["page_url"]
    out_dir = config.BASE_DIR / "discovery_output"
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("response_*.txt"):
        old.unlink()
    records: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        page = browser.new_page(user_agent=config.USER_AGENT)

        def on_response(resp):
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document"):
                return
            try:
                text = resp.text()
            except Exception:
                text = None
            low = (text or "").lower()
            entry = {
                "url": resp.url,
                "method": req.method,
                "status": resp.status,
                "resource_type": req.resource_type,
                "content_type": resp.headers.get("content-type", ""),
                "request_headers": {k: v for k, v in req.headers.items() if k.lower() != "cookie"},
                "post_data": (req.post_data or "")[:3000] if req.post_data else None,
                "size": len(text) if text else 0,
                "looks_like_data": bool(text) and all(w in low for w in DATA_WORDS),
            }
            if entry["looks_like_data"]:
                name = f"response_{len(records) + 1}.txt"
                (out_dir / name).write_text(text[:3_000_000], encoding="utf-8", errors="replace")
                entry["saved_as"] = name
            else:
                entry["body_preview"] = (text or "")[:300]
            records.append(entry)

        page.on("response", on_response)
        page.goto(url, wait_until="networkidle", timeout=config.REQUEST_TIMEOUT_SECONDS * 1000)
        print(f"\nPage loaded. For the next {seconds}s, in the browser window:")
        print("  1. Interval -> 15-Min-Block")
        print("  2. Delivery Period -> 01-04-2022 to 30-04-2022")
        print("  3. Click 'Update Report'\n")
        page.wait_for_timeout(seconds * 1000)
        (out_dir / "page.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_dir / "page.png"), full_page=True)
        browser.close()

    log_path = out_dir / "discovery_log.json"
    log_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
    print(f"Captured {len(records)} calls:")
    for r in records:
        flag = "   <-- looks like the data" if r["looks_like_data"] else ""
        print(f"  {r['method']:4} {r['status']} {r['resource_type']:8} {r['content_type'][:28]:28} {r['size']:>8}b  {r['url'][:90]}{flag}")
    print(f"\nFull details: {log_path}")
    print("Please send me discovery_log.json (and any response_*.txt files) so I can finish the setup.")
    return log_path
