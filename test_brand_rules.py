"""Regression tests for the registrar-name rules in announcement_history.

No network and nothing on disk: every fixture is a fragment of a real
announcement, trimmed to the sentence the rule turns on, and the one test class
that needs a database builds it in memory. Run with `python test_brand_rules.py`.
"""

import sqlite3
import unittest

from announcement_history import (
    CLASS_ADDRESS,
    CLASS_OTHER,
    CLASS_PROVIDER,
    SCHEMA,
    _find_brands,
    classify_headline,
    derive_ticker_changes,
    resolve_registrars,
    strip_watermark,
)


def brands(text: str) -> list[str]:
    return list(dict.fromkeys(name for _, name in _find_brands(text)))


# How pdfminer reads the ASX's rotated "For personal use only" stamp back: one
# character per line, and in an order that is neither the phrase nor a clean
# reversal of it. Verbatim from Champion Iron's 12 Jan 2024 notice.
STAMP = "l\n\ny\nn\no\ne\ns\nu\n\nl\n\na\nn\no\ns\nr\ne\np\nr\no\nF"


class Headlines(unittest.TestCase):
    """What is worth opening. A headline never matched is a change never found."""

    def test_share_register_is_the_same_notice_as_share_registry(self):
        # Macquarie Group, 11 Aug 2020 - the move to Link. Matches no spelling
        # of "registry", so before this it was never opened.
        self.assertEqual(
            classify_headline("Change of share register notification"), CLASS_PROVIDER
        )
        self.assertEqual(classify_headline("Transfer of share register"), CLASS_PROVIDER)
        self.assertEqual(classify_headline("Change of Share Registry"), CLASS_PROVIDER)

    def test_address_still_wins_over_provider(self):
        self.assertEqual(
            classify_headline("Change of share register address"), CLASS_ADDRESS
        )

    def test_unqualified_register_words_are_not_this(self):
        # A bare "register" is the register of members, a registered office, or
        # a substantial holding - the qualifier is what makes it the share
        # register.
        for headline in (
            "Register of members",
            "Change of registered office",
            "Becoming a substantial holder",
            "Notification of registered address",
            "Cleansing statement and register update",
        ):
            self.assertIsNone(classify_headline(headline), headline)

    def test_registry_industry_talking_about_itself_is_excluded(self):
        self.assertIsNone(classify_headline("Computershare acquires US Transfer Agent"))
        self.assertIsNone(classify_headline("Sale of registry business"))

    def test_unclear_registry_headline_falls_to_other(self):
        self.assertEqual(classify_headline("Share register update"), CLASS_OTHER)


class AmbiguousBrands(unittest.TestCase):
    """Names that are also ordinary words or other people's firms."""

    def test_boardroom_as_a_meeting_venue_is_not_a_registrar(self):
        # Resource Star, 23 Oct 2009 notice of AGM. The document names no
        # registrar at all, so before the context rule this venue was the only
        # brand in it - the one shape `backfill` trusts without question.
        text = (
            "NOTICE OF ANNUAL GENERAL MEETING TIME: 11.00am (WST) DATE: 23 November 2009 "
            "PLACE: The Boardroom Nissen Kestel Harford Level 2, Spectrum 100 Railway Road "
            "PERTH. Shares will be taken to be held by those who are registered holders "
            "at 5.00pm on 21 November 2009."
        )
        self.assertEqual(brands(text), [])

    def test_registered_holder_is_not_registry_context(self):
        # "registered" must not satisfy the context rule - it is boilerplate in
        # exactly the meeting notices the rule exists to exclude.
        self.assertEqual(brands("held in the Boardroom by a registered holder"), [])

    def test_register_is_maintained_by_counts_as_context(self):
        # Live Verdure, 11 Aug 2017. This is how a notice names the registrar it
        # is leaving, and "register" is the only registry word in the sentence.
        text = (
            "Lodgement of documentation by member organisations, security holders "
            "and other interested parties must be made at the new address from "
            "Monday, 2 October 2017. Our register is currently maintained by "
            "Boardroom Pty Limited."
        )
        self.assertEqual(brands(text), ["Boardroom"])

    def test_boardroom_named_as_the_registry_still_counts(self):
        # Trustees Australia, 22 Nov 2016 notice of EGM. Boardroom really did
        # hold this register, and the tightened rule must not lose it.
        text = (
            "Securities Registry means Boardroom Pty Limited ABN 14 003 209 836. "
            "If you wish to change your address for this holding, please contact "
            "Boardroom Pty Limited at the address and phone number below."
        )
        self.assertEqual(brands(text), ["Boardroom"])

    def test_solicitors_in_a_corporate_directory_are_not_the_registry(self):
        # Intiger Group's 2016 annual report. Steinepreis Paganin is the law
        # firm GG Registry operated out of, and it is listed one line above the
        # share registry, which is why the brand is matched by name only.
        text = (
            "CORPORATE DIRECTORY Solicitors Steinepreis Paganin Level 4, The Read "
            "Buildings, 16 Milligan Street, Perth WA 6000 Share Registry Security "
            "Transfer Australia Pty Ltd 770 Canning Highway, Applecross WA 6153"
        )
        self.assertEqual(brands(text), ["Security Transfer Australia"])

    def test_gg_registry_named_outright_still_counts(self):
        text = "The Company's share registry is GG Registry Pty Ltd."
        self.assertEqual(brands(text), ["GG Registry"])

    def test_law_firm_away_from_the_registry_entry_is_not_a_registrar(self):
        text = (
            "Auditors BDO Audit Pty Ltd Level 11, 1 Margaret Street Sydney NSW 2000 "
            "Solicitors Gadens Lawyers Level 16, 77 Castlereagh Street Sydney NSW 2000"
        )
        self.assertEqual(brands(text), [])


