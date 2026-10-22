"""CLI entry point.

Examples
  python main.py --start 2022-04-01 --end 2022-04-30 --no-weather   # test ONE month first
  python main.py                               # all months from 2022-04-01 (page URL with dates)
  python main.py --source files --input-dir exports
  python main.py --start 2022-04-01 --end today --no-weather
  python main.py --hourly --existing my_15min.csv --end 2022-04-30   # test hourly + weather for ONE month
  python main.py --hourly --existing my_15min.csv                    # full hourly merge to today
  python main.py --discover                    # advanced: dump the page's network calls
"""
import argparse
import contextlib
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

import api_client
import config
import export_excel
import net
import processor
import scraper
import weather

log = logging.getLogger("main")


def parse_date(text: str) -> date:
    return date.today() if text == "today" else datetime.strptime(text, "%Y-%m-%d").date()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(config.LOG_FILE, encoding="utf-8")],
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="IEX monthly data collector")
    ap.add_argument("--start", default=config.START_DATE.isoformat())
    ap.add_argument("--end", default="today")
    ap.add_argument("--source", choices=["auto", "api", "files"], default="auto")
    ap.add_argument("--transport", choices=["auto", "requests", "browser"], default="auto",
                    help="how to replay the learned request (auto = whatever --learn decided)")
    ap.add_argument("--input-dir", default=str(config.EXPORTS_DIR))
    ap.add_argument("--segment", default=None, help="only this segment (default: all enabled)")
    ap.add_argument("--no-weather", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignore cached months")
    ap.add_argument("--output", default=str(config.OUTPUT_XLSX))
    ap.add_argument("--show-browser", action="store_true", help="when a browser is needed, show its window")
    ap.add_argument("--hourly", action="store_true",
                    help="scrape IEX hourly data (+ hourly weather) and merge it into --existing")
    ap.add_argument("--existing", default=None, help="your 15-minute IEX CSV to merge into (with --hourly)")
    ap.add_argument("--combined-output", default=str(config.COMBINED_OUTPUT_CSV),
                    help="where to write the merged CSV (with --hourly)")
    ap.add_argument("--extend-15min", action="store_true",
                    help="with --hourly: also fetch the 15-minute rows missing after the end of --existing")
    ap.add_argument("--hourly-interval-code", default=None,
                    help="the page's interval= value for Hourly (default: config.INTERVALS['hourly'])")
    ap.add_argument("--weather-sources", default="open-meteo,nasa",
                    help="with --hourly: comma list of open-meteo, nasa (--no-weather skips both)")
    ap.add_argument("--learn", action="store_true", help="fallback: learn the page's request from a browser session")
    ap.add_argument("--learn-start", default="2022-04-01")
    ap.add_argument("--learn-end", default="2022-04-30")
    ap.add_argument("--learn-seconds", type=int, default=240)
    ap.add_argument("--discover", action="store_true", help="record the page's network calls")
    ap.add_argument("--discover-seconds", type=int, default=60)
    ap.add_argument("--headless", action="store_true", help="discovery without a visible browser")
    args = ap.parse_args()
    setup_logging()

    segments = [args.segment] if args.segment else [s for s, c in config.SEGMENTS.items() if c["enabled"]]
    start, end = parse_date(args.start), parse_date(args.end)

    if args.learn:
        import learn
        learn.run(segments[0], parse_date(args.learn_start), parse_date(args.learn_end), args.learn_seconds)
        return 0
    if args.discover:
        import discover
        discover.run(segments[0], args.discover_seconds, headed=not args.headless)
        return 0
    if args.hourly:
        import hourly_job
        return hourly_job.run(args, segments, start, end)

    source = args.source
    if source == "auto":
        source = "api" if all(api_client.api_configured(s) for s in segments) else "files"
        log.info("Source: %s", source)

    use_browser = args.transport == "browser" or (
        args.transport == "auto" and source == "api" and api_client.learned_transport(segments[0]) == "browser"
    )

    session = net.make_session()
    frames, failures = [], []
    try:
        with contextlib.ExitStack() as stack:
            if source == "api" and api_client.uses_page_mode(segments[0]):
                import page_client
                client = page_client.PageClient(session, args.transport, headless=not args.show_browser)
                api_client.set_page_client(stack.enter_context(client))
            elif source == "api" and use_browser:
                import browser_fetch
                page_url = config.SEGMENTS[segments[0]]["page_url"]
                api_client.set_browser_fetcher(stack.enter_context(browser_fetch.BrowserFetcher(page_url)))
            for seg in segments:
                if source == "api":
                    df, fails = scraper.collect_segment(session, seg, start, end, args.refresh)
                else:
                    folder = Path(args.input_dir)
                    folder.mkdir(exist_ok=True)
                    df, fails = scraper.load_export_folder(folder, seg, start, end)
                frames.append(df)
                failures += fails
    except api_client.NotConfigured as exc:
        log.error("%s", exc)
        return 2

    non_empty = [f for f in frames if not f.empty]
    if not non_empty:
        log.error("No data collected. Run `python main.py --learn` once, or put exported IEX files in %s.", args.input_dir)
        return 2
    data = pd.concat(non_empty, ignore_index=True)

    monthly = processor.monthly_summary(data)
    daily = processor.daily_summary(data)

    weather_monthly = pd.DataFrame()
    if config.WEATHER_ENABLED and not args.no_weather:
        w_daily, w_fails = weather.fetch_weather(session, start, end)
        failures += w_fails
        weather_monthly = weather.monthly_weather(w_daily)

    notes = [
        "Source: Indian Energy Exchange (https://www.iexindia.com), Day Ahead Market snapshot, 15-minute blocks.",
        "MCP is the unconstrained Market Clearing Price in Rs/MWh. MWh = sum of block MW x 0.25 h.",
        "Weather: Open-Meteo archive (ERA5-based reanalysis), averaged across the configured cities.",
        "complete_month = False means the month is partial (data not yet published or missing days).",
        "IEX terms: only correct quotation with attribution is permitted. Check https://www.iexindia.com/terms-of-uses.",
    ]
    export_excel.export_workbook(Path(args.output), monthly, daily, weather_monthly, notes)

    print(f"\nMonths collected: {len(monthly)}  |  rows: {len(data):,}  |  output: {args.output}")
    partial = monthly.loc[~monthly["complete_month"], ["segment", "month", "days"]]
    if not partial.empty:
        print("Partial months:", ", ".join(f"{r.segment} {r.month:%Y-%m} ({r.days}d)" for r in partial.itertuples()))
    if failures:
        print("Failures:")
        for label, msg in failures:
            print(f"  - {label}: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
