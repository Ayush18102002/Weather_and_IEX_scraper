"""`python main.py --hourly --existing <your 15-minute CSV>`

1. scrapes IEX hourly data from `--start` (default 2022-04-01) to today (month by month, cached),
2. optionally scrapes the 15-minute blocks that are missing after the last row of your file (--extend-15min),
3. downloads hourly weather (Open-Meteo, NASA POWER) unless --no-weather,
4. writes your dataset back unchanged plus new columns: hour_start, h_* (IEX hourly), om_* / np_* (weather).
"""
import logging
from pathlib import Path

import pandas as pd

import api_client
import combine
import config
import net
import page_client
import scraper
import weather_hourly

log = logging.getLogger(__name__)


def run(args, segments: list[str], start, end) -> int:
    seg = segments[0]
    if not api_client.uses_page_mode(seg):
        log.error("Hourly download needs page mode: set SEGMENTS['%s']['query'] in config.py", seg)
        return 2
    if args.hourly_interval_code:
        config.INTERVALS["hourly"] = args.hourly_interval_code
    if not args.existing:
        log.error("--hourly needs --existing <path to your 15-minute CSV>")
        return 2

    existing = combine.load_existing(args.existing)
    log.info("Loaded %s rows from %s", f"{len(existing):,}", args.existing)
    session = net.make_session()
    failures: list[tuple[str, str]] = []

    with page_client.PageClient(session, args.transport, headless=not args.show_browser) as client:
        api_client.set_page_client(client)
        hourly, fails = scraper.collect_segment(session, seg, start, end, args.refresh, interval="hourly")
        failures += fails
        if hourly.empty:
            log.error("No hourly data collected.")
            _print_failures(failures)
            return 2

        added = 0
        if args.extend_15min:
            last = combine.parse_timestamps(existing["timestamp"]).max()
            ext_start = last.date() + pd.Timedelta(days=1).to_pytimedelta() if (last.hour, last.minute) == (23, 45) else last.date()
            if ext_start <= end:
                log.info("Extending the 15-minute data from %s", ext_start)
                new15, fails = scraper.collect_segment(session, seg, ext_start, end, args.refresh)
                failures += fails
                if not new15.empty:
                    existing, added = combine.append_quarter_rows(existing, new15)
    api_client.set_page_client(None)

    combined, stats = combine.attach_hourly(existing, hourly)
    check = combine.crosscheck(existing, hourly)

    weather_report, weather_stats = [], {}
    if not args.no_weather:
        sources = [s.strip() for s in args.weather_sources.split(",") if s.strip()]
        wide, by_city, w_fails, weather_report = weather_hourly.build(session, sources, start, end, args.refresh)
        failures += w_fails
        if not wide.empty:
            combined, weather_stats = combine.attach_weather(combined, wide)
            by_city.to_csv(config.BASE_DIR / "iex_hourly_weather_by_city.csv", index=False)

    out_path = Path(args.combined_output)
    combine.write_csv(out_path, combined)
    hourly_out = hourly.copy()
    hourly_out["date"] = hourly_out["date"].dt.strftime("%Y-%m-%d")
    hourly_out.to_csv(config.HOURLY_OUTPUT_CSV, index=False)

    _print_report(out_path, combined, stats, check, added, weather_stats, weather_report, failures)
    return 0


def _print_failures(failures) -> None:
    if failures:
        print("Failures:")
        for label, msg in failures:
            print(f"  - {label}: {msg}")


def _print_report(out_path, combined, stats, check, added, weather_stats, weather_report, failures) -> None:
    print(f"\nWrote {out_path}  ({len(combined):,} rows x {combined.shape[1]} columns)")
    print(f"Hourly IEX file: {config.HOURLY_OUTPUT_CSV}  ({stats['hourly_rows']:,} hours)")
    if added:
        print(f"Appended {added:,} new 15-minute rows after the end of your file")
    print(f"15-minute rows with hourly data: {stats['matched_rows']:,} of {stats['rows']:,}"
          f"  (without: {stats['unmatched_rows']:,})")
    if stats["hourly_not_in_existing"]:
        print(f"Hourly hours with no 15-minute rows in the file: {stats['hourly_not_in_existing']:,} "
              "(use --extend-15min to fetch the missing 15-minute data)")
    mcp = check["mcp"]
    if mcp["hours_compared"]:
        print(f"\nCross-check, IEX hourly vs average of its four 15-minute blocks ({mcp['hours_compared']:,} hours):")
        for col, res in check.items():
            print(f"  {col:20s} within tolerance in {res['share_close']:.1%} of hours, max difference {res['max_abs_diff']:.2f}")
    if weather_stats:
        print("\nWeather columns added (rows filled): " + ", ".join(f"{k}={v:,}" for k, v in weather_stats.items()))
    for line in weather_report:
        print("  " + line)
    _print_failures(failures)
