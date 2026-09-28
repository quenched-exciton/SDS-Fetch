"""Tests for reading hazard statements and the SDS date from SDS text (no internet needed)."""

from datetime import date

from sds_fetch.sds_info import (
    describe_codes,
    find_hazard_codes,
    find_sds_date,
    hazard_section,
    parse_date,
    read_sds_info,
)

# Laid out like an EU (CLP) SDS for copper(II) sulfate pentahydrate, CAS 7758-99-8.
# Section 16 lists H-codes of other substances, which must NOT be picked up.
EU_STYLE_SDS = """
SAFETY DATA SHEET
Version 6.9 Revision Date 15.03.2024 Print Date 28.09.2026
SECTION 1: Identification of the substance/mixture
Product name : Copper(II) sulfate pentahydrate  CAS-No. : 7758-99-8
SECTION 2: Hazards identification
2.1 Classification of the substance or mixture
Acute toxicity, Oral (Category 4), H302
Eye irritation (Category 2), H319
Long-term (chronic) aquatic hazard (Category 1), H410
2.2 Label elements
Signal word Warning
Hazard statement(s)
H302 + H319 Harmful if swallowed. Causes serious eye irritation.
H410 Very toxic to aquatic life with long lasting effects.
SECTION 3: Composition/information on ingredients
Formula : CuSO4 · 5H2O
SECTION 16: Other information
H314 Causes severe skin burns and eye damage. H350 May cause cancer.
"""

# Laid out like a US (OSHA HazCom 2012) SDS: sentences without H-codes, and the
# organ named in the middle of the H372 sentence.
US_STYLE_SDS = """
SAFETY DATA SHEET
Creation Date 10-Dec-2009 Revision Date 24-Dec-2021 Revision Number 6
1. Identification
Product Name Sulfuric acid
2. Hazard(s) identification
Classification
Skin Corrosion/Irritation Category 1 A
Label Elements
Signal Word
Danger
Hazard Statements
Causes severe skin burns and eye damage
May be corrosive to metals
Causes damage to organs (Teeth) through prolonged or repeated exposure if inhaled
Harmful to aquatic
life
3. Composition/Information on Ingredients
11. Toxicological information
Toxic if swallowed
"""


def test_eu_style_sds():
    info = read_sds_info(EU_STYLE_SDS)
    assert info.section_2_found
    assert info.signal_word == "Warning"
    assert info.hazard_codes == ["H302", "H319", "H410"]      # not H314/H350 from Section 16
    assert (info.sds_date, info.date_kind) == (date(2024, 3, 15), "revision")  # not the print date


def test_us_style_sds_without_codes():
    info = read_sds_info(US_STYLE_SDS)
    assert info.signal_word == "Danger"
    # H372, not H370: the organ in brackets is ignored. H402 despite the line
    # break in its sentence. Not H301 from Section 11.
    assert info.hazard_codes == ["H290", "H314", "H372", "H402"]
    assert (info.sds_date, info.date_kind) == (date(2021, 12, 24), "revision")
    assert info.outdated                                        # more than 3 years old


def test_longer_statements_are_not_also_read_as_shorter_ones():
    text = ("Very toxic to aquatic life with long lasting effects. "
            "May be fatal if swallowed and enters airways. Highly flammable liquid and vapour.")
    # Not H400/H401 (inside H410), not H300 (inside H304), not H226 (inside H225).
    assert find_hazard_codes(text) == ["H225", "H304", "H410"]


def test_organ_statements_and_reproductive_toxicity_codes():
    assert find_hazard_codes("May cause damage to the kidneys through prolonged or repeated exposure") == ["H373"]
    assert find_hazard_codes("Causes damage to organs") == ["H370"]
    assert find_hazard_codes("H360FD May damage fertility. May damage the unborn child.") == ["H360"]
    assert find_hazard_codes("H300+H310+H330 Fatal if swallowed, in contact with skin or if inhaled") == [
        "H300", "H310", "H330"]
    # EUH codes are kept; "H3021" and made-up codes such as H999 or H2O are not H-codes.
    assert find_hazard_codes("EUH066 H3021 H999 H2O") == ["EUH066"]


def test_table_of_contents_is_not_taken_for_section_2():
    text = ("1 Identification 2 Hazards identification 3 Composition\n"
            + EU_STYLE_SDS)
    section, found = hazard_section(text)
    assert found and "Signal word Warning" in section


def test_date_formats():
    assert parse_date("2023-05-12") == date(2023, 5, 12)
    assert parse_date("12.05.2023") == date(2023, 5, 12)       # European day.month.year
    assert parse_date("05/12/2023") == date(2023, 5, 12)       # US month/day/year
    assert parse_date("25/12/2023") == date(2023, 12, 25)      # first number > 12: day first
    assert parse_date("12 May 2023") == date(2023, 5, 12)
    assert parse_date("Sept. 3, 2020") == date(2020, 9, 3)
    assert parse_date("version 6.9") is None
    assert parse_date("31.02.2023") is None                    # no such day


def test_issue_date_used_when_there_is_no_revision_date():
    assert find_sds_date("Date of issue: 2019-01-31") == (date(2019, 1, 31), "issue")
    assert find_sds_date("Print Date 28.09.2026") == (None, "")


def test_describe_codes():
    assert describe_codes(["H302", "EUH066"]) == "H302 Harmful if swallowed; EUH066"
