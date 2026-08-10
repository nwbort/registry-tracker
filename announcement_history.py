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

Plenty of notices name only the registrar the company is moving to. That leaves a
change with one end, which is no change at all as far as `changes` is concerned,
so `backfill` goes looking for the other end in the company's other filings - a
proxy form or annual report lodged either side of the notice prints whoever held
the register at the time.

Usage:
    python announcement_history.py scan --codes ECS,BHP   # index announcements
    python announcement_history.py scan --limit 50        # sample of the market
    python announcement_history.py scan                   # whole market (~30k requests)
    python announcement_history.py resolve                # read candidate PDFs
    python announcement_history.py backfill               # name the unnamed side
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
from datetime import date, datetime, timedelta
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

# The noun for the thing that changes. "Transfer agent" is the North-American
# name for a registrar, and companies with a second listing in Toronto or New
# York use it in place of - or alongside - "share registry".
_REGISTRY_NOUN = r"(?:share\s*)?registr(?:y|ies|ars?)\b|transfer\s+agents?\b"

# Anything matching this is about a share registry in some way. Deliberately
# broad - narrowing happens below, and a headline we never look at is a change
# we can never find.
#
# The last alternative is the one that does not name a registry at all. A
# company that is switching registrar on two exchanges at once tends to headline
# the notice for the market rather than for the document: AuMEGA Metals (AAM)
# moved from Automic to Computershare on 8 Dec 2025 under "AuMEGA Metals
# Announces Capital Market Changes", with "registry and transfer agent" appearing
# only in the body. Only the "<capital market(s)> change/update" shape is
# admitted, not "capital markets" on its own - that phrase is otherwise all
# investor days and debt raisings, and this net is paid for one PDF at a time.
_REGISTRY_HEADLINE = re.compile(
    rf"{_REGISTRY_NOUN}|registry\s*services|"
    r"capital\s+market\w*\s+(?:change|update)",
    re.I,
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
    rf"change\s+(?:of|in)\s+(?:the\s+)?(?:{_REGISTRY_NOUN})|"
    rf"(?:new|appointment\s+of|transfer\s+of)\s+(?:a\s+)?(?:{_REGISTRY_NOUN})|"
    r"registr(?:y|ies|ars?)\s+(?:change|transfer|appointment)|"
    r"share\s+registry\s+(?:service\s+)?provider|"
    # "<Company> reappoints X as registrars" - the appointment verb and the
    # registry word sit either side of the incoming registrar's name.
    r"\bre-?appoints?\b|"
    rf"\bappoints?\b.{{0,60}}?(?:{_REGISTRY_NOUN})|"
    rf"(?:{_REGISTRY_NOUN}).{{0,60}}?\bappoint",
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

# Two names for one company, collapsed to one brand. MPMS is MUFG Pension &
# Market Services - the group MUFG Corporate Markets (itself the renamed Link
# Market Services) was rebranded to. Do NOT split these back apart: the token
# hardly ever arrives as a company name, it arrives inside the letterhead
# domain, which every MUFG notice carries:
#     https://au.investorcentre.mpms.mufg.com
#     support@cm.mpms.mufg.com
# So a routine "we have moved to MUFG" letter names both spellings, and treating
# them as two registrars makes the resolver read a rebrand as a company changing
# provider - in whichever direction the two names happen to fall in the text.
# That produced seven false switches before this alias existed.
#
# The alias has to live here rather than in registry_tracker._CANONICAL_PATTERNS,
# which still maps /\bmpms\b/ to a distinct "MPMS" brand: that pattern also
# matches "mps market" and "market place", which need not be MUFG at all, and
# the daily tracker's change alerts key off those names.
_BRAND_ALIASES: dict[str, str] = {
    "MPMS": "MUFG Corporate Markets",
}


def _brand(text: str) -> str | None:
    """Canonical registrar name for a matched fragment, or None.

    Defunct registrars are resolved from the local table; everything else is
    handed to registry_tracker.canonical_registry so the names line up with what
    the daily tracker writes, then run through _BRAND_ALIASES so two names for
    the same company do not look like two registrars.
    """
    lowered = text.lower()
    for pattern, name in _HISTORICAL_REGISTRARS:
        if re.search(pattern, lowered):
            return name
    for pattern in _MODERN_REGISTRAR_PATTERNS:
        if re.search(pattern, lowered):
            name = canonical_registry(text)
            return _BRAND_ALIASES.get(name, name)
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
      5. a single brand, placed on whichever side the surrounding phrasing puts it

    Rule 5 leaves the other side NULL, which is not a change `changes` can use -
    `backfill` is what fills it in, from documents outside the notice.
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
        # One registrar named and nothing to pair it with. Which side of the
        # change it sits on is whatever the surrounding sentence says. Treating
        # it as the incoming one is right far more often - a company announcing
        # a move prints the new registrar's contact details and never says who
        # it left, which is the shape of Siren Gold's 23 Mar 2026 notice ("the
        # share registry of the Company will be transferred to Computershare",
        # and Automic is not mentioned at all) - but reading a "X will cease to
        # act as registrar" notice that way inverts the change, so a cease
        # phrase naming the only brand wins.
        if old_guess == brands[0] and new_guess is None:
            res.old = brands[0]
            res.method = "one_brand_cease"
        else:
            res.new = brands[0]
            res.method = "one_brand"
    return res


# --------------------------------------------------------------------------
# Recovering the side a notice does not name
# --------------------------------------------------------------------------

# A one-sided notice names the registrar the company is moving to (or, rarely,
# the one it is leaving) and stops there, which leaves `resolve` with half a
# change and `changes` - which needs both ends - with none. The missing half is
# almost always written down somewhere else in the same company's announcement
# stream: routine documents that are produced *by* the registry, or that have to
# print its address for shareholders to act on, name whoever held the register
# on the day they were lodged.
#
# So the outgoing registrar is read out of the newest such document lodged
# before the notice, and the incoming one out of the oldest lodged well after
# it. This is evidence, not inference: if the document names the same registrar
# the notice does, then the register did not move and nothing is written - which
# is exactly what should happen for the address notices that slip into
# CLASS_PROVIDER on a headline typo ("Change of Registry Addresss").
_CORROBORATING = re.compile(
    r"proxy\s+form|notice\s+of\s+(?:annual\s+general|general|extraordinary)\s+meeting|"
    r"letter\s+to\s+(?:share|unit|security)\s*holders|"
    r"(?:dividend|distribution)\s+reinvestment|"
    r"annual\s+report",
    re.I,
)

# An announcement whose headline says it is about the registry is not
# independent evidence of who the registry was - it is the notice itself, or its
# corrections and reminders.
_NOT_CORROBORATING = _REGISTRY_HEADLINE

# A switch is announced before it takes effect ("effective Monday, 30 March"),
# so a document lodged in the days after the notice can still have been produced
# by the outgoing registrar. Nothing inside this window is read as evidence of
# the incoming one.
_EFFECTIVE_LAG_DAYS = 45


def corroborating(headline: str) -> bool:
    """Would this announcement be expected to name the company's registrar?"""
    if not headline:
        return False
    text = " ".join(headline.split())
    return bool(_CORROBORATING.search(text) and not _NOT_CORROBORATING.search(text))


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
        text = extract_text(io.BytesIO(blob))
        # An image-only scan still yields a page-separator form feed, which is
        # truthy but carries no text. Treat that as "not extractable" so the
        # `ok` column distinguishes a scan from a document we actually read.
        return text if text and text.strip() else None
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
    ok            INTEGER NOT NULL,
    -- ids_id of the unrelated announcement the missing side was read out of,
    -- when `backfill` supplied it. NULL means both sides came from the notice.
    backfilled_from TEXT
);