class UnambiguousBrands(unittest.TestCase):
    """Registrars whose name alone is the evidence - no context needed."""

    def test_bare_mention_is_enough(self):
        self.assertEqual(brands("Please contact Computershare on 1300 556 161."), ["Computershare"])

    def test_mufg_letterhead_domain_is_one_brand_not_two(self):
        text = "Visit https://au.investorcentre.mpms.mufg.com or email support@cm.mpms.mufg.com"
        self.assertEqual(brands(text), ["MUFG Corporate Markets"])


class DefinedTerms(unittest.TestCase):
    """Short forms a document declares for itself."""

    # Legend Mining, 1 Mar 2024. Trimmed but otherwise as lodged.
    LEG = (
        "Legend Mining Limited (ASX: LEG) (the Company) advises that, following the "
        "completion of Automic Pty Ltd's (“Automic”) acquisition of its share "
        "registry provider, Advanced Share Registry Limited (“Advanced”) late "
        "last year, as of Monday, 4 March 2024, the provider of shareholder registry "
        "services for the Company will change from Advanced to Automic."
    )

    def test_short_form_gets_the_direction_right(self):
        res = resolve_registrars(self.LEG)
        self.assertEqual(res.old, "Advanced Share Registry")
        self.assertEqual(res.new, "Automic")
        self.assertEqual(res.method, "from_to")

    def test_alias_that_already_names_a_registrar_is_not_redefined(self):
        from announcement_history import _defined_terms
        self.assertEqual(_defined_terms(self.LEG), {"advanced": "Advanced Share Registry"})

    def test_lead_in_naming_two_registrars_defines_nothing(self):
        # The expansion must name exactly one registrar, or the alias would take
        # whichever brand the greedy match happened to swallow first.
        from announcement_history import _defined_terms
        text = 'The register moved from Computershare to Automic Pty Ltd ("Automic").'
        self.assertEqual(_defined_terms(text), {})
        res = resolve_registrars(text)
        self.assertEqual((res.old, res.new), ("Computershare", "Automic"))


class Watermark(unittest.TestCase):
    """The stamp the ASX adds, which is not part of the lodged document."""

    def test_a_scan_carrying_only_the_stamp_has_no_text_left(self):
        # Champion Iron, 12 Jan 2024. The page is an image; the stamp is the
        # only text on it. Left in, the scan looks like a letter that was read
        # and named no registrar, and `ok = 1` stops meaning anything.
        self.assertEqual(strip_watermark(STAMP + "\n\n \n \n\x0c").strip(), "")

    def test_bullets_are_not_text_either(self):
        # NWL, 14 Apr 2025: two stamps and the bullet glyphs of a scanned list.
        left = strip_watermark(f"{STAMP}\n\n• • • • • •\n\n{STAMP}")
        self.assertFalse(any(c.isalpha() for c in left))

    def test_stamp_does_not_push_a_registrar_out_of_its_context(self):
        # The stamp lands wherever the page's text order puts it, routinely
        # mid-sentence, and it is 35 characters of a 120-character window. Here
        # it is the only thing between "share register" and the registrar that
        # word is there to vouch for, and Boardroom stops counting as one.
        lead = (
            "Our share register is currently maintained by the provider named "
            "below, whose contact details for all holder enquiries are "
        )
        tail = "Boardroom Pty Limited, Level 12, 225 George Street, Sydney."
        squash = lambda t: " ".join(t.split())
        self.assertEqual(brands(squash(lead + tail)), ["Boardroom"])
        self.assertEqual(brands(squash(f"{lead}\n\n{STAMP}\n\n{tail}")), [])
        self.assertEqual(
            brands(squash(strip_watermark(f"{lead}\n\n{STAMP}\n\n{tail}"))), ["Boardroom"]
        )

    def test_running_text_is_left_alone(self):
        for text in ("a b c d e", "Level 2, 100 Railway Road", ""):
            self.assertEqual(strip_watermark(text), text)


