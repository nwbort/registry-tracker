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

## Historical changes (`announcement_history.py`)

The tracker above only sees changes that happen *while it is running* — it diffs today's
registrar against yesterday's, so its history starts the day it was first run.
`announcement_history.py` goes the other way: it mines the ASX **announcement archive**,
which reaches back to 1998, for the notices companies must lodge under Listing Rule 3.15.1
when they change share registry. That reconstructs a registrar history for a ticker from
long before this repo existed.

```bash
python announcement_history.py scan --codes ECS,BHP   # index announcements
python announcement_history.py scan                   # whole market
python announcement_history.py resolve                # read the candidate PDFs
python announcement_history.py changes                # resolved switches
python announcement_history.py timeline ECS           # one ticker
python announcement_history.py export out.csv
```

### Which endpoint serves history

The modern `asx.api.markitdigital.com/.../companies/{code}/announcements` JSON endpoint is
**not** usable: it is hard-capped at the 5 most recent announcements and silently ignores
`pageSize`, `page`, `count` and every date parameter. The market-wide
`/markets/announcements` feed pages properly but only 25 at a time with no date filter, so
walking it back years is hopeless.

The one path that serves history is the legacy
`www.asx.com.au/asx/v2/statistics/announcements.do`, and only in per-company-per-year
slices (`by=asxCode&asxCode=ECS&timeframe=Y&year=2026`). `timeframe=D/W/M/A` and
`by=date`/`by=announcementType` all return nothing, so there is no market-wide historical
search — the crawl is necessarily company × year. Unlike the `asx/1/company/...` paths,
this one is not behind Imperva.

Two useful side effects: the page reports **ticker code changes** ("ECS is not the current
code for this company. The new code is AYG"), and it returns one table per code a company
has used, so a rename does not split the history.

Scope is about 30,000 company-years for the full market — the crawl starts at each
company's listing date rather than 1998, which prunes it by nearly half. That is roughly
half an hour at the default pacing.

### Why the PDFs get read

The headline alone cannot tell you what happened, because two very different events share
almost the same wording:

| Headline | What it means |
| --- | --- |
| `Change of Share Registry` | the provider actually changed |
| `Change of Share Registry Address` | the *same* registrar moved office |
| `Details of Share Registry address` | same registrar, new address |

Address notices outnumber real switches roughly three to one, and they arrive in clusters —
when one registrar relocates, every client lodges on the same day. Only the PDF body names
the outgoing and incoming registrar, so `resolve` fetches it.

The PDF sits behind a terms-of-access interstitial: `displayAnnouncement.do?display=pdf&idsId=N`
returns an HTML page whose hidden `pdfURL` field holds the real
`announcements.asx.com.au/asxpdf/...` link. Two requests per document. Most of these PDFs
set the "no text extraction" metadata flag; pdfminer notes it and proceeds.

`resolve_registrars()` extracts the pair by trying, in decreasing order of confidence:
an explicit `from X to Y`; an `X will cease` paired with an appointment phrase; either of
those plus the only other registrar named in the document; or, failing all that, exactly
two distinct registrar brands in order of appearance. The `method` column records which
rule fired, so a `two_brands` guess is never mistaken for a stated `from_to`.

Historical registrars that no longer appear in the live ASX feed (ASX Perpetual Registrars,
Registries Limited, Security Transfer, White Outsourcing …) are added on top of
`registry_tracker.canonical_registry`, so a 2004 announcement resolves to the same
canonical names as a 2026 one.

### Worked example

ECS Botanics is the case this was built from — its 30 July 2026 notice was the prompt. The
archive turns up two switches, neither of which the daily tracker was alive to see:

```
CODE   DATE        FROM                TO             METHOD
ECS    2026-07-30  Automic             Xcend          cease_plus_one
ECS    2023-07-24  Computershare       Automic        from_to
```

### Terms of use

The ASX PDF interstitial draws an explicit distinction between *private or personal
investment* use, which is free, and **commercial or professional use, which it says
requires ASX's express written authority**. Indexing headlines is one thing; systematically
downloading the PDF archive is the part that engages this. Worth reading
[the general conditions](https://www.asx.com.au/about/terms-use.htm) before pointing
`resolve` at the whole market.

## Notes

- The `access_token` in the directory URL is the public one embedded in the ASX website's
  own JavaScript. If the directory call starts 401ing, reload an ASX company page and pull
  the current token from the network tab.
- `announcement_history.py` records every `(code, year)` it has fetched in a `scanned`
  table, so an interrupted crawl resumes where it stopped. The current year is always
  re-scanned, since it is still filling up.
