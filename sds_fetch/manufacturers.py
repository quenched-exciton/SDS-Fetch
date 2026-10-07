"""
manufacturers.py - Recognising manufacturer names, including their other brand names.

You can ask for the SDS of a particular manufacturer (see README, "Choosing the
manufacturer"). Websites and SDS files often use a different name for the same
company: a Sigma-Aldrich product can be labelled "MilliporeSigma", "Merck",
"Aldrich", "Fluka" or "Supelco". The table below groups these names so that
asking for any one of them finds all of them.

Names that are not in the table still work: they are matched as typed (ignoring
upper/lower case and punctuation), e.g. "Acme Plating" matches "ACME PLATING INC".

To add a company or a brand, add a line to MANUFACTURER_GROUPS. Write every
name in lower case. The first name of each line is the one shown in the log.
"""

from __future__ import annotations

import re

# Each line: (name shown in the log, [other names that mean the same company]).
# Brand ownership as of 2025. Short names such as "tci" are matched as whole
# words only, so "tci" does not match inside another word.
MANUFACTURER_GROUPS: list[tuple[str, list[str]]] = [
    ("Sigma-Aldrich", ["sigma-aldrich", "sigma aldrich", "sigma", "aldrich", "milliporesigma",
                       "millipore sigma", "merck", "fluka", "supelco"]),
    ("Thermo Fisher", ["thermo fisher", "thermofisher", "thermo scientific", "fisher scientific",
                       "fisher", "acros", "acros organics", "alfa aesar", "fisher chemical"]),
    ("Avantor / VWR", ["avantor", "vwr", "j.t. baker", "j. t. baker", "jt baker", "macron"]),
    ("TCI", ["tci", "tokyo chemical industry"]),
    ("Gelest", ["gelest"]),
    ("Honeywell", ["honeywell", "riedel-de haen", "riedel-de haën", "riedel de haen"]),
    ("Spectrum Chemical", ["spectrum chemical", "spectrum"]),
    ("Oakwood Chemical", ["oakwood"]),
    ("Strem", ["strem"]),
    ("Fluorochem", ["fluorochem"]),
    ("Combi-Blocks", ["combi-blocks", "combi blocks"]),
    ("Santa Cruz Biotechnology", ["santa cruz biotechnology", "santa cruz"]),
    ("Cayman Chemical", ["cayman chemical", "cayman"]),
    ("BASF", ["basf"]),
    ("Dow", ["dow", "dow chemical", "dow corning"]),
    ("Evonik", ["evonik"]),
    ("Wacker", ["wacker"]),
    ("Shin-Etsu", ["shin-etsu", "shin etsu", "shinetsu"]),
    ("Momentive", ["momentive"]),
    ("Atotech", ["atotech"]),
    ("MacDermid", ["macdermid", "macdermid enthone", "enthone"]),
]


def _words(text: str) -> str:
    """
    Lower case, accents kept, every run of punctuation/spaces turned into ONE space,
    with a space at both ends, e.g. "Sigma-Aldrich, Inc." -> " sigma aldrich inc ".
    The spaces at both ends let us find whole words with a simple "in" test.
    """
    return " " + " ".join(re.findall(r"\w+", text.lower())) + " "


def names_for(requested: str) -> tuple[str, list[str]]:
    """
    Return (display name, all names to look for) for what the user typed.

    "aldrich" -> ("Sigma-Aldrich", ["sigma-aldrich", "sigma aldrich", "sigma", ...])
    "Acme"    -> ("Acme", ["acme"])
    """
    wanted = _words(requested)
    # 1. Exact match with a name in the table ("merck", "Alfa Aesar").
    for display_name, aliases in MANUFACTURER_GROUPS:
        if any(_words(alias) == wanted for alias in aliases + [display_name]):
            return display_name, aliases
    # 2. A name from the table inside what was typed ("Sigma-Aldrich Inc."). The
    #    longest such name wins, so "Thermo Fisher Scientific" is not decided by "fisher".
    best = None
    for display_name, aliases in MANUFACTURER_GROUPS:
        for alias in aliases:
            if _words(alias) in wanted and (best is None or len(alias) > len(best[0])):
                best = (alias, display_name, aliases)
    if best:
        return best[1], best[2]
    # 3. Not in the table: look for the name exactly as typed.
    return requested.strip(), [requested.strip().lower()]


def display_name(requested: str) -> str:
    """The name used in the log for what the user typed, e.g. "merck" -> "Sigma-Aldrich"."""
    return names_for(requested)[0]


def mentions(requested: str, text: str) -> bool:
    """True when `text` names the requested manufacturer (or one of its other names)."""
    if not text:
        return False
    haystack = _words(text)
    return any(_words(alias) in haystack for alias in names_for(requested)[1])


def preference_rank(preferences: list[str], supplier: str) -> int | None:
    """
    Position of the first preference that `supplier` matches (0 = most wanted),
    or None when it matches none of them.
    """
    for rank, requested in enumerate(preferences):
        if mentions(requested, supplier):
            return rank
    return None
