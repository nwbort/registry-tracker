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
python announcement_history.py backfill               # name the side a notice left out
python announcement_history.py changes                # resolved switches
python announcement_history.py tickers                # codes companies have traded under
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

The headline therefore decides *ordering*, not eligibility: `resolve` opens address notices
too. On a 60-notice sample, **6.7% of them were real switches** — companies do lodge a
genuine change of provider under "Details of Share Registry address". `--skip-address`
buys back about 35% of the fetches if that trade is not worth it.

### Headlines that do not say "registry"

Ordering is all the headline decides *once an announcement is indexed*. Whether it is
indexed at all is a hard gate: an announcement the headline rules do not match is never
stored, so no PDF is ever opened and the change cannot be recovered later. Two shapes get
past a plain `registr` match:

- **`transfer agent`** — the North-American name for a registrar. Companies with a second
  listing in Toronto or New York use it in place of, or alongside, "share registry".
- **`capital market(s) change/update`** — a company switching registrar on two exchanges at
  once headlines the notice for the market rather than for the document. AuMEGA Metals
  (AAM) moved from Automic to Computershare on 8 December 2025 under *"AuMEGA Metals
  Announces Capital Market Changes"*; the words "registry and transfer agent" appear only
  in the body, above a clean `From: … To: …` table.

Only the `change`/`update` shape is admitted, not bare "capital markets" — measured over a
108,618-headline sample of the archive, `capital market` alone matches investor days and
debt-adviser appointments (`ARR engages BMO Capital Markets as Financial Adviser`), while
the narrowed pattern and `transfer agent` between them matched nothing that was not a
registry notice. Every pattern here is paid for one PDF at a time, so one that fires on the
wrong thing costs real requests.

Widening the net also pulls in one thing that has to be pushed back out: **a registrar
buying another registrar**. `Computershare acquires US Transfer Agent` is lodged under
Computershare's own ticker, and left in it reads as three registry changes at CPU. That is
the same case as the `sale of registry business` patterns `_NOT_REGISTRY` already carried
for the 2005 ASX/Perpetual sale, so acquisitions join them there — otherwise
`backfill --include-other` would go looking for a registrar to pair each one with.

Because `scanned` records that a company-year was fetched and not what the headline rules
made of it, widening those rules leaves every already-scanned year holding the old verdict.
`scan --rescan` re-indexes them. Note it can only *add* — `INSERT OR REPLACE` never deletes,
so announcements a newly **narrowed** rule rejects have to be removed from the table
explicitly.

The PDF sits behind a terms-of-access interstitial: `displayAnnouncement.do?display=pdf&idsId=N`
returns an HTML page whose hidden `pdfURL` field holds the real
`announcements.asx.com.au/asxpdf/...` link. Two requests per document. Most of these PDFs
set the "no text extraction" metadata flag; pdfminer notes it and proceeds.

`resolve_registrars()` extracts the pair by trying, in decreasing order of confidence:
an explicit `from X to Y`; an `X will cease` paired with an appointment phrase; either of
those plus the only other registrar named in the document; or, failing all that, exactly
two distinct registrar brands in order of appearance. The `method` column records which
rule fired, so a `two_brands` guess is never mistaken for a stated `from_to`.

When the document names exactly one registrar, which end of the change it sits on is
decided by the sentence around it. The default is the incoming one — a company announcing a
move prints the new registrar's contact details and routinely never says who it left — but
a `cease` phrase naming that same registrar flips it to the outgoing side (`one_brand_cease`).
Reading a "X will cease to act as registrar" notice the default way would invert the change.

Some names are only a registrar in context. `Boardroom` is also an ordinary word — Resource
Star's 2009 notice of AGM names no registrar at all, but held the meeting at *"The Boardroom,
Nissen Kestel Harford"* — and `Gadens`, `Gould Ralph` and Steinepreis Paganin (which ran
GG Registry) are the solicitors and accountants half the small-cap market lists in its
corporate directory. These brands, in `_NEEDS_REGISTRY_CONTEXT`, only count when a registry
word sits within 120 characters. "Register" counts, because *"our register is currently
maintained by Boardroom Pty Limited"* is how a notice names the registrar it is leaving;
"registered" does not, because "registered holder" is boilerplate in exactly the meeting
notices the rule exists to exclude. The bare `steinepreis` pattern is gone entirely: in a
corporate directory the solicitors are listed a line above the share registry, so no
context window can separate them.