-- Documents opened by `backfill` only to see which registrar they name. These
-- are ordinary announcements (proxy forms, annual reports) that say nothing
-- about a registry change, so they have no place in `announcement`; the table
-- exists so a second backfill run does not re-fetch them.
CREATE TABLE IF NOT EXISTS probe (
    ids_id     TEXT PRIMARY KEY,
    code       TEXT NOT NULL,
    date       TEXT NOT NULL,
    headline   TEXT,
    pdf_url    TEXT,
    brands     TEXT,
    probed_at  TEXT NOT NULL,
    ok         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_probe_code ON probe(code, date);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently leaves an existing table alone, so
    without this a database from an earlier version keeps working right up
    until the first INSERT that mentions a new column.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(announcement)")}
    if "price_sensitive" not in have:
        conn.execute("ALTER TABLE announcement ADD COLUMN price_sensitive INTEGER")
        conn.commit()
    have = {row["name"] for row in conn.execute("PRAGMA table_info(resolution)")}
    if "backfilled_from" not in have:
        conn.execute("ALTER TABLE resolution ADD COLUMN backfilled_from TEXT")
        conn.commit()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
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
    if args.rescan:
        # `scanned` records that a company-year was fetched, not what
        # classify_headline made of it, so widening the headline net leaves
        # every already-scanned year holding the old verdict. Re-index them.
        done = set()

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
    params = list(wanted)
    # A stored resolution is normally final - the PDF does not change, so
    # re-reading it is a wasted request. It stops being final when the
    # extraction rules do: --reresolve re-reads the documents an old rule
    # decided, and since the row is rewritten from scratch, any side `backfill`
    # had supplied for it goes too and has to be earned again.
    redo = ""
    if args.reresolve:
        redo = " OR r.method = ?"
        params.append(args.reresolve)
    sql = (
        f"SELECT a.* FROM announcement a "
        f"LEFT JOIN resolution r ON r.ids_id = a.ids_id "
        f"WHERE a.classification IN ({placeholders}) "
        f"AND (r.ids_id IS NULL{redo}) "
        f"ORDER BY CASE a.classification WHEN '{CLASS_PROVIDER}' THEN 0 "
        f"WHEN '{CLASS_OTHER}' THEN 1 ELSE 2 END, a.date DESC"
    )
    rows = list(conn.execute(sql, params))
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


# A notice that named only one registrar. `method` is not tested: what makes a
# row half-resolved is that one column is filled and the other is not, however
# the resolver got there.
_ONE_SIDED_SQL = """
SELECT a.code, a.date, a.headline, a.classification,
       r.ids_id, r.old_registry, r.new_registry, r.method
FROM announcement a
JOIN resolution r ON r.ids_id = a.ids_id
WHERE r.ok = 1 AND r.backfilled_from IS NULL
  AND (r.old_registry IS NULL) <> (r.new_registry IS NULL)
  AND a.classification IN ({placeholders})
ORDER BY a.date DESC
"""


def _probe_pdf(
    client: AsxArchiveClient, conn: sqlite3.Connection, ann: Announcement, now: str
) -> list[str]:
    """Registrars named by one announcement, reading a cached probe if there is one."""
    cached = conn.execute(
        "SELECT brands, ok FROM probe WHERE ids_id = ?", (ann.ids_id,)
    ).fetchone()
    if cached is not None:
        return [b for b in (cached["brands"] or "").split(",") if b]

    url, text = client.pdf_text(ann.ids_id)
    brands: list[str] = []
    for _, name in _find_brands(" ".join((text or "").split())):
        if name not in brands:
            brands.append(name)
    conn.execute(
        "INSERT OR REPLACE INTO probe "
        "(ids_id, code, date, headline, pdf_url, brands, probed_at, ok) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ann.ids_id, ann.code, ann.date, ann.headline, url,
         ",".join(brands), now, 1 if text else 0),
    )
    return brands


