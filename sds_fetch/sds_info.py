"""
sds_info.py - Reads hazard information and the revision date from an SDS's text.

For every SDS the program saves, it pulls three things out of the PDF text:
    * the GHS signal word ("Danger" or "Warning")
    * the GHS hazard statements, as H-codes (e.g. H302, H314)
    * the SDS date (revision date, or the issue date when no revision date is given)

These go into the hazard summary spreadsheet (CSV) and the log. SDS files that
are older than SDS_MAX_AGE_YEARS are flagged as outdated.

How the hazard statements are found:
    1. Only Section 2 ("Hazards identification") is searched when the program can
       find it. Section 2 holds the classification of the product itself. Other
       sections can mention hazard statements that do not apply to the product:
       Section 3 and Section 16 list those of individual ingredients, and
       Section 11 quotes toxicology data.
    2. H-codes printed in the text are collected (EU-style SDSs print them,
       e.g. "H314 Causes severe skin burns and eye damage").
    3. The standard English wording of each statement is also recognised,
       because many US SDSs print only the sentence without its code.

LIMITS - treat the results as a quick overview, not as a replacement for reading
Section 2: PDF text extraction is sometimes imperfect, hazard pictograms are
images and cannot be read, and only English-language SDSs are understood.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

# SDS files older than this many years are marked "outdated". This is a common
# internal review interval, not a legal limit: OSHA HazCom sets no expiry date,
# and REACH requires suppliers to update an SDS when new hazard information
# becomes available.
SDS_MAX_AGE_YEARS = 3


# ---------------------------------------------------------------------------
# GHS hazard statements (standard English wording, GHS Annex 3)
# ---------------------------------------------------------------------------

# Code -> standard sentence. Used for two things: recognising the sentence in
# SDSs that do not print the code, and writing the sentence next to each code in
# the spreadsheet. H227, H303, H305, H313, H316, H320, H333, H401 and H402 are
# used in the US (OSHA) and other GHS countries but not in the EU (CLP).
H_STATEMENTS = {
    "H200": "Unstable explosive",
    "H220": "Extremely flammable gas",
    "H221": "Flammable gas",
    "H222": "Extremely flammable aerosol",
    "H223": "Flammable aerosol",
    "H224": "Extremely flammable liquid and vapor",
    "H225": "Highly flammable liquid and vapor",
    "H226": "Flammable liquid and vapor",
    "H227": "Combustible liquid",
    "H228": "Flammable solid",
    "H240": "Heating may cause an explosion",
    "H241": "Heating may cause a fire or explosion",
    "H242": "Heating may cause a fire",
    "H250": "Catches fire spontaneously if exposed to air",
    "H251": "Self-heating; may catch fire",
    "H252": "Self-heating in large quantities; may catch fire",
    "H260": "In contact with water releases flammable gases which may ignite spontaneously",
    "H261": "In contact with water releases flammable gas",
    "H270": "May cause or intensify fire; oxidizer",
    "H271": "May cause fire or explosion; strong oxidizer",
    "H272": "May intensify fire; oxidizer",
    "H280": "Contains gas under pressure; may explode if heated",
    "H281": "Contains refrigerated gas; may cause cryogenic burns or injury",
    "H290": "May be corrosive to metals",
    "H300": "Fatal if swallowed",
    "H301": "Toxic if swallowed",
    "H302": "Harmful if swallowed",
    "H303": "May be harmful if swallowed",
    "H304": "May be fatal if swallowed and enters airways",
    "H305": "May be harmful if swallowed and enters airways",
    "H310": "Fatal in contact with skin",
    "H311": "Toxic in contact with skin",
    "H312": "Harmful in contact with skin",
    "H313": "May be harmful in contact with skin",
    "H314": "Causes severe skin burns and eye damage",
    "H315": "Causes skin irritation",
    "H316": "Causes mild skin irritation",
    "H317": "May cause an allergic skin reaction",
    "H318": "Causes serious eye damage",
    "H319": "Causes serious eye irritation",
    "H320": "Causes eye irritation",
    "H330": "Fatal if inhaled",
    "H331": "Toxic if inhaled",
    "H332": "Harmful if inhaled",
    "H333": "May be harmful if inhaled",
    "H334": "May cause allergy or asthma symptoms or breathing difficulties if inhaled",
    "H335": "May cause respiratory irritation",
    "H336": "May cause drowsiness or dizziness",
    "H340": "May cause genetic defects",
    "H341": "Suspected of causing genetic defects",
    "H350": "May cause cancer",
    "H351": "Suspected of causing cancer",
    "H360": "May damage fertility or the unborn child",
    "H361": "Suspected of damaging fertility or the unborn child",
    "H362": "May cause harm to breast-fed children",
    "H370": "Causes damage to organs",
    "H371": "May cause damage to organs",
    "H372": "Causes damage to organs through prolonged or repeated exposure",
    "H373": "May cause damage to organs through prolonged or repeated exposure",
    "H400": "Very toxic to aquatic life",
    "H401": "Toxic to aquatic life",
    "H402": "Harmful to aquatic life",
    "H410": "Very toxic to aquatic life with long lasting effects",
    "H411": "Toxic to aquatic life with long lasting effects",
    "H412": "Harmful to aquatic life with long lasting effects",
    "H413": "May cause long lasting harmful effects to aquatic life",
    "H420": "Harms public health and the environment by destroying ozone in the upper atmosphere",
}

# Shorter wordings that SDSs use for the reproductive toxicity statements when
# only fertility or only the unborn child is affected (e.g. H360F, H361d).
_EXTRA_PHRASES = [
    ("H360", "may damage fertility"),
    ("H360", "may damage the unborn child"),
    ("H361", "suspected of damaging fertility"),
    ("H361", "suspected of damaging the unborn child"),
]

# The organ-damage statements often name the organ in the middle of the sentence,
# e.g. "Causes damage to organs (kidney, liver) through prolonged or repeated
# exposure" or "May cause damage to the lungs through prolonged or repeated
# exposure". These patterns allow any short text in that position. They are
# checked before the fixed sentences so that H372/H373 are not mistaken for the
# shorter H370/H371 sentences they start with.
_ORGAN_PATTERNS = [
    ("H373", re.compile(r"may cause damage to [^.;]{1,80}? through prolonged or repeated exposure")),
    ("H372", re.compile(r"causes damage to [^.;]{1,80}? through prolonged or repeated exposure")),
]


def _build_phrase_patterns() -> list[tuple[str, re.Pattern]]:
    """
    Turn every standard sentence into a search pattern, longest sentence first.

    Longest first matters: "Toxic to aquatic life" (H401) is part of "Very toxic to
    aquatic life with long lasting effects" (H410). Each match is blanked out of
    the text after it is found, so the shorter sentence cannot match it again.
    """
    phrases = [(code, text.lower()) for code, text in H_STATEMENTS.items()] + _EXTRA_PHRASES
    phrases.sort(key=lambda item: len(item[1]), reverse=True)
    patterns = []
    for code, phrase in phrases:
        # re.escape makes characters such as "-" or "(" count as plain text.
        # \s* after ";" allows "fire;oxidizer" as well as "fire; oxidizer".
        pattern = re.escape(phrase).replace("; ", r";\s*")
        patterns.append((code, re.compile(pattern)))
    return _ORGAN_PATTERNS + patterns


_PHRASE_PATTERNS = _build_phrase_patterns()

# An H-code printed in the text: "H302", "EUH066", "H360FD", or one part of a
# combined code such as "H300+H310". The look-behind stops "EUH066" from also
# being read as "H066", and "(?!\d)" stops "H3021" from being read as "H302".
_H_CODE = re.compile(r"(?<![A-Za-z0-9])(EUH\d{3}|H\d{3})(?!\d)")


def _is_real_h_code(code: str) -> bool:
    """True for codes that exist in GHS: H200-H290, H300-H373, H400-H433, and EUH codes."""
    if code.startswith("EUH"):
        return True
    number = int(code[1:])
    return 200 <= number <= 290 or 300 <= number <= 373 or 400 <= number <= 433


def _sort_key(code: str) -> tuple[int, int]:
    """Sort order: H-codes by number, then the EU-only EUH codes."""
    return (1, int(code[3:])) if code.startswith("EUH") else (0, int(code[1:]))


# ---------------------------------------------------------------------------
# Section 2 and signal word
# ---------------------------------------------------------------------------

# Headings such as "SECTION 2: Hazards identification", "2. HAZARD(S) IDENTIFICATION"
# or "2 Hazards Identification". (?i) means "ignore upper/lower case".
_SECTION_2_START = re.compile(r"(?i)(?:section\s*)?\b2\s*[.:)]?\s*hazards?(?:\(s\))?\s+identification")
_SECTION_3_START = re.compile(r"(?i)(?:section\s*)?\b3\s*[.:)]?\s*composition")

_SIGNAL_WORD = re.compile(r"(?i)signal\s*word\s*[:\-]?\s*(danger|warning|none|no signal word)")


def hazard_section(text: str) -> tuple[str, bool]:
    """
    Return (the Section 2 text, True) when Section 2 can be found, otherwise
    (the whole text, False).
    """
    for start in _SECTION_2_START.finditer(text):
        end = _SECTION_3_START.search(text, start.end())
        section = text[start.end(): end.start() if end else len(text)]
        # A very short "section" is a table of contents line such as
        # "2 Hazards identification ... 3 Composition": try the next heading.
        if len(section.strip()) >= 30:
            return section, True
    return text, False


def find_signal_word(text: str) -> str:
    """Return "Danger", "Warning", "none" (no signal word) or "" (not found)."""
    match = _SIGNAL_WORD.search(text)
    if not match:
        return ""
    word = match.group(1).lower()
    return "none" if word in ("none", "no signal word") else word.capitalize()


def find_hazard_codes(text: str) -> list[str]:
    """Return the GHS hazard codes in `text`, sorted, each code only once."""
    codes = {code for code in _H_CODE.findall(text) if _is_real_h_code(code)}

    # Tidy the text before looking for sentences: one space between words,
    # lower case, US spelling "vapor", and no text in brackets, e.g. the organ
    # in "Causes damage to organs (liver) through prolonged or repeated exposure".
    tidy = re.sub(r"\([^)]*\)", " ", text)
    tidy = re.sub(r"\s+", " ", tidy).lower().replace("vapour", "vapor")
    tidy = tidy.replace(" ;", ";")
    for code, pattern in _PHRASE_PATTERNS:
        if pattern.search(tidy):
            codes.add(code)
            tidy = pattern.sub(" | ", tidy)  # blank it out (see _build_phrase_patterns)
    return sorted(codes, key=_sort_key)


def describe_codes(codes: list[str]) -> str:
    """'H302 Harmful if swallowed; H314 Causes severe ...' for the spreadsheet."""
    return "; ".join(f"{code} {H_STATEMENTS[code]}" if code in H_STATEMENTS else code for code in codes)


# ---------------------------------------------------------------------------
# SDS date
# ---------------------------------------------------------------------------

# Labels in front of the date, in order of preference. "Print date" is ignored
# on purpose: some suppliers print the download date there, which says nothing
# about how old the SDS content is.
_REVISION_LABEL = re.compile(
    r"(?i)(?:revision\s+date|date\s+of\s+revision|date\s+revised|revised\s+on|last\s+revised|revision)")
_ISSUE_LABEL = re.compile(
    r"(?i)(?:issue\s+date|date\s+of\s+issue|date\s+issued|issued\s+on|creation\s+date|"
    r"date\s+of\s+preparation|preparation\s+date|version\s+date|sds\s+date)")

_MONTHS = {name: number for number, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# The date formats found on SDSs, each with a function that turns the pieces
# into (year, month, day).
_DATE_FORMATS = [
    # 2023-05-12 or 2023/05/12
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b"),
     lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    # 12-May-2023, 12 May 2023, 12.May.2023
    (re.compile(r"(?i)\b(\d{1,2})[-\s.]+([a-z]{3})[a-z]*\.?[-\s.,]+(\d{4})\b"),
     lambda m: (int(m[3]), _MONTHS.get(m[2].lower(), 0), int(m[1]))),
    # May 12, 2023
    (re.compile(r"(?i)\b([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b"),
     lambda m: (int(m[3]), _MONTHS.get(m[1].lower(), 0), int(m[2]))),
    # 12.05.2023 (European, day first)
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"),
     lambda m: (int(m[3]), int(m[2]), int(m[1]))),
    # 05/12/2023: US month/day/year, unless the first number is above 12
    # (then it must be day/month/year). A wrongly guessed order shifts the date
    # by less than a year.
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"),
     lambda m: (int(m[3]), int(m[2]), int(m[1])) if int(m[1]) > 12
     else (int(m[3]), int(m[1]), int(m[2]))),
]


def parse_date(text: str) -> date | None:
    """Return the first believable date written in `text`, or None."""
    found = []
    for pattern, to_parts in _DATE_FORMATS:
        for match in pattern.finditer(text):
            try:
                year, month, day = to_parts(match)
                value = date(year, month, day)  # raises ValueError for e.g. month 0 or 13
            except ValueError:
                continue
            if 1985 <= value.year <= date.today().year + 1:
                found.append((match.start(), value))
    # Several formats can match; the one that starts first in the text wins.
    return min(found)[1] if found else None


def find_sds_date(text: str) -> tuple[date | None, str]:
    """
    Return (date, "revision") or (date, "issue"), or (None, "") when no date
    could be found next to a revision or issue label.
    """
    for label, kind in ((_REVISION_LABEL, "revision"), (_ISSUE_LABEL, "issue")):
        for match in label.finditer(text):
            # The date is normally right after its label: look at the next 40
            # characters, but stop at a "Print date", which is often the download day.
            window = text[match.end(): match.end() + 40]
            window = re.split(r"(?i)print", window)[0]
            value = parse_date(window)
            if value:
                return value, kind
    return None, ""


def age_in_years(value: date, today: date | None = None) -> float:
    """How many years ago `value` was, e.g. 3.4."""
    return ((today or date.today()) - value).days / 365.25


# ---------------------------------------------------------------------------
# Everything together
# ---------------------------------------------------------------------------

@dataclass
class SdsInfo:
    """What was read from one SDS."""

    signal_word: str = ""                            # "Danger", "Warning", "none" or "" (not found)
    hazard_codes: list[str] = field(default_factory=list)
    section_2_found: bool = False                    # False: whole text was searched
    sds_date: date | None = None
    date_kind: str = ""                              # "revision", "issue" or ""

    @property
    def age_years(self) -> float | None:
        return age_in_years(self.sds_date) if self.sds_date else None

    @property
    def outdated(self) -> bool:
        return self.sds_date is not None and age_in_years(self.sds_date) > SDS_MAX_AGE_YEARS


def read_sds_info(text: str) -> SdsInfo:
    """Read the signal word, hazard codes and SDS date from the text of an SDS."""
    section, found = hazard_section(text)
    sds_date, kind = find_sds_date(text)
    return SdsInfo(
        signal_word=find_signal_word(section) or find_signal_word(text),
        hazard_codes=find_hazard_codes(section),
        section_2_found=found,
        sds_date=sds_date,
        date_kind=kind,
    )