A notice that names a registrar in full and then uses a short form is read through its own
definitions. Legend Mining's 1 March 2024 notice says the provider *"will change from
Advanced to Automic"* after defining `Advanced Share Registry Limited ("Advanced")`; with
the short forms unresolved, the from/to rule saw no brand on either side and the change
published backwards off the `two_brands` fallback, whose order-of-appearance guess was the
preamble explaining that Automic had bought Advanced. `_defined_terms` requires an
expansion to name exactly one registrar, so a lead-in that sweeps up a second brand defines
nothing rather than defining the alias wrongly.

A stored resolution is otherwise final — the PDF does not change, so re-reading it is a
wasted request. It stops being final when the extraction rules do:
`resolve --reresolve one_brand` re-reads the documents an older rule decided, and
`resolve --reresolve-brand Boardroom` re-reads by what was found rather than by how, which
is what tightening one brand's pattern invalidates. Rules are covered by
`python test_brand_rules.py` — stdlib `unittest`, no network, every fixture a fragment of a
real announcement.

Historical registrars that no longer appear in the live ASX feed (ASX Perpetual Registrars,
Registries Limited, Security Transfer, White Outsourcing …) are added on top of
`registry_tracker.canonical_registry`, so a 2004 announcement resolves to the same
canonical names as a 2026 one.

One rebrand has to be collapsed on top of that, in `_BRAND_ALIASES`: **MPMS is not a
separate registrar.** It is MUFG Pension & Market Services — the group MUFG Corporate
Markets (itself the renamed Link Market Services) was rebranded to. The token barely ever
appears as a company name in these letters; it arrives inside the letterhead domain
`au.investorcentre.mpms.mufg.com`, which nearly every MUFG notice carries. Left as two
brands, a routine "we have moved to MUFG" letter names both spellings and the resolver reads
the rebrand as a company switching registrar — in whichever direction the two names happen
to fall in the text, which is why one of the seven false switches this produced pointed the
opposite way from the other six.

The alias lives in `announcement_history.py` and is applied *after*
`canonical_registry()`, which still maps `mpms` to its own `MPMS` brand. That is
deliberate: the tracker's pattern also matches `mps market` and `market place`, which need
not be MUFG at all, and the daily change alerts key off those names.

### The watermark, and why `ok` lied about it

The ASX stamps **"For personal use only"** down the side of the announcements it serves.
It is a rotated text layer the exchange adds, not part of the lodged document, and pdfminer
reads it back one character per line in an order that is neither the phrase nor a clean
reversal of it:

```
l\n\ny\nn\no\ne\ns\nu\n\nl\n\na\nn\no\ns\nr\ne\np\nr\no\nF
```

On an **image-only scan it is the only text on the page**, which made the scan
indistinguishable from a letter that was read and found to name no registrar: 55 characters
of text, `ok = 1`, no brands. That is why `ok = 0` used to stop in 2007 — not because the
ASX stopped serving scans, but because it started watermarking them. Champion Iron's
12 January 2024 change of registrar sat in that gap, and so did eight other modern scans.
`strip_watermark()` takes the stamp out before anything reads the text, matching it on
*the letters* rather than their order — a run of isolated single characters holding exactly
the letters of the phrase is the stamp whichever way the page was rotated, and is not
something running text produces.

The stamp also lands wherever the page's text order puts it, routinely mid-sentence, where
its 35 characters count against the 120-character windows the from/to, cease and
registry-context rules search in. Re-resolving the 168 documents decided by those
proximity rules changed none of them, so in this archive the damage was confined to the
scans — but the windows are what the rule is there to protect, and the
`test_stamp_does_not_push_a_registrar_out_of_its_context` fixture is a real one it breaks.
The 951 `one_brand` and 534 `from_to` documents have not been re-read; the stamp cannot
hide a name from `_find_brands`, only from the words around it.

### How far back it actually works

Three separate limits, worth keeping apart because they have different fixes:

