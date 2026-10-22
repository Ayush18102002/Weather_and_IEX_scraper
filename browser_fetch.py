"""Replay requests through a headless browser session (shares the page's cookies).

Used only when a plain HTTP replay is rejected (401/403) by the site.
"""
import logging
import random
import time

import config

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 502, 503, 504}


class BrowserFetcher:
    def __init__(self, page_url: str, headless: bool = True):
        self.page_url = page_url
        self.headless = headless

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(user_agent=config.USER_AGENT)
        page = self._context.new_page()
        page.goto(self.page_url, wait_until="networkidle", timeout=config.REQUEST_TIMEOUT_SECONDS * 1000)
        return self

    def __exit__(self, *exc):
        self._browser.close()
        self._pw.stop()

    def fetch(self, method: str, url: str, body: str | None, headers: dict) -> str:
        last: Exception | None = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                resp = self._context.request.fetch(
                    url, method=method, data=body, headers=headers,
                    timeout=config.REQUEST_TIMEOUT_SECONDS * 1000,
                )
                if resp.status in RETRY_STATUS:
                    raise ConnectionError(f"HTTP {resp.status}")
                if resp.status >= 400:
                    raise RuntimeError(f"HTTP {resp.status} for {url}")
                return resp.text()
            except RuntimeError:
                raise
            except Exception as exc:
                last = exc
                if attempt == config.MAX_RETRIES:
                    break
                wait = config.BACKOFF_BASE_SECONDS**attempt + random.uniform(0, 1)
                log.warning("Browser fetch attempt %d failed (%s); retrying in %.1fs", attempt, exc, wait)
                time.sleep(wait)
        raise RuntimeError(f"Browser fetch failed after {config.MAX_RETRIES} attempts: {url}") from last
