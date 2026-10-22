"""Download IEX Market Snapshot data by loading the public page with the date range in the URL.

The page reads its range and interval from the query string, e.g.
  .../market-snapshot?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false&toDate=30-04-2022&fromDate=01-04-2022
and shows the table for that range. We read that table from the HTML:
first with a plain request, and with a real (headless) browser if the plain request does not
return the table for the requested dates or the table is paginated.

interval is "15min" (15-minute blocks) or "hourly" (see config.INTERVALS).

This behaves like a normal visitor loading a public page: it identifies itself honestly (config.USER_AGENT),
pauses between months, and does not try to get around any blocking. If the site blocks it, use the
manual-export route instead.
"""
import logging
from datetime import date, timedelta
from io import StringIO

import pandas as pd

import config
import net
import processor

log = logging.getLogger(__name__)

PAGINATION_SELECTORS = [
    "button[aria-label='Go to next page']",
    "button[aria-label='Next page']",
    "button[title='Next page']",
]
MAX_PAGES = 100
TABLE_WITH_ROWS = "table:has-text('Date') td"


def build_url(segment: str, start: date, end: date, interval: str = "15min") -> str:
    seg = config.SEGMENTS[segment]
    query = seg["query"].format(start=start, end=end, interval=config.INTERVALS[interval])
    return f"{seg['page_url']}?{query}"


def _flatten_columns(columns) -> list[str]:
    out = []
    for col in columns:
        if isinstance(col, tuple):
            col = " ".join(str(p) for p in col if "Unnamed" not in str(p))
        out.append(str(col))
    return out


def _is_data_table(headers: set[str], interval: str) -> bool:
    if interval == "hourly":
        return "mcp" in headers and "date" in headers and bool(headers & {"hour", "time_block"})
    return {"time_block", "mcp"} <= headers


def data_tables(html: str, interval: str = "15min") -> list[pd.DataFrame]:
    """All tables in the page that look like the data table (not the summary table)."""
    try:
        tables = pd.read_html(StringIO(html))
    except ValueError:  # "No tables found"
        return []
    found = []
    for table in tables:
        table = table.copy()
        table.columns = _flatten_columns(table.columns)
        if _is_data_table({processor._map_header(c) for c in table.columns}, interval):
            found.append(table)
    return found


def to_frame(html_pages: list[str], segment: str, start: date, end: date, interval: str = "15min") -> pd.DataFrame:
    """Parse one or more page snapshots into a clean frame limited to [start, end]."""
    tables = [t for html in html_pages for t in data_tables(html, interval)]
    if not tables:
        raise RuntimeError(f"no {interval} data table found on the page")
    normalise = processor.normalise_hourly if interval == "hourly" else processor.normalise
    try:
        clean = normalise(pd.concat(tables, ignore_index=True), segment)
    except ValueError as exc:
        raise RuntimeError(f"could not read the table: {exc}") from exc
    days = clean["date"].dt.date
    in_range = clean[(days >= start) & (days <= end)]
    if in_range.empty:
        seen = f"{days.min()}..{days.max()}"
        raise RuntimeError(
            f"the page returned dates {seen}, none inside {start}..{end} "
            "(the date parameters may have been ignored)"
        )
    return in_range.reset_index(drop=True)


def _days(df: pd.DataFrame) -> int:
    return int(df["date"].nunique())


class PageClient:
    """Fetches one date range at a time. transport: auto | requests | browser."""

    def __init__(self, session, transport: str = "auto", headless: bool = True):
        self.session = session
        self.transport = transport
        self.headless = headless
        self._pw = self._browser = self._page = None

    # -- lifecycle -----------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self) -> None:
        for closer in (getattr(self._browser, "close", None), getattr(self._pw, "stop", None)):
            if closer:
                try:
                    closer()
                except Exception:
                    pass
        self._pw = self._browser = self._page = None

    # -- public --------------------------------------------------------------
    def fetch(self, segment: str, start: date, end: date, interval: str = "15min") -> pd.DataFrame:
        url = build_url(segment, start, end, interval)
        expected = (end - start).days + 1

        if self.transport in ("auto", "requests"):
            try:
                df = to_frame([self._requests_html(url)], segment, start, end, interval)
            except RuntimeError as exc:
                if self.transport == "requests":
                    raise RuntimeError(f"{exc}. Try: python main.py --transport browser") from exc
                log.info("Plain request not enough (%s); switching to a browser for the rest of the run", exc)
                self.transport = "browser"
            else:
                finished_window = end < date.today() - timedelta(days=2)
                if self.transport == "auto" and _days(df) < expected and finished_window:
                    log.info("Plain request gave %d of %d days; trying the browser (the table may be paginated)",
                             _days(df), expected)
                    try:
                        better = to_frame(self._browser_pages(url), segment, start, end, interval)
                        if _days(better) > _days(df):
                            self.transport = "browser"
                            return better
                    except RuntimeError as exc:
                        log.warning("Browser attempt failed too (%s); keeping the plain result", exc)
                return df

        return to_frame(self._browser_pages(url), segment, start, end, interval)

    # -- transports ----------------------------------------------------------
    def _requests_html(self, url: str) -> str:
        resp = net.request_with_retry(
            self.session, "GET", url,
            headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"},
        )
        return resp.text

    def _start_browser(self) -> None:
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            channel=config.BROWSER_CHANNEL,
            executable_path=config.BROWSER_EXECUTABLE,
        )
        context = self._browser.new_context(user_agent=config.USER_AGENT)
        self._page = context.new_page()

    def _browser_pages(self, url: str) -> list[str]:
        """Load the URL in the browser and return the HTML of every table page (follows pagination)."""
        self._start_browser()
        page = self._page
        timeout = config.REQUEST_TIMEOUT_SECONDS * 1000
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        try:
            page.wait_for_selector(TABLE_WITH_ROWS, timeout=timeout)
        except Exception as exc:
            self._save_debug(page)
            raise RuntimeError(
                "the data table did not appear in the browser (blocked, slow, or the layout changed); "
                "see discovery_output/last_failure.png"
            ) from exc

        pages = [page.content()]
        for _ in range(MAX_PAGES):
            button = None
            for selector in PAGINATION_SELECTORS:
                locator = page.locator(selector)
                if locator.count() and locator.first.is_enabled():
                    button = locator.first
                    break
            if button is None:
                break
            button.click()
            page.wait_for_timeout(700)
            html = page.content()
            if html == pages[-1]:
                break
            pages.append(html)
        return pages

    def _save_debug(self, page) -> None:
        try:
            out = config.BASE_DIR / "discovery_output"
            out.mkdir(exist_ok=True)
            (out / "last_failure.html").write_text(page.content(), encoding="utf-8")
            page.screenshot(path=str(out / "last_failure.png"), full_page=True)
        except Exception:
            pass
