"""Regression tests for the registrar-name rules in announcement_history.

No network and no database: every fixture is a fragment of a real announcement,
trimmed to the sentence the rule turns on. Run with `python test_brand_rules.py`.
"""

import unittest

from announcement_history import (
    CLASS_ADDRESS,
    CLASS_OTHER,
    CLASS_PROVIDER,
    _find_brands,
    classify_headline,
    resolve_registrars,
)


def brands(text: str) -> list[str]:
    return list(dict.fromkeys(name for _, name in _find_brands(text)))


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