1. **The announcement index** parses cleanly back to 1998. So the *date* a company changed
   registry is recoverable across the whole archive, always.
2. **Before about 2010 the PDFs are image-only scans.** pdfminer returns nothing usable, so
   the registrars cannot be read *out of the notice* without OCR. They can still be read out
   of the filings either side of it — see `backfill` below, which is what lifts 2005–2009
   off the floor.
3. **A readable PDF may still name one registrar or none.** Plenty of letters say "we have
   appointed X" without naming who they left, and plenty of others describe the change
   without naming either end. That, not scanning, is what caps the 2010s.

Measured over the full market (1,840 codes, 29,921 company-years), across the 942
`provider_change` notices:

| Era | PDFs readable | Pair from the notice alone | Pair after `backfill` |
| --- | --- | ---: | ---: |
| before 2005 | 0 / 26 | 0 / 26 | 0 / 26 |
| 2005–2009 | 44 / 74 | 21 / 74 (28%) | 30 / 74 (**40%**) |
| 2010–2014 | 169 / 169 | 62 / 169 (36%) | 138 / 169 (**81%**) |
| 2015–2019 | 228 / 228 | 124 / 228 (54%) | 215 / 228 (**94%**) |
| 2020+ | 442 / 445 | 404 / 445 (90%) | 431 / 445 (**96%**) |

OCR would now only buy back the pre-2005 rows, where the surrounding filings are scans too
and there is nothing to probe. Everywhere else the cap is limit 3, which is what `backfill`
is for: it lifts 2010–2014 from 36% to 81% and 2015–2019 from 54% to 94%.

Of the 905 pairs that resolve, 534 (59%) come from an explicit "from X to Y" in the text
and 203 (22%) needed `backfill` for one of their two ends or both. 44 rest on the weakest
`two_brands` fallback — two registrar names in one document, taken in order of appearance.
The `method` column keeps all of these apart; do not treat a `two_brands` row as equal
evidence to a `from_to` one.

`resolution.ok = 0` means the PDF was an image-only scan, not that parsing failed.

### Naming the sides the notice leaves out (`backfill`)

A change with one end is not a change: `changes` and `export` both require `old_registry`
and `new_registry`, so a notice that names only where the register went is dropped
entirely. Siren Gold (SNG) is the shape — its 23 March 2026 notice reads *"the share
registry of the Company will be transferred to Computershare"*, gives Computershare's Perth
address, and never mentions Automic at all.

The missing half is usually written down elsewhere in the same company's announcement
stream. Documents that a registry produces, or that have to print its address for
shareholders to act on, name whoever held the register on the day they were lodged — proxy
forms, notices of meeting, letters to shareholders, DRP notices, annual reports. So
`backfill` reads the outgoing registrar out of the newest such document lodged *before* the
notice, and the incoming one out of the oldest lodged well after it. For SNG that is the
proxy form of 16 September 2025, which names Automic, and the change becomes
`Automic → Computershare`.

**A notice that names neither registrar is the same problem twice, not a different one.**
Champion Iron (CIA) lodged *"Change of Share Registrar"* on 12 January 2024 as an image-only
scan — nothing readable in it at all. Its 2023 notice of meeting names Automic and its
FY2024 annual report names Computershare, so the change is `Automic → Computershare` on
evidence of exactly the kind `backfill` already trusts, and its `method` is
`prior_doc+next_doc` with both source ids in `backfilled_from`. Requiring the notice to
have named at least one end wrote off every scan in the archive as unrecoverable without
OCR, along with every readable letter that described a change without naming either side —
84 `provider_change` notices between them, 17 of which turned out to be recoverable
switches.

Both ends are answered from one set of index pages, so a notice missing both costs three
years indexed rather than four. Nothing is written unless *both* ends end up known: a run
that names one side and not the other leaves the row alone, so a later run with more
`--probes` finishes the job rather than inheriting half of it, and `backfilled_from`
keeps meaning "this row is complete, stop probing it".

Four properties are worth keeping in mind:

