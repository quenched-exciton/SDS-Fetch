"""
cas.py - Reading and checking CAS Registry Numbers.

A CAS Registry Number has three parts separated by hyphens:

    7732-18-5
    ^^^^ ^^ ^
    |    |  +-- 1 check digit
    |    +----- 2 digits
    +---------- 2 to 7 digits

The check digit lets us catch most typing mistakes without going online.
Take every digit except the check digit, read them from RIGHT to LEFT,
multiply the 1st by 1, the 2nd by 2, the 3rd by 3 and so on, add the
products, and keep only the last digit of the sum (the sum modulo 10).
That last digit must equal the check digit.

Worked example for water, 7732-18-5:
    digits right-to-left : 8  1  2  3  7  7
    multipliers          : 1  2  3  4  5  6
    products             : 8  2  6 12 35 42  -> sum = 105 -> 105 % 10 = 5  (valid)

This module has no network code, so everything in it can be tested offline.
"""

# "from __future__ import annotations" lets us write modern type hints such as
# "list[str]" while still supporting older Python versions (3.9).
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Regular expressions (text patterns) used below
# ---------------------------------------------------------------------------

# Word processors and PDF files often replace the normal hyphen "-" with look-alike
# dash characters (en dash, em dash, minus sign, ...). We convert all of them to a
# plain hyphen before searching for CAS numbers.
_DASH_CHARACTERS = "‐‑‒–—―−﹣－"
_DASH_TRANSLATION = str.maketrans({dash: "-" for dash in _DASH_CHARACTERS})

# Pattern for "something that looks like a CAS number":
#   (\d{2,7})  -> 2 to 7 digits   (first part, captured as group 1)
#   -(\d{2})   -> hyphen + 2 digits (second part, group 2)
#   -(\d)      -> hyphen + 1 digit  (check digit, group 3)
# The "(?<![\d-])" and "(?![\d-])" parts make sure the match is not glued to more
# digits or hyphens on either side (so "164-17-55" is NOT read as "64-17-5").
_CAS_PATTERN = re.compile(r"(?<![\d-])(\d{2,7})-(\d{2})-(\d)(?![\d-])")


# ---------------------------------------------------------------------------
# Small data containers
# ---------------------------------------------------------------------------

@dataclass
class InvalidEntry:
    """One piece of user input that could not be used as a CAS number."""

    text: str    # exactly what the user typed (or what was in the CSV cell)
    reason: str  # human-readable explanation, written to the log file


@dataclass
class ParsedInput:
    """Everything we learned from reading the user's list."""

    valid_cas: list[str] = field(default_factory=list)          # unique, in input order
    invalid: list[InvalidEntry] = field(default_factory=list)   # rejected entries
    duplicates_ignored: int = 0                                  # repeats that were skipped
    entries_read: int = 0                                        # non-empty lines / cells seen


# ---------------------------------------------------------------------------
# Core CAS helpers
# ---------------------------------------------------------------------------

def normalize_dashes(text: str) -> str:
    """Replace every look-alike dash character with a plain hyphen '-'."""
    return text.translate(_DASH_TRANSLATION)


def has_valid_check_digit(cas: str) -> bool:
    """
    Return True if the CAS number's check digit is correct.

    `cas` must already be in the form "digits-digits-digit" (e.g. "64-17-5").
    """
    first_part, second_part, check_digit = cas.split("-")

    # Join the first two parts ("64" + "17" -> "6417") and reverse the string
    # ("7146") so that position 1 is the right-most digit.
    digits_right_to_left = (first_part + second_part)[::-1]

    # enumerate(..., start=1) gives (1, '7'), (2, '1'), (3, '4'), (4, '6')
    total = sum(position * int(digit)
                for position, digit in enumerate(digits_right_to_left, start=1))

    return total % 10 == int(check_digit)


def check_cas(text: str) -> tuple[str | None, str]:
    """
    Check a single piece of text that should be exactly one CAS number.

    Returns a pair (cas, message):
      * ("64-17-5", "")               when the text is a valid CAS number
      * (None, "reason it is invalid") otherwise
    """
    cleaned = normalize_dashes(text.strip())
    match = _CAS_PATTERN.fullmatch(cleaned)
    if match is None:
        return None, "not in CAS format (expected e.g. 64-17-5)"
    return _check_match(match)


