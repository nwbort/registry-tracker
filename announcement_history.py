"""Historical ASX share-registry changes, reconstructed from company announcements.

`registry_tracker.py` can only see changes that happen *while it is running* - it
diffs today's registrar against yesterday's. This module goes the other way: it
mines the ASX announcement archive, which goes back to 1998, for the notices
companies are required to lodge under Listing Rule 3.15.1 when they change share
registry. That reconstructs a registrar history for a ticker long before the
tracker existed.

Data sources (both public, no auth):
  * Per-company, per-year announcement index (HTML)
      https://www.asx.com.au/asx/v2/statistics/announcements.do?by=asxCode&asxCode=<CODE>&timeframe=Y&year=<YYYY>
  * Announcement PDF, behind a terms-of-access interstitial
      https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?display=pdf&idsId=<ID>
      -> hidden "pdfURL" field -> https://announcements.asx.com.au/asxpdf/...

Note the modern `asx.api.markitdigital.com/.../announcements` JSON endpoint is *not*
usable here: it is hard-capped at the 5 most recent announcements and ignores every
paging parameter. The legacy `statistics/announcements.do` path is the only one that
serves history, and only in per-company-per-year slices.

Why the PDFs matter: the headline alone cannot tell you what happened. "Change of
Share Registry Address" is a registrar moving office - the provider is unchanged -
and those outnumber real provider switches roughly three to one. Only the PDF body
names the outgoing and incoming registrar, so `resolve` fetches it and extracts them.

Usage:
    python announcement_history.py scan --codes ECS,BHP   # index announcements
    python announcement_history.py scan --limit 50        # sample of the market
    python announcement_history.py scan                   # whole market (~30k requests)
    python announcement_history.py resolve                # read candidate PDFs
    python announcement_history.py changes                # resolved registrar switches
    python announcement_history.py timeline ECS           # one ticker's history
    python announcement_history.py export out.csv
"""

from __future__ import annotations

import argparse
import csv
import html
import io
import logging
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import requests

from registry_tracker import canonical_registry

INDEX_URL = "https://www.asx.com.au/asx/v2/statistics/announcements.do"
PDF_GATE_URL = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Referer": "https://www.asx.com.au/markets/trade-our-cash-market/historical-announcements",
}

# The archive starts here; earlier years return an empty table.
FIRST_YEAR = 1998

DB_PATH = Path(__file__).with_name("announcements.sqlite")

log = logging.getLogger("announcement_history")


# --------------------------------------------------------------------------
# Headline classification
# --------------------------------------------------------------------------

# Anything matching this is about a share registry in some way. Deliberately
# broad - narrowing happens below, and a headline we never look at is a change
# we can never find.
_REGISTRY_HEADLINE = re.compile(
    r"(share\s*)?registr(?:y|ies|ars?)\b|registry\s*services", re.I
)

# Headlines that merely *contain* a registry word but are about something else.
# "Registration" is the big one: re-registration, registration statements, and
# US Form S-x filings all collide with a naive "registr" match.
_NOT_REGISTRY = re.compile(
    r"registration|registered\s+office|register\s+of\s+members|"
    r"substantial\s+holder|form\s+s-\d|prospectus|"
    r"registry\s+business|sale\s+of\s+registr|registry\s+sale",
    re.I,
)

# The registrar itself moved office - same provider, new address. These are far
# more common than real switches, and they arrive in clusters (every client of
# one registrar lodges on the same day).
_ADDRESS_ONLY = re.compile(
    r"\baddress\b|\brelocat|\bnew\s+premises\b|change\s+of\s+.{0,20}details?\s+.{0,20}address",
    re.I,
)

# Strong positives for an actual change of provider.
_PROVIDER_CHANGE = re.compile(
    r"change\s+(?:of|in)\s+(?:the\s+)?(?:share\s+)?registr(?:y|ies|ars?)|"
    r"(?:new|appointment\s+of|transfer\s+of)\s+(?:a\s+)?(?:share\s+)?registr(?:y|ies|ars?)|"
    r"registr(?:y|ies|ars?)\s+(?:change|transfer|appointment)|"
    r"share\s+registry\s+(?:service\s+)?provider|"
    # "<Company> reappoints X as registrars" - the appointment verb and the
    # registry word sit either side of the incoming registrar's name.
    r"\bre-?appoints?\b|"
    r"\bappoints?\b.{0,60}?\bregistr(?:y|ies|ars?)\b|"
    r"\bregistr(?:y|ies|ars?)\b.{0,60}?\bappoint",
    re.I,
)

