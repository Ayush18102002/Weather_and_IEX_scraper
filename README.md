# Weather and IEX scraper

Collects Indian Energy Exchange (IEX) Day-Ahead Market 15-minute block data from
1 April 2022 (the earliest date IEX publishes 15-minute blocks), aggregates it
month by month, adds free Open-Meteo weather, and writes `iex_monthly_data.xlsx`.

## How it gets the data
The IEX Market Snapshot page takes its date range from the URL:

```
https://www.iexindia.com/market-data/day-ahead-market/market-snapshot
  ?interval=ONE_FOURTH_HOUR&dp=SELECT_RANGE&showGraph=false&toDate=30-04-2022&fromDate=01-04-2022
```
For every month the script opens that URL with the month's first and last date and reads the table
from the page. It tries a plain request first; if the site returns no table for those dates, or the
table is paginated, it switches to a headless browser by itself (and clicks through the pages).
Finished months are cached in `raw_data/`, so a re-run only downloads what is missing.

## Setup
Python 3.10+.
```
pip install -r requirements.txt
playwright install chromium
```
No Playwright download? Set `BROWSER_CHANNEL = "chrome"` (or `"msedge"`) in `config.py` to use the browser
you already have installed.

## Run
Test ONE month first:
```
python main.py --start 2022-04-01 --end 2022-04-30 --no-weather
```
Check `iex_monthly_data.xlsx` (the DAM Daily sheet should have 30 days). Then everything:
```
python main.py
```
Other options:
```
python main.py --transport browser        # always use a browser
python main.py --show-browser             # see the browser window while it works
python main.py --no-weather               # skip the weather download
python main.py --refresh                  # ignore cached months
python -m pytest -q tests                 # offline tests
```
A month that comes back with fewer days than expected is reported, not cached, and retried on the next run.

## If it does not work
- **"the data table did not appear"**: open `discovery_output/last_failure.png` to see what the browser saw.
  Try `--show-browser`. If the site is blocking automated access, do not try to get around it; use the manual route below.
- **Manual route**: on the IEX page set Interval = 15-Min-Block, choose a date range, click **Export**, and save the
  files in `exports/`. Then run `python main.py --source files`.
- Advanced fallbacks: `python main.py --discover` (records the page's network calls) and `python main.py --learn`.

## Hourly IEX data + hourly weather, merged into your 15-minute CSV
Test one month first, then everything (use the path to your own file):
```
python main.py --hourly --existing "C:\path\combined_dataset_from_IEX.csv" --start 2022-04-01 --end 2022-04-30
python main.py --hourly --existing "C:\path\combined_dataset_from_IEX.csv" --extend-15min
```
It writes `combined_dataset_with_hourly.csv`: your file's rows exactly as they were, plus new columns on the right.
Also written: `iex_hourly_data.csv` (the scraped hourly rows) and `iex_hourly_weather_by_city.csv`.

New columns
- `hour_start` and `h_purchase_bid_mw`, `h_sell_bid_mw`, `h_mcv_mw`, `h_final_scheduled_mw`, `h_mcp`: IEX's hourly
  figures, repeated on the four 15-minute rows of that hour.
- `om_*`: Open-Meteo (temp C, humidity %, wind m/s, cloud %, precipitation mm, solar W/m2).
- `np_*`: NASA POWER (temp C, humidity %, wind m/s, solar W/m2; NASA reports Wh/m2 per hour, numerically the hourly mean W/m2).
- Weather values are the average of the cities in `config.WEATHER_CITIES`. Missing values stay blank, never filled in.

Time alignment: both weather sources are requested in UTC and converted to Indian-time hours (IST = UTC+5:30),
because NASA's default hourly clock is solar time, not IST. See the top of `weather_hourly.py` for the exact rule.

`--extend-15min` also downloads 15-minute rows after the last row of your file (up to today) and appends them in your
file's style. Without it, hourly rows after the end of your file are only saved in `iex_hourly_data.csv`.

Three things the first run checks and prints; read them:
1. **Hourly interval code.** `config.INTERVALS["hourly"] = "ONE_HOUR"` is a guess. On the IEX page choose
   Interval = Hourly, copy the value after `interval=` from the address bar, and pass it with `--hourly-interval-code`.
   If wrong you get "not hourly" or "no hourly data table".
2. **Cross-check.** It compares IEX's hourly figures with the average of the four 15-minute blocks of your file.
   100% means the hourly page is simply that average.
3. **Solar peak check.** It should say the average solar peak is in the hour starting 11:00 or 12:00 IST.
   If NASA says UNEXPECTED, change `NASA_STAMP` in `config.py` to `"period_end"`.

Weather data trails real time by about 6 days, so the newest rows have blank weather.

## Workbook sheets
- **Monthly Summary** - MCP min/max/avg (Rs/MWh and Rs/kWh), volume-weighted MCP, purchase bid / sell bid / MCV /
  final scheduled volume in MWh, days covered, `complete_month`, plus weather columns.
- **DAM Daily** - the same metrics per day.
- **Weather Monthly** - per-city and averaged monthly weather.
- **MCP by Month** - table and line chart.
- **Notes** - source and definitions.

MWh = sum of the block MW values x 0.25 h (matches the "Total (MWh)" line on IEX's page).

## Notes
- The IEX site runs bot detection. The script behaves like a normal visitor: honest User-Agent
  (`config.USER_AGENT`), a 3 s pause between months, no attempt to evade blocking.
- IEX has no robots.txt (404), but its page states only correct quotation with attribution is permitted;
  see https://www.iexindia.com/terms-of-uses. Keep the request rate low and attribute IEX.
- Only the DAM page is set up. For another segment add an entry in `config.SEGMENTS` with its `page_url` and `query`.
- `learned_api.json` (only created by `--learn`) may contain request headers. Do not share it.
- Open-Meteo's free tier is for non-commercial use and trails real time by a few days.