- **It is evidence, not inference.** If the two ends come out the same — the probed document
  naming the registrar the notice already named, or the two probes naming each other — the
  register did not move and nothing is written. That is what should happen to the address
  notices that reach `provider_change` on a headline typo — `Change of Registry
  **Addresss** Notification` misses the `\baddress\b` filter — so those cost a request and
  produce no false switch. Over the full market 27 notices ended this way.
- **A document naming two registrars is skipped**, not guessed at. Dual-listed companies
  print both their Australian and their overseas registrar.
- **The incoming side is only read 45 days out.** A switch is announced before it takes
  effect ("effective Monday, 30 March"), so a document lodged days after the notice may
  still have come from the outgoing registrar.
- **The candidates are looked up under the company's *current* code.** ASX tickers get
  recycled, and the archive resolves a query code to one entity — whichever holds it latest
  — then serves that entity's whole history under every code it has used. So asking about a
  code its original owner has since given up returns a stranger's filings. Intiger Group
  lodged its 13 June 2017 change under IAM; IAM now belongs to a company that traded as TAU
  in 2017, and the archive answers a 2016–17 IAM query with nothing but TAU. Trustees
  Australia's notice of meeting names Boardroom honestly and as its only registrar, so it
  looks exactly like the evidence being hunted for — and the register it describes belongs
  to someone else. `scan` already stores the code it asked about in `announcement.query_code`,
  which *is* the current code, so `backfill` looks candidates up under that and proves the
  archive is serving the right entity by checking the notice appears on its own year page.

`address_only` notices are excluded by default: they are one-sided for the honest reason —
the registrar moved office, so there was only ever one to name — and pairing each with
whatever came before would manufacture a change out of a non-event. `--include-other` opens
the `registry_other` bucket as well.

**Probing both sides is restricted to `provider_change` headlines**, even under
`--include-other`. With one end stated the notice anchors the probe: the other end has to
differ from a registrar the document itself named. With neither end stated, the only thing
asserting that a change happened at all is the headline — and `Share register update`, the
`registry_other` shape, does not assert it. Two probes either side of one of those would
date whatever the company's next real switch was to the wrong announcement.

Probed documents are recorded in a `probe` table so a second run does not re-fetch them,
and the `method` column carries the provenance: `one_brand+prior_doc` means the incoming
registrar was stated in the notice and the outgoing one came from another filing;
`prior_doc+next_doc` means the notice named neither and both came from other filings. The
`ids_id` of each is in `resolution.backfilled_from`, one per side supplied.

Over the full market this completed **203 `provider_change` notices** — 186 that named one
registrar and 17 that named none — from 701 probed documents. 212 of those probes are what
the answers actually rest on: annual reports supplied 123 of them and notices of meeting
and proxy forms 84, which is why those two shapes lead the pattern; the remaining 5 came
from DRP notices and shareholder letters.

### Worked example

ECS Botanics is the case this was built from — its 30 July 2026 notice was the prompt. The
archive turns up two switches, neither of which the daily tracker was alive to see:

```
CODE   DATE        FROM                TO             METHOD
ECS    2026-07-30  Automic             Xcend          cease_plus_one
ECS    2023-07-24  Computershare       Automic        from_to
```

### Full-market results (1,840 codes, 29,921 company-years)

The whole market scanned in 53.0 minutes at 9.4 requests/sec with **zero failed fetches**,
turning up 2,119 registry-related announcements. All 2,119 PDFs were then fetched; 1,968
yielded text and 151 were image-only scans — 142 of them before June 2007 and 9 modern ones
that only became visible as scans once the watermark stopped counting as text. `backfill`
then opened 701 ordinary filings against the 331 `provider_change` notices that named fewer
than two registrars, completing 203 of them.

99 of those announcements — 53 of them `provider_change` — exist only because the headline
rule learned "share register" alongside "share registry". They were never indexed before,
so no amount of re-resolving would have found them; the market had to be re-scanned.

**905 registrar switches across 741 of the 1,840 companies** — so roughly two companies in
five have changed registry at least once in a window the daily tracker could never have
seen. 814 came from `provider_change` headlines, 47 from `address_only` and 44 from
`registry_other`: the 91 from non-obvious headlines are what the headline-only approach
would have missed.

Net movement over the resolved history:

| Registrar | Gained | Lost | Net |
| --- | ---: | ---: | ---: |
| Automic | 422 | 92 | **+330** |
| Xcend | 45 | 0 | +45 |
| Registries Limited | 14 | 10 | +4 |
| Boardroom | 84 | 83 | +1 |
| Registry Direct | 11 | 11 | 0 |
| MUFG Corporate Markets | 116 | 154 | **−38** |
| Security Transfer Australia | 37 | 100 | **−63** |
| Advanced Share Registry | 68 | 180 | **−112** |
| Computershare | 106 | 272 | **−166** |

This is the picture the daily tracker cannot show: a one-way consolidation into Automic,
with Xcend picking up 45 clients and losing none. Note the direction of travel is not the
same as market share — Computershare still holds the large caps (55% of ASX market cap in
the table above) while shedding small-cap mandates by count. The 203 backfilled rows sharpen
this rather than redirect it — 167 of them are 2010s switches, leaving mostly Computershare
(78) and Security Transfer (48) and arriving mostly at MUFG Corporate Markets (83) and
Automic (62). MUFG is where they change the picture most: without them its `Gained` column
collapses to 33, so the pre-Automic decade of consolidation into what was then Link Market
Services is largely invisible.

Switches are heavily concentrated in time: **180 in 2024** against 42-67 in surrounding
years. That spike is not 180 independent decisions — it is dominated by Automic absorbing
Advanced Share Registry's book over a few days in early March 2024. Bulk transfers of a
registrar's client list look identical to individual switches in this data, so treat
same-week clusters as one event.

`data/registry_changes_history.csv` is the flat export of all 905, with the `method` column
so weaker inferences stay visible.

### Ticker changes

A registrar switch is filed under whatever code the company traded as **that day**, which
is not necessarily the code it trades as now. 271 of the 905 rows above are in that
position, and 249 of them name a code that is not in `data/asx_registries.csv` at all — so
before this existed, joining the two files on `code` silently dropped a quarter of the
history.

Recovering the renames costs no extra requests, because the scan already recorded both
halves. The archive resolves any code to one entity and then serves that entity's whole
history under every code it has used, so an announcement whose released-under `code` differs
from the `query_code` we asked about *is* a rename, stated by the ASX rather than inferred.
`tickers` groups those pairs into the `ticker_change` table and
`data/ticker_changes.csv`: **436 renames across 362 companies**, from 2003 to 2025, 69 of
those companies having changed code more than once.

Three things it does not claim:

- **It is not a date, it is a bracket.** The rename happened somewhere between the last
  announcement lodged under the old code and the first under the new one, and this database
  holds only *registry-related* announcements — a sparse sample of a company's filings. So
  the bracket is wide, and 241 of the 436 have no upper end at all, the company not having
  lodged a registry notice under its new code yet.
- **It is not the full list of ASX renames.** A company reaches this table only by having
  filed something about its share registry, under a code it has since given up.
- **The old code is not a key.** ASX recycles codes: 41 of these old codes belong to a
  different company today, and `AUK` and `EMS` each appear twice because two entities used
  the code in turn and both later renamed. The primary key is the pair, and
  `old_code_relisted` flags the 41 so a join does not quietly land on a stranger — the same
  hazard `backfill` guards against by looking candidates up under `query_code`.

The safe direction is current → old: `current_code` is unambiguous, which is why `export`
now carries it as its own column alongside the released-under `code`.

### Schema (`announcements.sqlite`)

Separate database from `registry.sqlite` — the daily tracker's state is unaffected by
running this.

A snapshot is committed at [`data/announcements.sqlite`](data/announcements.sqlite). Unlike
`registry.sqlite`, which is rebuildable from a single day's scrape and so is gitignored,
this one is expensive to rebuild (tens of thousands of archive requests) and describes a
past that does not change — so it is worth carrying in the repo.

**It currently holds the full-market crawl above** — 29,921 company-years across all 1,840
codes. The `scanned` table records exactly which `(code, year)` pairs it holds, so running
`scan` against it resumes rather than restarting: it skips what is already there and crawls
only the gaps (plus the current year, which is always re-scanned). Point `--db` at it to
extend it.