CLASS_PROVIDER = "provider_change"
CLASS_ADDRESS = "address_only"
CLASS_OTHER = "registry_other"


def classify_headline(headline: str) -> str | None:
    """Bucket a headline, or None if it is not about a share registry at all.

    Returns one of CLASS_PROVIDER (looks like a real registrar switch),
    CLASS_ADDRESS (same registrar, new address) or CLASS_OTHER (registry-related
    but unclear). The PDF is what settles it - this only decides what is worth
    opening.
    """
    if not headline:
        return None
    text = " ".join(headline.split())
    if not _REGISTRY_HEADLINE.search(text):
        return None
    if _NOT_REGISTRY.search(text):
        return None
    # Address wins over provider: "Change of Share Registry Address" matches both
    # patterns, and it is an address notice.
    if _ADDRESS_ONLY.search(text):
        return CLASS_ADDRESS
    if _PROVIDER_CHANGE.search(text):
        return CLASS_PROVIDER
    return CLASS_OTHER


# --------------------------------------------------------------------------
# Registrar extraction from PDF text
# --------------------------------------------------------------------------

# Historical registrars that no longer appear in the live ASX feed, so
# registry_tracker's canonical list has never needed them. Ordered; first wins.
_HISTORICAL_REGISTRARS: list[tuple[str, str]] = [
    (r"asx\s+perpetual\s+registrars?", "ASX Perpetual Registrars"),
    (r"perpetual\s+registrars?", "ASX Perpetual Registrars"),
    (r"registries\s+limited|registries\s+ltd", "Registries Limited"),
    (r"white\s+outsourcing", "White Outsourcing"),
    (r"national\s+registry", "National Registry Services"),
    (r"gould\s+ralph", "Gould Ralph"),
    (r"gg\s+registry|steinepreis", "GG Registry"),
    (r"gaden|gadens", "Gadens"),
    (r"gould", "Gould Ralph"),
]

# Registrars still trading. These patterns are tighter than the ones in
# registry_tracker._CANONICAL_PATTERNS, which are matched against a short
# "attention" field and can afford wildcards - here they are run over whole PDFs,
# where a `.*` would happily span half a document. The canonical *name* is not
# repeated: registry_tracker.canonical_registry() supplies it, so the two modules
# cannot drift apart on spelling.
_MODERN_REGISTRAR_PATTERNS: list[str] = [
    r"computershare",
    r"link\s+market\s+services|link\s+group",
    r"mufg\s+(?:corporate\s+markets|pension|retirement)|\bmufg\b",
    r"automic",
    r"board\s*room",
    r"registry\s+direct",
    r"\bxcend\b",
    r"tricor",
    r"vistra",
    r"\bmpms\b",
    r"equiniti|american\s+stock\s+transfer",
    r"security\s+transfer",
    r"advanced\s+share",
]

_REGISTRAR_ANY = re.compile(
    "|".join(
        f"(?:{p})"
        for p in _MODERN_REGISTRAR_PATTERNS + [p for p, _ in _HISTORICAL_REGISTRARS]
    ),
    re.I,
)


def _brand(text: str) -> str | None:
    """Canonical registrar name for a matched fragment, or None.

    Defunct registrars are resolved from the local table; everything else is
    handed to registry_tracker.canonical_registry so the names line up with what
    the daily tracker writes.
    """
    lowered = text.lower()
    for pattern, name in _HISTORICAL_REGISTRARS:
        if re.search(pattern, lowered):
            return name
    for pattern in _MODERN_REGISTRAR_PATTERNS:
        if re.search(pattern, lowered):
            return canonical_registry(text)
    return None


def _find_brands(text: str) -> list[tuple[int, str]]:
    """Every registrar mention in `text` as (offset, canonical brand)."""
    out: list[tuple[int, str]] = []
    for m in _REGISTRAR_ANY.finditer(text):
        name = _brand(m.group(0))
        if name:
            out.append((m.start(), name))
    return out


