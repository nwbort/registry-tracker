"""ASX share registry tracker.

Scrapes the share registry (registrar) for every ASX listed company and records
*changes only* - a company's registry is stored as an effective-dated row that is
written once and then left alone until the registry actually changes.

Data sources (both public, no auth):
  * Company directory CSV
      https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file
  * Per-company "about" JSON (contains addressShareRegistry)
      https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0/companies/<code>/about

Usage:
    python registry_tracker.py fetch                 # scrape everything
    python registry_tracker.py fetch --codes 14D,BHP # scrape a few codes
    python registry_tracker.py fetch --limit 50      # smoke test
    python registry_tracker.py summary               # market share by registrar
    python registry_tracker.py changes               # registry switches detected
    python registry_tracker.py history 14D           # one company's timeline
    python registry_tracker.py runs                  # scrape run log
    python registry_tracker.py export out.csv        # current state as CSV
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import requests

DIRECTORY_URL = (
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file"
    "?access_token=83ff96335c2d45a094df02a206a39ff4&fileName=ASXListedCompanies.csv"
)
ABOUT_URL = (
    "https://cdn-api.markitdigital.com/apiman-gateway/ASX/asx-research/1.0"
    "/companies/{code}/about"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.asx.com.au",
    "Referer": "https://www.asx.com.au/",
}

DB_PATH = Path(__file__).with_name("registry.sqlite")

log = logging.getLogger("registry_tracker")


# --------------------------------------------------------------------------
# Registry name normalisation
# --------------------------------------------------------------------------

# The ASX free-text "attention" field spells the same registrar many ways.
# Ordered list of (regex, canonical name); first match wins.
_CANONICAL_PATTERNS: list[tuple[str, str]] = [
    (r"computershare", "Computershare"),
    (r"\bmuf?g\b|link market services|link group", "MUFG Corporate Markets"),
    (r"automic", "Automic"),
    (r"board\s*room", "Boardroom"),
    (r"advanced share", "Advanced Share Registry"),
    (r"registry direct", "Registry Direct"),
    (r"security transfer", "Security Transfer Australia"),
    (r"\bxcend\b", "Xcend"),
    (r"tricor", "Tricor"),
    (r"vistra", "Vistra"),
    (r"\bmpms\b|mps market|market place", "MPMS"),
    (r"equiniti|american stock transfer", "Equiniti"),
    (r"\bbnp paribas\b", "BNP Paribas"),
    (r"\bcitibank\b|\bcitigroup\b", "Citi"),
    (r"\bhsbc\b", "HSBC"),
    (r"\bjp\s*morgan\b|\bjpmorgan\b", "J.P. Morgan"),
    (r"\btmx\b|\bcst trust\b|canadian stock transfer", "TSX Trust"),
    (r"\bnew zealand\b.*registr|\bnzx\b", "NZX Registry"),
]

# Corporate-form noise stripped before title-casing an unmatched name.
_SUFFIX_RE = re.compile(
    r"\b(pty|proprietary|ltd|limited|llc|inc|incorporated|plc|"
    r"services|investor|share|registry|registrar|registries)\b",
    re.I,
)


def canonical_registry(raw: str | None) -> str | None:
    """Map a free-text registrar name to a canonical brand, or None if blank."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    lowered = text.lower()
    for pattern, canonical in _CANONICAL_PATTERNS:
        if re.search(pattern, lowered):
            return canonical
    # Unknown registrar: keep it, but tidy it up so grouping is still useful.
    cleaned = _SUFFIX_RE.sub("", text)
    cleaned = re.sub(r"[^A-Za-z0-9&' ]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned.title() or text.strip().title()


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class Company:
    code: str
    name: str
    industry: str
    listing_date: str | None
    market_cap: int | None


@dataclass
class RegistryRecord:
    code: str
    registry_raw: str | None
    registry_canonical: str | None
    registry_address: str | None
    registry_phone: str | None
    company_name: str | None
    company_address: str | None
    website: str | None
    error: str | None = None


# --------------------------------------------------------------------------
# Scraping
# --------------------------------------------------------------------------


class AsxClient:
    """Thin HTTP client with retries and a shared rate limit across threads."""

    def __init__(
        self, delay: float = 0.02, timeout: int = 30, retries: int = 3, pool: int = 12
    ):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        # Default urllib3 pool is 10; without this, extra workers just churn
        # connections ("Connection pool is full, discarding connection").
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=max(pool, 10), pool_maxsize=max(pool, 10)
        )
        self.session.mount("https://", adapter)
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._lock = threading.Lock()
        self._next_at = 0.0

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self.delay

    def get(self, url: str) -> requests.Response:
        last_exc: Exception | None = None
        for attempt in range(self.retries):
            self._throttle()
            try:
                resp = self.session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:  # network blip
                last_exc = exc
            else:
                if resp.status_code == 404:
                    return resp  # genuine "no such company"; don't retry
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_exc = requests.HTTPError(f"HTTP {resp.status_code}")
                else:
                    return resp
            time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"GET {url} failed after {self.retries} attempts: {last_exc}")

    def directory(self) -> list[Company]:
        resp = self.get(DIRECTORY_URL)
        resp.raise_for_status()
        reader = csv.DictReader(io.StringIO(resp.text))
        companies: list[Company] = []
        for row in reader:
            code = (row.get("ASX code") or "").strip().upper()
            if not code:
                continue
            companies.append(
                Company(
                    code=code,
                    name=(row.get("Company name") or "").strip(),
                    industry=(row.get("GICs industry group") or "").strip(),
                    listing_date=_iso_date(row.get("Listing date")),
                    market_cap=_to_int(row.get("Market Cap")),
                )
            )
        return companies

    def about(self, code: str) -> RegistryRecord:
        try:
            resp = self.get(ABOUT_URL.format(code=code))
        except RuntimeError as exc:
            return _error_record(code, str(exc))
        if resp.status_code == 404:
            return _error_record(code, "not found (404)")
        if resp.status_code != 200:
            return _error_record(code, f"HTTP {resp.status_code}")
        try:
            data = resp.json().get("data") or {}
        except json.JSONDecodeError:
            return _error_record(code, "invalid JSON")

        registry = data.get("addressShareRegistry") or {}
        contact = data.get("addressContact") or {}
        raw = (registry.get("attention") or "").strip() or None
        return RegistryRecord(
            code=code,
            registry_raw=raw,
            registry_canonical=canonical_registry(raw),
            registry_address=(registry.get("address") or "").strip() or None,
            registry_phone=(registry.get("phone") or "").strip() or None,
            company_name=(data.get("displayName") or "").strip() or None,
            company_address=(contact.get("address") or "").strip() or None,
            website=(data.get("websiteUrl") or "").strip() or None,
        )