def _candidate_documents(
    client: AsxArchiveClient, code: str, on_date: str, before: bool
) -> list[Announcement]:
    """Corroborating announcements on one side of `on_date`, nearest to it first.

    Only the announcement's own year and the neighbouring one on that side are
    indexed. A company lodges an annual report and a notice of meeting every
    year, so two years is already generous, and each extra year is a request
    spent on a company that is unlikely to have a proxy form hiding in it.
    """
    year = int(on_date[:4])
    years = (year - 1, year) if before else (year, year + 1)
    cutoff = on_date
    if not before:
        cutoff = (
            date.fromisoformat(on_date) + timedelta(days=_EFFECTIVE_LAG_DAYS)
        ).isoformat()

    out: list[Announcement] = []
    for y in years:
        if y > date.today().year:
            continue
        for a in client.index(code, y) or []:
            if (a.date < cutoff) != before:
                continue
            if corroborating(a.headline):
                out.append(a)
    out.sort(key=lambda a: a.date, reverse=before)
    return out


def cmd_backfill(args: argparse.Namespace) -> int:
    conn = connect(args.db)
    # Address notices are excluded by default. They are one-sided for the honest
    # reason - the registrar moved office, so there is only ever one to name -
    # and probing them would pair each with whatever the register looked like
    # before, turning a non-event into a change.
    wanted = [CLASS_PROVIDER]
    if args.include_other:
        wanted.append(CLASS_OTHER)
    placeholders = ",".join("?" * len(wanted))
    rows = list(conn.execute(_ONE_SIDED_SQL.format(placeholders=placeholders), wanted))
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("nothing to backfill")
        return 0

    print(
        f"backfilling {len(rows)} one-sided notices "
        f"(up to {args.probes} documents each)",
        file=sys.stderr,
    )
    client = AsxArchiveClient(workers=args.workers, delay=args.delay)
    now = datetime.now().isoformat(timespec="seconds")
    filled = unchanged = exhausted = 0

    def work(row: sqlite3.Row):
        before = row["old_registry"] is None
        known = row["new_registry"] if before else row["old_registry"]
        cands = _candidate_documents(client, row["code"], row["date"], before)
        return row, before, known, cands[: args.probes]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for i, (row, before, known, cands) in enumerate(pool.map(work, rows), 1):
            found = None
            source = None
            same = False
            for ann in cands:
                brands = _probe_pdf(client, conn, ann, now)
                if len(brands) != 1:
                    # Nothing, or a document that names two registrars and so
                    # cannot say which one held the register.
                    continue
                if brands[0] == known:
                    # The register did not move: this was not a switch.
                    same = True
                    break
                found, source = brands[0], ann.ids_id
                break

            if found:
                column = "old_registry" if before else "new_registry"
                conn.execute(
                    f"UPDATE resolution SET {column} = ?, method = ?, "
                    f"backfilled_from = ? WHERE ids_id = ?",
                    (found, f"{row['method']}+{'prior' if before else 'next'}_doc",
                     source, row["ids_id"]),
                )
                filled += 1
                if args.verbose:
                    a, b = (found, known) if before else (known, found)
                    print(f"  {row['code']} {row['date']}: {a} => {b}", file=sys.stderr)
            elif same:
                unchanged += 1
            else:
                exhausted += 1
            if i % 25 == 0:
                conn.commit()
                print(f"  {i}/{len(rows)}", file=sys.stderr)
    conn.commit()
    print(
        f"backfilled {filled} of {len(rows)}; {unchanged} showed the same "
        f"registrar either side (no change); {exhausted} found no evidence",
        file=sys.stderr,
    )
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
    bf = conn.execute(
        "SELECT COUNT(*) n, SUM(backfilled_from IS NOT NULL) done, "
        "SUM(ok = 1 AND (old_registry IS NULL) <> (new_registry IS NULL)) half "
        "FROM resolution"
    ).fetchone()
    probes = conn.execute("SELECT COUNT(*) n FROM probe").fetchone()
    print(
        f"one-sided notices still missing a side: {bf['half'] or 0}; "
        f"{bf['done'] or 0} completed by backfill from {probes['n'] or 0} probed documents"
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
    s.add_argument("--rescan", action="store_true",
                   help="re-index company-years already scanned; needed after the "
                        "headline rules change, since a stored scan keeps the old verdict")
    s.add_argument("--workers", type=int, default=8)
    s.add_argument("--delay", type=float, default=0.08)
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("resolve", help="read candidate PDFs and extract old/new registrar")
    s.add_argument("--limit", type=int)
    s.add_argument("--skip-address", action="store_true",
                   help="do not open address-change notices (~35%% fewer fetches, "
                        "but misses the ~7%% of them that are real switches)")
    s.add_argument("--reresolve", metavar="METHOD",
                   help="also re-read PDFs whose stored resolution used METHOD "
                        "(e.g. one_brand), after a change to the extraction rules")
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--delay", type=float, default=0.12)
    s.set_defaults(func=cmd_resolve)

    s = sub.add_parser(
        "backfill",
        help="name the missing side of a one-sided notice from other announcements",
    )
    s.add_argument("--limit", type=int)
    s.add_argument("--probes", type=int, default=3,
                   help="documents to open per notice before giving up (default 3)")
    s.add_argument("--include-other", action="store_true",
                   help=f"also backfill {CLASS_OTHER} notices, not just {CLASS_PROVIDER}")
    s.add_argument("--workers", type=int, default=6)
    s.add_argument("--delay", type=float, default=0.12)
    s.set_defaults(func=cmd_backfill)

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