# "from X to Y" is the phrasing almost every one of these notices uses.
_FROM_TO = re.compile(
    r"\bfrom\b(?P<old>.{0,120}?)\bto\b(?P<new>.{0,120})", re.I | re.S
)
_CEASE = re.compile(
    r"(?P<who>.{0,120}?)\bwill\s+cease|(?P<who2>.{0,120}?)\bceases?\s+to\s+be", re.I | re.S
)
_APPOINT = re.compile(
    r"\b(appointed|has\s+appointed|engaged|transferred\s+to|moved\s+to)\b(?P<new>.{0,120})",
    re.I | re.S,
)


@dataclass
class Resolution:
    old: str | None = None
    new: str | None = None
    method: str = ""
    brands: list[str] = field(default_factory=list)


def resolve_registrars(text: str) -> Resolution:
    """Pull the outgoing and incoming registrar out of an announcement's text.

    Tries, in order of how much it trusts them:
      1. an explicit "from X to Y" where both sides name a known registrar
      2. "X will cease" / "X ceases to be" for the outgoing side, paired with
         whichever other brand the document mentions
      3. an appointment phrase naming the incoming registrar
      4. exactly two distinct brands in the document, taken in order of appearance
    """
    flat = " ".join(text.split())
    brands = []
    for _, name in _find_brands(flat):
        if name not in brands:
            brands.append(name)
    res = Resolution(brands=brands)

    for m in _FROM_TO.finditer(flat):
        old = _brand(m.group("old"))
        new = _brand(m.group("new"))
        if old and new and old != new:
            return Resolution(old=old, new=new, method="from_to", brands=brands)

    old_guess = None
    for m in _CEASE.finditer(flat):
        chunk = m.group("who") or m.group("who2") or ""
        old_guess = _brand(chunk)
        if old_guess:
            break

    new_guess = None
    for m in _APPOINT.finditer(flat):
        new_guess = _brand(m.group("new"))
        if new_guess:
            break

    if old_guess and new_guess and old_guess != new_guess:
        return Resolution(old=old_guess, new=new_guess, method="cease_appoint", brands=brands)

    others = [b for b in brands if b != old_guess]
    if old_guess and len(others) == 1:
        return Resolution(old=old_guess, new=others[0], method="cease_plus_one", brands=brands)
    others = [b for b in brands if b != new_guess]
    if new_guess and len(others) == 1:
        return Resolution(old=others[0], new=new_guess, method="appoint_plus_one", brands=brands)

    if len(brands) == 2:
        return Resolution(old=brands[0], new=brands[1], method="two_brands", brands=brands)
    if len(brands) == 1:
        res.new = brands[0]
        res.method = "one_brand"
    return res


# --------------------------------------------------------------------------
# Index page parsing
# --------------------------------------------------------------------------

_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S | re.I)
_TABLE_RE = re.compile(
    r"<caption[^>]*>\s*Announcements released as\s*([A-Z0-9]+)\s*</caption>(.*?)</table>",
    re.S | re.I,
)
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
_TIME_RE = re.compile(r'class="dates-time">\s*([^<]+?)\s*<')
_IDS_RE = re.compile(r"idsId=(\d+)")
# The price-sensitive column always exists; only a flagged announcement carries
# the asterisk image inside it.
_PRICE_SENS_RE = re.compile(
    r'<td[^>]*class="pricesens".*?</td>', re.S | re.I
)


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


@dataclass
class Announcement:
    code: str          # the code the announcement was released under
    query_code: str    # the code we asked about (differs after a code change)
    date: str          # ISO
    time: str
    headline: str
    ids_id: str
    price_sensitive: bool
    pages: int | None
    year: int


def _is_price_sensitive(row: str) -> bool:
    """The price-sensitive column is always present; only a flagged announcement
    carries the asterisk image inside it."""
    cell = _PRICE_SENS_RE.search(row)
    return bool(cell and "<img" in cell.group(0).lower())