def _error_record(code: str, error: str) -> RegistryRecord:
    return RegistryRecord(code, None, None, None, None, None, None, None, error=error)


def _iso_date(value: str | None) -> str | None:
    """ASX gives d/m/Y; store ISO so ordering works."""
    if not value or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return value.strip()


def _to_int(value: str | None) -> int | None:
    if value is None or not str(value).strip():
        return None
    try:
        return int(float(str(value).replace(",", "").replace("$", "")))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Storage
#
# The database records changes, not snapshots. Each company has exactly one
# `is_current` row in registry_state; a run only writes when the registry
# details actually differ from that row, so a quiet day adds no rows at all.
# --------------------------------------------------------------------------

SCHEMA = """
-- Market cap is price-derived and moves every day, so it is deliberately NOT
-- refreshed on every run - see should_refresh_caps(). market_cap_as_at records
-- when the stored figure was actually taken.
CREATE TABLE IF NOT EXISTS company (
    code             TEXT PRIMARY KEY,
    name             TEXT,
    industry         TEXT,
    listing_date     TEXT,
    market_cap       INTEGER,
    market_cap_as_at TEXT,
    company_address  TEXT,
    website          TEXT,
    updated_at       TEXT
);

-- One row per (company, registry), written once and then left alone. Closed out
-- when the registry changes, so first_seen..closed_on gives the registry as at
-- any date. An unchanged company is not touched at all - the run table is what
-- records that we checked and found nothing.
CREATE TABLE IF NOT EXISTS registry_state (
    id                  INTEGER PRIMARY KEY,
    code                TEXT NOT NULL,
    registry_raw        TEXT,
    registry_canonical  TEXT,
    registry_address    TEXT,
    registry_phone      TEXT,
    first_seen          TEXT NOT NULL,
    closed_on           TEXT,
    is_current          INTEGER NOT NULL DEFAULT 1,
    closed_reason       TEXT,
    UNIQUE (code, first_seen)
);
CREATE INDEX IF NOT EXISTS idx_state_current ON registry_state(code, is_current);
CREATE INDEX IF NOT EXISTS idx_state_registry ON registry_state(registry_canonical);

-- A registrar actually changed hands (canonical name differs).
CREATE TABLE IF NOT EXISTS registry_change (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL,
    detected_on     TEXT NOT NULL,
    old_registry    TEXT,
    new_registry    TEXT,
    old_raw         TEXT,
    new_raw         TEXT,
    UNIQUE (code, detected_on)
);

-- Proof the scrape ran, even on days that produce no rows anywhere else.
CREATE TABLE IF NOT EXISTS run (
    run_date        TEXT PRIMARY KEY,
    started_at      TEXT,
    finished_at     TEXT,
    companies       INTEGER DEFAULT 0,
    new_states      INTEGER DEFAULT 0,
    changed         INTEGER DEFAULT 0,
    unchanged       INTEGER DEFAULT 0,
    data_gaps       INTEGER DEFAULT 0,
    errors          INTEGER DEFAULT 0,
    delisted        INTEGER DEFAULT 0,
    caps_written    INTEGER DEFAULT 0,
    caps_refreshed  INTEGER DEFAULT 0
);

-- Only written when a fetch fails; empty on a healthy run.
CREATE TABLE IF NOT EXISTS fetch_error (
    code        TEXT NOT NULL,
    run_date    TEXT NOT NULL,
    error       TEXT,
    PRIMARY KEY (code, run_date)
);
"""