class Resolution(unittest.TestCase):
    def test_from_to_reads_both_sides(self):
        res = resolve_registrars(
            "The Company advises that its share registry will transfer from "
            "Computershare Investor Services Pty Limited to Automic Pty Ltd, "
            "effective 15 March 2024."
        )
        self.assertEqual((res.old, res.new, res.method), ("Computershare", "Automic", "from_to"))

    def test_venue_does_not_become_the_incoming_registrar(self):
        res = resolve_registrars(
            "Notice of Annual General Meeting to be held at The Boardroom, "
            "Nissen Kestel Harford, Level 2 Spectrum, 100 Railway Road."
        )
        self.assertIsNone(res.old)
        self.assertIsNone(res.new)


class TickerChanges(unittest.TestCase):
    """Renames read back out of the announcements, in memory - no file on disk."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def add(self, ids_id, code, query_code, date):
        self.conn.execute(
            "INSERT INTO announcement (ids_id, code, query_code, date, headline, "
            "classification, year) VALUES (?,?,?,?,?,?,?)",
            (ids_id, code, query_code, date, "Change of Share Registry",
             CLASS_PROVIDER, int(date[:4])),
        )

    def derive(self, current_codes=frozenset()):
        derive_ticker_changes(self.conn, set(current_codes), "2026-08-13T00:00:00")
        return {
            (r["old_code"], r["current_code"]): r
            for r in self.conn.execute("SELECT * FROM ticker_change")
        }

    def test_a_differing_released_under_code_is_a_rename(self):
        # Advance Metals lodged under ASE in 2025; the archive serves it back
        # when asked about VMS, which is what the company trades as now.
        self.add("1", "ASE", "VMS", "2025-07-14")
        self.add("2", "VMS", "VMS", "2026-03-02")
        row = self.derive()[("ASE", "VMS")]
        self.assertEqual(row["old_last_seen"], "2025-07-14")
        self.assertEqual(row["current_first_seen"], "2026-03-02")
        self.assertEqual(row["announcements"], 1)

    def test_a_company_that_never_renamed_produces_no_row(self):
        self.add("1", "BHP", "BHP", "2020-01-01")
        self.assertEqual(self.derive(), {})

    def test_the_bounds_are_the_widest_the_announcements_support(self):
        self.add("1", "TI1", "AEU", "2019-05-05")
        self.add("2", "TI1", "AEU", "2021-02-19")
        self.add("3", "AEU", "AEU", "2022-11-01")
        row = self.derive()[("TI1", "AEU")]
        self.assertEqual(
            (row["old_last_seen"], row["current_first_seen"], row["announcements"]),
            ("2021-02-19", "2022-11-01", 2),
        )

    def test_an_old_code_someone_else_now_trades_under_is_flagged(self):
        # AEU was Australian Education Trust's before CQE; it belongs to a
        # different company today, so a join on the old code lands on a stranger.
        self.add("1", "AEU", "CQE", "2009-06-26")
        self.add("2", "TI1", "AEU", "2021-02-19")
        rows = self.derive(current_codes={"AEU", "CQE"})
        self.assertEqual(rows[("AEU", "CQE")]["old_code_relisted"], 1)
        self.assertEqual(rows[("TI1", "AEU")]["old_code_relisted"], 0)

    def test_rebuilding_retracts_a_pair_the_evidence_no_longer_supports(self):
        self.add("1", "IAM", "TAU", "2017-06-13")
        self.assertIn(("IAM", "TAU"), self.derive())
        # A rescan reassigns that announcement to whoever holds the code now.
        self.conn.execute("UPDATE announcement SET query_code = 'IAM' WHERE ids_id = '1'")
        self.assertEqual(self.derive(), {})

    def test_rows_from_before_query_code_existed_are_not_renames(self):
        self.add("1", "ECS", "", "2026-07-30")
        self.assertEqual(self.derive(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