def parse_index(page: str, query_code: str, year: int) -> list[Announcement]:
    """Parse one company-year index page.

    A company that has changed ticker gets one table per code it has used, and
    the ASX serves all of them when you query any one of the codes - so the
    caption, not the query, says who released a given announcement.
    """
    out: list[Announcement] = []
    for released_as, table in _TABLE_RE.findall(page):
        for row in _ROW_RE.findall(table):
            ids = _IDS_RE.search(row)
            dm = _DATE_RE.search(row)
            if not ids or not dm:
                continue
            dd, mm, yyyy = dm.groups()
            link = re.search(r"<a[^>]*idsId=\d+[^>]*>(.*?)</a>", row, re.S | re.I)
            body = _text(link.group(1)) if link else _text(row)
            # Strip the trailing "N pages 123.4KB" that lives inside the anchor.
            headline = re.sub(
                r"\s*\d+(\.\d+)?\s*(page|pages)\b.*$", "", body, flags=re.I
            ).strip()
            headline = re.sub(r"\s*\d+(\.\d+)?\s*(KB|MB)\s*$", "", headline, flags=re.I).strip()
            pages = None
            pm = re.search(r"(\d+)\s*(?:page|pages)\b", body, re.I)
            if pm:
                pages = int(pm.group(1))
            tm = _TIME_RE.search(row)
            if not headline:
                continue
            out.append(
                Announcement(
                    code=released_as.upper(),
                    query_code=query_code.upper(),
                    date=f"{yyyy}-{mm}-{dd}",
                    time=tm.group(1) if tm else "",
                    headline=headline,
                    ids_id=ids.group(1),
                    price_sensitive=_is_price_sensitive(row),
                    pages=pages,
                    year=year,
                )
            )
    return out


_PDF_URL_RE = re.compile(r'name="pdfURL"\s+value="([^"]+)"')


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------