# Fields that constitute "the registry" for change purposes.
_STATE_FIELDS = ("registry_canonical", "registry_raw", "registry_address", "registry_phone")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(run)")}
    for col in ("caps_written", "caps_refreshed"):
        if col not in run_cols:
            conn.execute(f"ALTER TABLE run ADD COLUMN {col} INTEGER DEFAULT 0")
            conn.commit()
    have = {row["name"] for row in conn.execute("PRAGMA table_info(company)")}
    if "market_cap_as_at" not in have:
        conn.execute("ALTER TABLE company ADD COLUMN market_cap_as_at TEXT")
        # Existing figures were captured at the last run we know about.
        conn.execute(
            "UPDATE company SET market_cap_as_at = (SELECT MAX(run_date) FROM run)"
            " WHERE market_cap IS NOT NULL"
        )
        conn.commit()


def should_refresh_caps(conn: sqlite3.Connection, run_date: str) -> bool:
    """True on the first run of a calendar month (or if we've never stored any).

    Keyed on when a refresh was last *attempted*, not when a value last changed -
    otherwise a month where no cap happened to move would re-trigger every day.
    """
    last = conn.execute(
        "SELECT MAX(run_date) FROM run WHERE caps_refreshed = 1"
    ).fetchone()[0]
    if last:
        return last[:7] != run_date[:7]
    stored = conn.execute("SELECT MAX(market_cap_as_at) FROM company").fetchone()[0]
    return not stored or stored[:7] != run_date[:7]


