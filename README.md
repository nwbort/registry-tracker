# ASX Share Registry Tracker

Tracks the **share registry (registrar)** of every ASX listed company. It records
**changes only** — a company's registry is written once and then left alone until it
actually changes, so a day where nothing moves adds no rows at all.

## How it gets the data

The ASX company page (e.g. `https://www.asx.com.au/markets/company/14D`) renders the
registry block client-side — the raw HTML has nothing in it, and `www.asx.com.au` sits
behind Imperva, which rejects the old `asx/1/company/...` and `ASXListedCompanies.csv`
paths. The page actually calls two public JSON/CSV endpoints, which this tracker hits
directly instead of driving a browser:

| Purpose | Endpoint |
| --- | --- |
| Full listed-company directory (CSV) | `asx.api.markitdigital.com/asx-research/1.0/companies/directory/file` |
| Per-company details incl. registry | `cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/companies/{CODE}/about` |

Neither needs authentication. The registry lives in `data.addressShareRegistry`
(`attention` = registrar name, plus address and phone).

## Install

```bash
pip install -r requirements.txt
```

## Usage

Scrape the whole market (~1,840 companies in about a minute):

```bash
python registry_tracker.py fetch
```

```bash
python registry_tracker.py summary
```

```bash
python registry_tracker.py changes
```

```bash
python registry_tracker.py history 14D
```

```bash
python registry_tracker.py runs
```

```bash
python registry_tracker.py export data/asx_registries.csv
```

`fetch` also takes `--codes 14D,BHP`, `--limit N` for smoke tests, `--run-date` to override
the run date, and `--db` (global) to point at another SQLite file.

### Throughput

`--workers` (default 12) sets concurrency, but **`--delay` is the real rate limit** — it is
a single throttle shared across all threads, so raising workers without lowering delay
changes nothing. The default 0.02s caps the run at ~50 requests/sec, which does the full
market in about a minute. The connection pool is sized to match `--workers`, so raising it
does not cause urllib3 to churn connections.

The API tolerated 100+ req/sec in testing with no errors, so you can go faster if you need
to — but this is a free public endpoint, and the GitHub Actions job shares an IP range with
a lot of other traffic. The defaults are deliberately unremarkable.

## What gets recorded

Each company has exactly one `is_current` row in `registry_state`. On every run:

- **unchanged** → nothing is written at all
- **registry details differ** → the current row is closed (`closed_on` = the previous run's
  date, the last date the old registry was actually observed) and a new row opened
- **the registrar itself changed** → additionally logged to `registry_change`, which is what
  alerts fire on. An address or punctuation edit updates state without raising an alert.
- **blank registry upstream where we previously had one** → treated as a data gap and
  ignored, not a company leaving its registrar
- **code dropped off the ASX directory** → its state row is closed as `delisted` (full-market
  runs only)

The `run` table records every scrape with its tallies, so a quiet day is still provably a
day the tracker ran and found nothing — as distinct from a day it didn't run.

### Market cap

Market cap is price-derived, so it moves every day. Refreshing it on every run would rewrite
the whole company table daily and change the exported CSV daily, burying real registry
changes in noise. It is therefore only written:

1. **once per calendar month** — on the first run of a new month
2. **for new listings** — a code appearing in the directory for the first time
3. **for any company whose registry changed** — so the row being published is internally
   consistent

`market_cap_as_at` records when each figure was actually taken, and `summary` prints it, so
a stale cap is never mistaken for a live one. Force one with `fetch --refresh-caps`.

The monthly trigger keys off when a refresh was last *attempted* (`run.caps_refreshed`), not
when a value last changed — otherwise a month in which no cap happened to move would
re-trigger the refresh every day.

### Schema

- `company` — code, name, GICS industry group, listing date, market cap + `market_cap_as_at`,
  address, website
- `registry_state` — effective-dated registry per company: `first_seen`, `closed_on`,
  `is_current`, `closed_reason`, plus raw/canonical name, address, phone
- `registry_change` — code, detected_on, old/new registry (canonical and raw)
- `run` — one row per scrape with counts of new/changed/unchanged/gaps/errors/delisted
- `fetch_error` — only written when a fetch fails; empty on a healthy run

Since only deltas are stored, the database stays around 800 KB indefinitely rather than
growing ~130 MB/year.

## Registry name normalisation

The ASX `attention` field is free text, so the same registrar appears several ways
(`COMPUTERSHARE INVESTOR SERVICES PTY LIMITED`, `... PTY. LIMITED`, `... LIMITED`).
`canonical_registry()` maps these to a canonical brand via an ordered regex list, and falls
back to a tidied title-cased version for anything unrecognised — an unknown registrar is
never silently dropped. Change detection compares canonical names, so a punctuation tweak
upstream does not fire a false change.

## Running it daily on GitHub Actions

[`.github/workflows/daily-registry-scrape.yml`](.github/workflows/daily-registry-scrape.yml)
runs the full scrape at 20:10 UTC (6:10am AEST / 7:10am AEDT), and is also runnable on
demand via **Run workflow** (with an optional `limit` for a cheap test).

Because runners are ephemeral and change detection needs the previous state, the database is
carried between runs by `actions/cache`, with a 90-day artifact as a fallback if the cache
is evicted. `data/asx_registries.csv` is committed back to the repo, but only when it
actually differs — so the commit log is a log of real registry changes. The SQLite file is
gitignored.

If all prior state is somehow lost, the run records a fresh baseline and detects no changes.
It never produces false positives.

When a company switches registrar, the workflow writes it to the job summary and **opens a
GitHub issue**. This needs `contents: write` and `issues: write`, both declared in the
workflow.

## Results as at 2026-08-07 (1,840 companies)

| Registry | Companies | Share | Market cap | Cap share |
| --- | ---: | ---: | ---: | ---: |
| Automic | 702 | 38.2% | $155.2bn | 4.2% |
| Computershare | 575 | 31.2% | $2,042.4bn | 55.2% |
| MUFG Corporate Markets | 307 | 16.7% | $1,283.3bn | 34.7% |
| Boardroom | 184 | 10.0% | $211.5bn | 5.7% |
| Xcend | 55 | 3.0% | $6.0bn | 0.2% |
| Registry Direct | 8 | 0.4% | $0.1bn | 0.0% |
| Tricor | 1 | 0.1% | $0.1bn | 0.0% |
| (none listed) | 8 | 0.4% | $0.7bn | 0.0% |

Automic dominates by company count (small caps), Computershare by market cap. The 8 blanks
are gaps in the ASX feed itself — those companies return an empty `attention` field
upstream — not parse failures.

## Notes

- The `access_token` in the directory URL is the public one embedded in the ASX website's
  own JavaScript. If the directory call starts 401ing, reload an ASX company page and pull
  the current token from the network tab.