class AsxArchiveClient:
    """Rate-limited client for the legacy announcement archive.

    `delay` is a single throttle shared across all worker threads, matching how
    registry_tracker paces itself - raising `workers` without lowering `delay`
    changes nothing.
    """

    def __init__(self, workers: int = 8, delay: float = 0.08, timeout: int = 30,
                 retries: int = 3) -> None:
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=workers, pool_maxsize=workers, max_retries=0
        )
        self.session.mount("https://", adapter)
        self.timeout = timeout
        self.retries = retries
        self._delay = delay
        self._lock = threading.Lock()
        self._next = 0.0
        self.errors: list[tuple[str, str]] = []

    def _throttle(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next = now + self._delay

    def get(self, url: str, params: dict | None = None, what: str = "") -> requests.Response | None:
        for attempt in range(self.retries):
            self._throttle()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.retries - 1:
                    self.errors.append((what, str(exc)))
                    return None
                time.sleep(2**attempt)
                continue
            if r.status_code == 200:
                return r
            # 403/429/503 are the shapes Imperva throttling takes.
            if r.status_code in (403, 429, 500, 502, 503) and attempt < self.retries - 1:
                time.sleep(2**attempt)
                continue
            self.errors.append((what, f"HTTP {r.status_code}"))
            return None
        return None

    def index(self, code: str, year: int) -> list[Announcement] | None:
        r = self.get(
            INDEX_URL,
            {"by": "asxCode", "asxCode": code, "timeframe": "Y", "year": year},
            what=f"index {code} {year}",
        )
        if r is None:
            return None
        return parse_index(r.text, code, year)

    def pdf_text(self, ids_id: str) -> tuple[str | None, str | None]:
        """Return (pdf_url, extracted text). The gate page must be fetched first."""
        gate = self.get(
            PDF_GATE_URL, {"display": "pdf", "idsId": ids_id}, what=f"gate {ids_id}"
        )
        if gate is None:
            return None, None
        m = _PDF_URL_RE.search(gate.text)
        if not m:
            self.errors.append((f"gate {ids_id}", "no pdfURL in interstitial"))
            return None, None
        url = html.unescape(m.group(1))
        r = self.get(url, what=f"pdf {ids_id}")
        if r is None:
            return url, None
        return url, extract_pdf_text(r.content)


def extract_pdf_text(blob: bytes) -> str | None:
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        log.error("pdfminer.six is required for `resolve` - pip install -r requirements.txt")
        raise SystemExit(2)
    # Many of these PDFs set the "no text extraction" metadata flag. pdfminer
    # logs a warning per file and proceeds anyway, which is what we want - but
    # at this volume the warnings drown out the progress output.
    logging.getLogger("pdfminer").setLevel(logging.ERROR)
    try:
        return extract_text(io.BytesIO(blob))
    except Exception as exc:  # a scanned or malformed PDF should not kill a run
        log.debug("pdf extract failed: %s", exc)
        return None


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS announcement (
    ids_id          TEXT PRIMARY KEY,
    code            TEXT NOT NULL,
    query_code      TEXT NOT NULL,
    date            TEXT NOT NULL,
    time            TEXT,
    headline        TEXT NOT NULL,
    classification  TEXT NOT NULL,
    price_sensitive INTEGER,
    pages           INTEGER,
    year            INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ann_code ON announcement(code, date);
CREATE INDEX IF NOT EXISTS idx_ann_class ON announcement(classification);

-- One row per (code, year) actually fetched, so a resumed scan skips work and a
-- year with no announcements is distinguishable from a year never scanned.
CREATE TABLE IF NOT EXISTS scanned (
    code        TEXT NOT NULL,
    year        INTEGER NOT NULL,
    scanned_at  TEXT NOT NULL,
    found       INTEGER NOT NULL,
    PRIMARY KEY (code, year)
);

CREATE TABLE IF NOT EXISTS resolution (
    ids_id        TEXT PRIMARY KEY REFERENCES announcement(ids_id),
    pdf_url       TEXT,
    old_registry  TEXT,
    new_registry  TEXT,
    method        TEXT,
    brands        TEXT,
    resolved_at   TEXT NOT NULL,
    ok            INTEGER NOT NULL
);
"""


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_announcements(conn: sqlite3.Connection, items: list[Announcement]) -> int:
    rows = []
    for a in items:
        cls = classify_headline(a.headline)
        if cls is None:
            continue
        rows.append(
            (a.ids_id, a.code, a.query_code, a.date, a.time, a.headline, cls,
             int(a.price_sensitive), a.pages, a.year)
        )
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO announcement "
            "(ids_id, code, query_code, date, time, headline, classification, "
            "price_sensitive, pages, year) VALUES (?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _load_codes(args: argparse.Namespace) -> list[tuple[str, int]]:
    """Return [(code, first_year)] to scan."""
    if args.codes:
        codes = [c.strip().upper() for c in args.codes.split(",") if c.strip()]
        return [(c, args.since or FIRST_YEAR) for c in codes]

    path = Path(args.universe)
    if not path.exists():
        raise SystemExit(
            f"{path} not found - run `python registry_tracker.py fetch` and "
            f"`export` first, or pass --codes"
        )
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or "").strip().upper()
            if not code:
                continue
            listed = (row.get("listing_date") or "")[:4]
            try:
                first = max(int(listed), FIRST_YEAR)
            except ValueError:
                first = FIRST_YEAR
            if args.since:
                first = max(first, args.since)
            out.append((code, first))
    if args.limit:
        out = out[: args.limit]
    return out


def cmd_scan(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    targets = _load_codes(args)
    this_year = date.today().year
    done = {(r["code"], r["year"]) for r in conn.execute("SELECT code, year FROM scanned")}

    jobs: list[tuple[str, int]] = []
    for code, first in targets:
        for year in range(first, this_year + 1):
            # Always re-scan the current year; it is still filling up.
            if (code, year) in done and year != this_year:
                continue
            jobs.append((code, year))

    if not jobs:
        print("nothing to scan (all company-years already done)")
        return 0

    print(
        f"scanning {len(targets)} codes, {len(jobs)} company-years "
        f"({args.workers} workers, {args.delay}s throttle)",
        file=sys.stderr,
    )
    client = AsxArchiveClient(workers=args.workers, delay=args.delay)
    started = time.monotonic()
    kept = failed = 0
    now = datetime.now().isoformat(timespec="seconds")

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = pool.map(lambda j: (j, client.index(*j)), jobs)
        for i, ((code, year), items) in enumerate(results, 1):
            if items is None:
                failed += 1
                continue
            kept += save_announcements(conn, items)
            conn.execute(
                "INSERT OR REPLACE INTO scanned (code, year, scanned_at, found) VALUES (?,?,?,?)",
                (code, year, now, len(items)),
            )
            if i % 250 == 0:
                conn.commit()
                rate = i / max(time.monotonic() - started, 0.001)
                eta = (len(jobs) - i) / max(rate, 0.001)
                print(
                    f"  {i}/{len(jobs)}  {rate:.1f} req/s  eta {eta/60:.0f}m  "
                    f"registry hits kept: {kept}",
                    file=sys.stderr,
                )
    conn.commit()
    elapsed = time.monotonic() - started
    print(
        f"scanned {len(jobs)} company-years in {elapsed/60:.1f}m "
        f"({len(jobs)/max(elapsed,0.001):.1f} req/s); "
        f"{kept} registry-related announcements; {failed} failed fetches",
        file=sys.stderr,
    )
    for what, err in client.errors[:10]:
        print(f"  error: {what}: {err}", file=sys.stderr)
    _print_class_summary(conn)
    return 0


def _print_class_summary(conn: sqlite3.Connection) -> None:
    print("\nregistry-related announcements by class:")
    for row in conn.execute(
        "SELECT classification, COUNT(*) n, COUNT(DISTINCT code) c "
        "FROM announcement GROUP BY classification ORDER BY n DESC"
    ):
        print(f"  {row['classification']:16s} {row['n']:6d} announcements  {row['c']:5d} codes")


def cmd_resolve(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    # Address notices are opened by default. Measured on a 60-notice sample,
    # 6.7% of them turn out to be real registrar switches - companies do lodge a
    # genuine change under "Details of Share Registry address" - and since the
    # PDF is the arbiter anyway, the headline only decides ordering, not
    # eligibility. --skip-address buys back ~35% of the fetches if that trade is
    # not worth it.
    wanted = [CLASS_PROVIDER, CLASS_OTHER]
    if not args.skip_address:
        wanted.append(CLASS_ADDRESS)
    placeholders = ",".join("?" * len(wanted))
    sql = (
        f"SELECT a.* FROM announcement a "
        f"LEFT JOIN resolution r ON r.ids_id = a.ids_id "
        f"WHERE a.classification IN ({placeholders}) AND r.ids_id IS NULL "
        f"ORDER BY CASE a.classification WHEN '{CLASS_PROVIDER}' THEN 0 "
        f"WHEN '{CLASS_OTHER}' THEN 1 ELSE 2 END, a.date DESC"
    )
    rows = list(conn.execute(sql, wanted))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("nothing to resolve")
        return 0

    print(f"resolving {len(rows)} announcement PDFs", file=sys.stderr)
    client = AsxArchiveClient(workers=args.workers, delay=args.delay)
    now = datetime.now().isoformat(timespec="seconds")
    started = time.monotonic()
    ok = 0

    def work(row: sqlite3.Row):
        url, text = client.pdf_text(row["ids_id"])
        return row, url, text

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (row, url, text) in enumerate(pool.map(work, rows), 1):
            if text:
                res = resolve_registrars(text)
                ok += 1
            else:
                res = Resolution()
            conn.execute(
                "INSERT OR REPLACE INTO resolution "
                "(ids_id, pdf_url, old_registry, new_registry, method, brands, resolved_at, ok) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    row["ids_id"], url, res.old, res.new, res.method,
                    ",".join(res.brands), now, 1 if text else 0,
                ),
            )
            if i % 50 == 0:
                conn.commit()
                rate = i / max(time.monotonic() - started, 0.001)
                print(f"  {i}/{len(rows)}  {rate:.1f}/s", file=sys.stderr)
    conn.commit()
    print(f"resolved {ok}/{len(rows)} PDFs", file=sys.stderr)
    for what, err in client.errors[:10]:
        print(f"  error: {what}: {err}", file=sys.stderr)
    return cmd_changes(args)


_CHANGES_SQL = """
SELECT a.code, a.date, a.headline, a.classification,
       r.old_registry, r.new_registry, r.method, r.brands, r.pdf_url
FROM announcement a
JOIN resolution r ON r.ids_id = a.ids_id
WHERE r.old_registry IS NOT NULL AND r.new_registry IS NOT NULL
  AND r.old_registry <> r.new_registry
ORDER BY a.date DESC
"""


def cmd_changes(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = list(conn.execute(_CHANGES_SQL))
    if not rows:
        print("no resolved registrar changes yet - run `resolve`")
        return 0
    print(f"\n{len(rows)} resolved registrar changes across "
          f"{len({r['code'] for r in rows})} codes\n")
    print(f"{'CODE':6s} {'DATE':11s} {'FROM':28s} {'TO':28s} METHOD")
    for r in rows[: args.limit or len(rows)]:
        print(
            f"{r['code']:6s} {r['date']:11s} {(r['old_registry'] or ''):28s} "
            f"{(r['new_registry'] or ''):28s} {r['method']}"
        )
    return 0


def cmd_timeline(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    code = args.code.upper()
    rows = list(
        conn.execute(
            "SELECT a.*, r.old_registry, r.new_registry, r.method, r.pdf_url "
            "FROM announcement a LEFT JOIN resolution r ON r.ids_id = a.ids_id "
            "WHERE a.code = ? OR a.query_code = ? ORDER BY a.date",
            (code, code),
        )
    )
    if not rows:
        print(f"no registry-related announcements recorded for {code}")
        return 0
    print(f"\n{code} - {len(rows)} registry-related announcements\n")
    for r in rows:
        line = f"  {r['date']}  [{r['classification']:14s}] {r['headline']}"
        if r["old_registry"] and r["new_registry"]:
            line += f"\n{'':14s}-> {r['old_registry']} => {r['new_registry']} ({r['method']})"
        print(line)
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    rows = list(conn.execute(_CHANGES_SQL))
    out = Path(args.path)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(
            ["code", "announced_on", "old_registry", "new_registry",
             "headline", "classification", "method", "pdf_url"]
        )
        for r in rows:
            w.writerow(
                [r["code"], r["date"], r["old_registry"], r["new_registry"],
                 r["headline"], r["classification"], r["method"], r["pdf_url"]]
            )
    print(f"wrote {len(rows)} rows to {out}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    scanned = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT code) c, MIN(year) y0, MAX(year) y1 FROM scanned"
    ).fetchone()
    print(
        f"scanned: {scanned['n']} company-years across {scanned['c']} codes "
        f"({scanned['y0']}-{scanned['y1']})"
    )
    _print_class_summary(conn)
    res = conn.execute(
        "SELECT COUNT(*) n, SUM(ok) ok, "
        "SUM(old_registry IS NOT NULL AND new_registry IS NOT NULL) pairs FROM resolution"
    ).fetchone()
    print(
        f"\nPDFs read: {res['n'] or 0} ({res['ok'] or 0} extracted); "
        f"{res['pairs'] or 0} with both old and new registrar"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--db", type=Path, default=DB_PATH)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="index historical announcements")
    s.add_argument("--codes", help="comma-separated ASX codes")
    s.add_argument("--universe", default="data/asx_registries.csv",
                   help="CSV of codes to scan (default: the tracker's export)")
    s.add_argument("--limit", type=int, help="only the first N codes")
    s.add_argument("--since", type=int, help=f"earliest year (default {FIRST_YEAR} or listing)")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--delay", type=float, default=0.08)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("resolve", help="read candidate PDFs and extract old/new registrar")
    s.add_argument("--limit", type=int)
    s.add_argument("--skip-address", action="store_true",
                   help="do not open address-change notices (~35%% fewer fetches, "
                        "but misses the ~7%% of them that are real switches)")
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--delay", type=float, default=0.12)
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser("changes", help="resolved registrar switches")
    s.add_argument("--limit", type=int)
    s.set_defaults(func=cmd_changes)

    s = sub.add_parser("timeline", help="one ticker's registry announcements")
    s.add_argument("code")
    s.set_defaults(func=cmd_timeline)

    s = sub.add_parser("export", help="write resolved changes to CSV")
    s.add_argument("path")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("status", help="what has been scanned and resolved")
    s.set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