def save_companies(
    conn: sqlite3.Connection, companies: list[Company], run_date: str, refresh_caps: bool
) -> tuple[set[str], int]:
    """Upsert directory data. Returns (new listing codes, market caps written).

    Static fields are only written when they actually differ. Market cap is only
    written for brand new listings, or when `refresh_caps` is set - it changes
    every day and would otherwise churn the database and the exported CSV.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = {row["code"] for row in conn.execute("SELECT code FROM company")}
    new_codes = {c.code for c in companies} - existing

    conn.executemany(
        """INSERT INTO company (code, name, industry, listing_date, market_cap,
               market_cap_as_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET
               name=excluded.name, industry=excluded.industry,
               listing_date=excluded.listing_date, updated_at=excluded.updated_at
           WHERE company.name IS NOT excluded.name
              OR company.industry IS NOT excluded.industry
              OR company.listing_date IS NOT excluded.listing_date""",
        [
            (c.code, c.name, c.industry, c.listing_date, c.market_cap, run_date, now)
            for c in companies
        ],
    )

    caps_written = len(new_codes)  # new listings carry their cap in on insert
    if refresh_caps:
        caps_written += update_market_caps(conn, run_date, companies)
    conn.commit()
    return new_codes, caps_written


def update_market_caps(
    conn: sqlite3.Connection, run_date: str, companies: list[Company], only: set[str] | None = None
) -> int:
    """Write market caps, skipping rows where the figure hasn't moved."""
    rows = [c for c in companies if only is None or c.code in only]
    written = 0
    for c in rows:
        cur = conn.execute(
            "UPDATE company SET market_cap = ?, market_cap_as_at = ?"
            " WHERE code = ? AND market_cap IS NOT ?",
            (c.market_cap, run_date, c.code, c.market_cap),
        )
        written += cur.rowcount
    return written


def current_state(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM registry_state WHERE code = ? AND is_current = 1", (code,)
    ).fetchone()


def apply_record(
    conn: sqlite3.Connection, run_date: str, rec: RegistryRecord, prev_run: str | None
) -> str:
    """Fold one fetched record into the database.

    Writes nothing at all when the registry is unchanged. `prev_run` is the date
    of the last scrape, used to close a superseded row on the last date its old
    registry was actually observed.

    Returns one of: new, changed, unchanged, gap, error.
    """
    if rec.error:
        conn.execute(
            """INSERT INTO fetch_error (code, run_date, error) VALUES (?, ?, ?)
               ON CONFLICT(code, run_date) DO UPDATE SET error=excluded.error""",
            (rec.code, run_date, rec.error),
        )
        return "error"

    # Company-level detail is overwritten in place - it isn't registry data and
    # shouldn't create registry history. Only write when it actually differs.
    conn.execute(
        """UPDATE company SET company_address = ?, website = ?
           WHERE code = ?
             AND (IFNULL(company_address, '') != IFNULL(?, '')
                  OR IFNULL(website, '') != IFNULL(?, ''))""",
        (rec.company_address, rec.website, rec.code, rec.company_address, rec.website),
    )

    cur = current_state(conn, rec.code)

    # An empty registry where we previously had one is an upstream data gap, not
    # a company leaving its registrar. Leave the existing state untouched.
    if rec.registry_canonical is None and cur is not None and cur["registry_canonical"]:
        log.debug("%s: blank registry upstream, keeping %s", rec.code, cur["registry_canonical"])
        return "gap"

    if cur is None:
        conn.execute(
            """INSERT INTO registry_state (code, registry_raw, registry_canonical,
                   registry_address, registry_phone, first_seen, is_current)
               VALUES (?, ?, ?, ?, ?, ?, 1)""",
            (
                rec.code, rec.registry_raw, rec.registry_canonical,
                rec.registry_address, rec.registry_phone, run_date,
            ),
        )
        return "new"

    if all(cur[f] == getattr(rec, f) for f in _STATE_FIELDS):
        return "unchanged"  # deliberately writes nothing

    # Something in the registry details moved. Close the old row, open a new one.
    conn.execute(
        """UPDATE registry_state SET is_current = 0, closed_reason = 'superseded',
               closed_on = ? WHERE id = ?""",
        (prev_run or run_date, cur["id"]),
    )
    conn.execute(
        """INSERT INTO registry_state (code, registry_raw, registry_canonical,
               registry_address, registry_phone, first_seen, is_current)
           VALUES (?, ?, ?, ?, ?, ?, 1)""",
        (
            rec.code, rec.registry_raw, rec.registry_canonical,
            rec.registry_address, rec.registry_phone, run_date,
        ),
    )

    # Only a different registrar counts as a change event; an address or
    # punctuation edit updates the state row without raising an alert.
    if cur["registry_canonical"] != rec.registry_canonical:
        conn.execute(
            """INSERT INTO registry_change
                   (code, detected_on, old_registry, new_registry, old_raw, new_raw)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(code, detected_on) DO UPDATE SET
                   old_registry=excluded.old_registry, new_registry=excluded.new_registry,
                   old_raw=excluded.old_raw, new_raw=excluded.new_raw""",
            (
                rec.code, run_date, cur["registry_canonical"], rec.registry_canonical,
                cur["registry_raw"], rec.registry_raw,
            ),
        )
        log.info(
            "CHANGE %s: %s -> %s", rec.code, cur["registry_canonical"], rec.registry_canonical
        )
        return "changed"

    log.info("detail update %s (%s)", rec.code, rec.registry_canonical)
    return "changed"


def close_delisted(conn: sqlite3.Connection, run_date: str, listed_codes: set[str]) -> int:
    """Retire state rows for codes that have dropped off the ASX directory."""
    stale = [
        row["code"]
        for row in conn.execute("SELECT code FROM registry_state WHERE is_current = 1")
        if row["code"] not in listed_codes
    ]
    if stale:
        conn.executemany(
            """UPDATE registry_state SET is_current = 0, closed_reason = 'delisted',
                   closed_on = ? WHERE code = ? AND is_current = 1""",
            [(run_date, c) for c in stale],
        )
        log.info("Closed %d delisted code(s): %s", len(stale), ", ".join(sorted(stale)[:10]))
    return len(stale)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_fetch(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    client = AsxClient(delay=args.delay, pool=args.workers)
    run_date = args.run_date or date.today().isoformat()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev_run = conn.execute(
        "SELECT MAX(run_date) FROM run WHERE run_date < ?", (run_date,)
    ).fetchone()[0]

    log.info("Fetching company directory...")
    companies = client.directory()
    refresh_caps = args.refresh_caps or should_refresh_caps(conn, run_date)
    new_codes, caps_written = save_companies(conn, companies, run_date, refresh_caps)
    log.info(
        "Directory: %d listed companies, %d new listing(s)%s",
        len(companies), len(new_codes),
        ", monthly market cap refresh" if refresh_caps else "",
    )
    listed_codes = {c.code for c in companies}

    full_market = not args.codes and not args.limit
    if args.codes:
        targets = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
    else:
        targets = [c.code for c in companies]
        if args.limit:
            targets = targets[: args.limit]

    log.info("Fetching registry details for %d companies (%d workers)...", len(targets), args.workers)
    tally = {"new": 0, "changed": 0, "unchanged": 0, "gap": 0, "error": 0}
    touched: set[str] = set()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, rec in enumerate(pool.map(client.about, targets), start=1):
            outcome = apply_record(conn, run_date, rec, prev_run)
            tally[outcome] += 1
            if outcome in ("new", "changed"):
                touched.add(rec.code)
            if outcome == "error":
                log.warning("%s: %s", rec.code, rec.error)
            if i % 250 == 0:
                conn.commit()
                log.info("  %d/%d", i, len(targets))

    # A company whose registry just moved gets a fresh market cap alongside it,
    # so the row we are about to publish is internally consistent.
    if touched and not refresh_caps:
        caps_written += update_market_caps(conn, run_date, companies, only=touched)

    delisted = close_delisted(conn, run_date, listed_codes) if full_market else 0

    conn.execute(
        """INSERT INTO run (run_date, started_at, finished_at, companies, new_states,
               changed, unchanged, data_gaps, errors, delisted, caps_written,
               caps_refreshed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(run_date) DO UPDATE SET
               finished_at=excluded.finished_at, companies=excluded.companies,
               new_states=excluded.new_states, changed=excluded.changed,
               unchanged=excluded.unchanged, data_gaps=excluded.data_gaps,
               errors=excluded.errors, delisted=excluded.delisted,
               caps_written=excluded.caps_written,
               caps_refreshed=excluded.caps_refreshed""",
        (
            run_date, started, datetime.now(timezone.utc).isoformat(timespec="seconds"),
            len(targets), tally["new"], tally["changed"], tally["unchanged"],
            tally["gap"], tally["error"], delisted, caps_written, int(refresh_caps),
        ),
    )
    conn.commit()

    log.info(
        "Run %s: %d companies - %d new, %d changed, %d unchanged, %d data gaps, "
        "%d errors, %d delisted, %d market caps written",
        run_date, len(targets), tally["new"], tally["changed"], tally["unchanged"],
        tally["gap"], tally["error"], delisted, caps_written,
    )
    return 0


def cmd_summary(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute(
        """SELECT COALESCE(s.registry_canonical, '(none listed)') AS registry,
                  COUNT(*) AS companies,
                  COALESCE(SUM(c.market_cap), 0) AS market_cap
           FROM registry_state s JOIN company c ON c.code = s.code
           WHERE s.is_current = 1
           GROUP BY registry ORDER BY companies DESC"""
    ).fetchall()
    if not rows:
        print("No data yet - run 'fetch' first.")
        return 1
    last_run = conn.execute("SELECT MAX(run_date) FROM run").fetchone()[0]
    cap_date = conn.execute("SELECT MAX(market_cap_as_at) FROM company").fetchone()[0]
    total = sum(r["companies"] for r in rows)
    total_cap = sum(r["market_cap"] for r in rows)
    print(f"Share registry market share - ASX, as at {last_run} ({total} companies)")
    print(f"Market caps as at {cap_date}\n")
    print(f"{'Registry':<34}{'Cos':>6}{'Share':>8}{'Mkt cap $bn':>14}{'Cap %':>8}")
    print("-" * 70)
    for r in rows:
        cap_pct = 100 * r["market_cap"] / total_cap if total_cap else 0
        print(
            f"{r['registry'][:33]:<34}{r['companies']:>6}"
            f"{100 * r['companies'] / total:>7.1f}%{r['market_cap'] / 1e9:>14,.1f}{cap_pct:>7.1f}%"
        )
    return 0


def cmd_changes(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute(
        """SELECT ch.detected_on, ch.code, c.name, ch.old_registry, ch.new_registry
           FROM registry_change ch LEFT JOIN company c ON c.code = ch.code
           ORDER BY ch.detected_on DESC, ch.code"""
    ).fetchall()
    if not rows:
        print("No registry changes detected yet (needs at least two runs).")
        return 0
    print(f"{len(rows)} registry change(s) detected\n")
    for r in rows:
        print(
            f"{r['detected_on']}  {r['code']:<5} {(r['name'] or '')[:38]:<40}"
            f"{r['old_registry']} -> {r['new_registry']}"
        )
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    code = args.code.upper()
    name = conn.execute("SELECT name FROM company WHERE code = ?", (code,)).fetchone()
    rows = conn.execute(
        """SELECT * FROM registry_state WHERE code = ?
           ORDER BY first_seen""",
        (code,),
    ).fetchall()
    if not rows:
        print(f"No registry history for {code}.")
        return 1
    last_run = conn.execute("SELECT MAX(run_date) FROM run").fetchone()[0]
    print(f"{code} - {name['name'] if name else 'unknown'}\n")
    for r in rows:
        if r["is_current"]:
            span, state = f"{r['first_seen']} -> now", f"confirmed {last_run}"
        else:
            span, state = f"{r['first_seen']} -> {r['closed_on']}", r["closed_reason"] or "closed"
        print(f"{span:<26}{str(r['registry_canonical']):<26}[{state}]")
        print(f"{'':<26}{r['registry_address'] or ''}")
    return 0


def cmd_runs(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute("SELECT * FROM run ORDER BY run_date DESC LIMIT ?", (args.limit,)).fetchall()
    if not rows:
        print("No runs recorded yet.")
        return 0
    print(
        f"{'Run':<12}{'Cos':>6}{'New':>6}{'Chg':>6}{'Same':>7}{'Gaps':>6}{'Err':>6}"
        f"{'Delist':>8}{'Caps':>7}"
    )
    print("-" * 70)
    for r in rows:
        caps = f"{r['caps_written']}" + ("*" if r["caps_refreshed"] else "")
        print(
            f"{r['run_date']:<12}{r['companies']:>6}{r['new_states']:>6}{r['changed']:>6}"
            f"{r['unchanged']:>7}{r['data_gaps']:>6}{r['errors']:>6}{r['delisted']:>8}{caps:>7}"
        )
    print("\n* monthly market cap refresh")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = conn.execute(
        """SELECT c.code, c.name, c.industry, c.listing_date, c.market_cap,
                  c.market_cap_as_at, s.registry_canonical, s.registry_raw,
                  s.registry_address, s.registry_phone, s.first_seen
           FROM registry_state s JOIN company c ON c.code = s.code
           WHERE s.is_current = 1 ORDER BY c.code"""
    ).fetchall()
    if not rows:
        print("No data yet - run 'fetch' first.")
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["code", "company", "industry", "listing_date", "market_cap", "market_cap_as_at",
             "registry", "registry_raw", "registry_address", "registry_phone", "registry_since"]
        )
        writer.writerows([tuple(r) for r in rows])
    print(f"Wrote {len(rows)} rows to {out}")
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", type=Path, default=DB_PATH, help="SQLite file (default: registry.sqlite)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Scrape registries, recording only what changed")
    p_fetch.add_argument("--run-date", help="Override run date (YYYY-MM-DD)")
    p_fetch.add_argument(
        "--refresh-caps", action="store_true",
        help="Force a market cap refresh (otherwise: monthly, new listings, and registry changes)",
    )
    p_fetch.add_argument("--codes", help="Comma-separated ASX codes instead of the full market")
    p_fetch.add_argument("--limit", type=int, help="Only fetch the first N codes (testing)")
    p_fetch.add_argument("--workers", type=int, default=12, help="Concurrent requests (default 12)")
    p_fetch.add_argument(
        "--delay", type=float, default=0.02,
        help="Min seconds between requests, shared across all threads (default 0.02 = 50/sec). "
             "This, not --workers, is the real rate limit.",
    )
    p_fetch.set_defaults(func=cmd_fetch)

    sub.add_parser("summary", help="Registrar market share, current state").set_defaults(func=cmd_summary)
    sub.add_parser("changes", help="List detected registry switches").set_defaults(func=cmd_changes)

    p_hist = sub.add_parser("history", help="Registry timeline for one company")
    p_hist.add_argument("code")
    p_hist.set_defaults(func=cmd_history)

    p_runs = sub.add_parser("runs", help="Scrape run log")
    p_runs.add_argument("--limit", type=int, default=30)
    p_runs.set_defaults(func=cmd_runs)

    p_exp = sub.add_parser("export", help="Export current registries to CSV")
    p_exp.add_argument("out", help="Output CSV path")
    p_exp.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