Because it is a binary blob, git cannot diff it meaningfully — every scan rewrites the whole
file. If it starts churning the history, `export` it to CSV and track that instead, which is
the pattern `data/asx_registries.csv` already follows.

`announcement` — one row per registry-related announcement found. Headlines that are not
about a share registry are discarded at parse time and never stored.

| Column | Meaning |
| --- | --- |
| `ids_id` | ASX announcement id (PK); also the key for the PDF URL |
| `code` | code the announcement was **released under** |
| `query_code` | code we searched for — differs from `code` after a ticker change |
| `date` | release date, ISO |
| `time` | release time as shown, e.g. `12:48 pm` |
| `headline` | announcement title verbatim |
| `classification` | `provider_change` / `address_only` / `registry_other` |
| `price_sensitive` | 1 if flagged with the asterisk |
| `pages` | page count |
| `year` | the archive year slice it came from |

`resolution` — one row per PDF opened. Separate from `announcement` so re-running `resolve`
does not re-fetch, and so a failed extraction is recorded rather than retried forever.

| Column | Meaning |
| --- | --- |
| `ids_id` | FK to `announcement` (PK) |
| `pdf_url` | resolved `announcements.asx.com.au` link |
| `old_registry` | outgoing registrar, canonical, NULL if not determined |
| `new_registry` | incoming registrar, canonical, NULL if not determined |
| `method` | which rule fired — `from_to` is strongest, `two_brands` weakest; a `prior_doc` / `next_doc` part means `backfill` supplied that side, and `prior_doc+next_doc` alone means the notice supplied neither |
| `brands` | every registrar named in the document, comma separated |
| `resolved_at` | when the PDF was read |
| `ok` | 1 if text was extractable — 0 means an image-only scan |
| `backfilled_from` | `ids_id` of the unrelated announcement a missing side was read out of — one per side supplied, comma separated, so a notice that named neither registrar carries two. NULL if the notice named both |

`probe` — documents `backfill` opened only to see which registrar they name. They say
nothing about a registry change, so they have no place in `announcement`; the table exists
so a second backfill run does not re-fetch them. It is a cache of an extraction rule's
output, so tightening a rule makes every probe that fired on it stale:
`backfill --reprobe-brand Boardroom` forgets those probes *and* clears every side they
supplied, putting the notice back to naming only what it named itself so the rest is
re-earned rather than keeping an answer whose evidence is gone. `backfill --only CODES`
bounds a repair to the companies it concerns.

| Column | Meaning |
| --- | --- |
| `ids_id` | ASX announcement id (PK) |
| `code`, `date`, `headline` | what was opened |
| `pdf_url` | resolved `announcements.asx.com.au` link |
| `brands` | every registrar named, comma separated — exactly one is what makes it usable |
| `probed_at` | when it was read |
| `ok` | 1 if text was extractable |

`scanned` — one row per `(code, year)` fetched, so an interrupted crawl resumes and a year
with genuinely no announcements is distinguishable from one never scanned.

| Column | Meaning |
| --- | --- |
| `code`, `year` | composite PK |
| `scanned_at` | when it was fetched |
| `found` | total announcements on that page, before registry filtering |

`ticker_change` — renames, derived from `announcement` and nothing else. `tickers` drops and
rewrites it rather than upserting, because a rescan can retract a pair: re-indexing a year
under a recycled code reassigns its announcements to the code's current owner, and an upsert
would leave the old pair behind as a fact nothing supports any more.

| Column | Meaning |
| --- | --- |
| `old_code`, `current_code` | composite PK — the old code alone is not unique |
| `old_last_seen` | last announcement lodged under the old code |
| `current_first_seen` | first lodged under the new one; NULL for 241 of 436 — the rename is bracketed on one side only |
| `announcements` | how many announcements carry the old code — 1 is a single sighting, not a weaker claim |
| `old_code_relisted` | 1 if another company trades under the old code today |
| `derived_at` | when the table was last rebuilt |

A registrar switch is `old_registry IS NOT NULL AND new_registry IS NOT NULL AND
old_registry <> new_registry` — that is what `changes` and `export` select, and it draws
from all three classifications, not just `provider_change`.

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