def _check_match(match: re.Match) -> tuple[str | None, str]:
    """Validate one regular-expression match of _CAS_PATTERN (see check_cas)."""
    first_part, second_part, check_digit = match.groups()

    # Remove leading zeros from the first part: "064-17-5" -> "64-17-5".
    # CAS numbers never start with zero, but spreadsheets sometimes add them.
    first_part = first_part.lstrip("0")
    if len(first_part) < 2:
        return None, "first part of a CAS number must have 2 to 7 digits"

    cas = f"{first_part}-{second_part}-{check_digit}"
    if not has_valid_check_digit(cas):
        return None, "check digit is wrong (probably a typo)"
    return cas, ""


def extract_cas_numbers(text: str) -> tuple[list[str], list[InvalidEntry]]:
    """
    Find every CAS-looking number inside one line of text (or one CSV cell).

    This is forgiving on purpose: a pasted line such as
        "64-17-5   Ethanol, absolute"
    gives ["64-17-5"], and the chemical name is simply ignored.

    Returns (valid_cas_list, invalid_entries).
    If the text contains nothing that looks like a CAS number, the whole text is
    reported as one invalid entry so the user can see it in the log.
    """
    cleaned = normalize_dashes(text.strip())
    found_valid: list[str] = []
    found_invalid: list[InvalidEntry] = []

    matches = list(_CAS_PATTERN.finditer(cleaned))
    if not matches:
        found_invalid.append(InvalidEntry(text.strip(), "no CAS number found (expected e.g. 64-17-5)"))
        return found_valid, found_invalid

    for match in matches:
        cas, reason = _check_match(match)
        if cas is None:
            found_invalid.append(InvalidEntry(match.group(0), reason))
        else:
            found_valid.append(cas)
    return found_valid, found_invalid


# ---------------------------------------------------------------------------
# Reading the user's list (typed text or CSV file)
# ---------------------------------------------------------------------------

def _collect(entries: list[str]) -> ParsedInput:
    """
    Shared logic for typed text and CSV files.

    `entries` is a list of raw strings (one per line or per CSV cell).
    Empty strings are skipped. Duplicate CAS numbers are kept only once, in the
    order they first appeared.
    """
    result = ParsedInput()
    already_seen: set[str] = set()

    for entry in entries:
        if not entry.strip():
            continue  # ignore blank lines / empty cells
        result.entries_read += 1

        valid, invalid = extract_cas_numbers(entry)
        result.invalid.extend(invalid)
        for cas in valid:
            if cas in already_seen:
                result.duplicates_ignored += 1
            else:
                already_seen.add(cas)
                result.valid_cas.append(cas)
    return result


def parse_text_list(text: str) -> ParsedInput:
    """
    Read CAS numbers typed or pasted into the multi-line text box.

    Put one CAS number per line. Several numbers on one line also work when they
    are separated by commas, semicolons, tabs or spaces.
    """
    return _collect(text.splitlines())


def _read_csv_text(csv_path: Path) -> str:
    """
    Read a CSV file as text.

    Files saved by Excel are usually UTF-8 (sometimes with an invisible "BOM"
    marker at the start) or, on older Windows setups, the Windows-1252 encoding.
    We try UTF-8 first and fall back to Windows-1252.
    """
    try:
        return csv_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return csv_path.read_text(encoding="cp1252")


def parse_csv_file(csv_path: str | Path) -> ParsedInput:
    """
    Read CAS numbers from a CSV file.

    Which column is used?
      1. If the first row has a header that contains the word "CAS"
         (for example "CAS", "CAS No.", "CAS Number"), that column is used and
         the header row is skipped.
      2. Otherwise the FIRST column is used. If the first cell of that column
         contains no digits at all (e.g. "Chemical"), it is treated as a header
         and skipped.

    Comma, semicolon and tab separated files are all detected automatically.
    """
    csv_path = Path(csv_path)
    text = _read_csv_text(csv_path)

    # csv.Sniffer guesses the separator (",", ";" or tab) from a sample of the file.
    # European versions of Excel, for example, save CSV files with ";".
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # plain comma-separated as the fallback

    rows = [row for row in csv.reader(text.splitlines(), dialect) if any(cell.strip() for cell in row)]
    if not rows:
        return ParsedInput()

    header = rows[0]
    cas_column = None
    for index, cell in enumerate(header):
        # A header cell mentions "cas" but is not itself a CAS number.
        if "cas" in cell.lower() and not _CAS_PATTERN.search(normalize_dashes(cell)):
            cas_column = index
            break

    if cas_column is not None:
        data_rows = rows[1:]
    else:
        cas_column = 0
        first_cell = header[0] if header else ""
        looks_like_header = not any(character.isdigit() for character in first_cell)
        data_rows = rows[1:] if looks_like_header else rows

    # Take the chosen column from each row (rows that are too short give "").
    cells = [row[cas_column] if cas_column < len(row) else "" for row in data_rows]
    return _collect(cells)
