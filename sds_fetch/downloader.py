"""
downloader.py - Finds, checks and saves one SDS PDF per CAS number, then writes the log.

For every CAS number the steps are:
  1. Skip it if "<CAS>.pdf" already exists in the download folder (unless the
     user ticked "overwrite").
  2. Look up the chemical name on PubChem (for the progress messages and the log).
  3. Try each SDS source in turn (see sources.SDS_SOURCES). For every link a
     source returns:
       a. download it and check that the file really is a PDF;
       b. read the text of the PDF and check that the CAS number is printed in it.
     The first PDF that passes both checks is saved as "<CAS>.pdf" and we move on.
  4. If no PDF contains the CAS number in its text (for example a scanned SDS
     with no text layer), the first PDF that the website linked to this exact CAS
     number is saved instead and marked "not verified" in the log.
  5. The signal word, hazard statements (H-codes) and SDS date are read from the
     saved PDF's text (see sds_info.py). They go into the hazard summary
     spreadsheet, and SDS files older than SDS_MAX_AGE_YEARS are flagged.

When manufacturers are requested (with "@ Gelest" on a line, or the "Preferred
manufacturers" box), step 3 changes:
  * SDS links from the first-choice manufacturer are tried as soon as a source
    lists them. All other links are put aside.
  * When every source has been asked and none of those worked, the links put
    aside are tried: other requested manufacturers in the order given, then
    links whose manufacturer the website did not name, then (unless "strict" is
    ticked) every other manufacturer.
  * Every saved PDF is checked for the manufacturer's name in its Section 1.
    In strict mode a PDF is only saved when the website or the PDF names a
    requested manufacturer.

The GUI calls run_batch() from a background thread. run_batch() reports what it
is doing through two "callback" functions supplied by the caller:
    report(message)          -> one line of text for the progress box
    progress(done, total)    -> how many CAS numbers are finished
"""

from __future__ import annotations

import csv
import io
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence

import requests
from pypdf import PdfReader

from .cas import ParsedInput, normalize_dashes
from .manufacturers import display_name, preference_rank
from .sds_info import SDS_MAX_AGE_YEARS, SdsInfo, describe_codes, identification_section, read_sds_info
from .sources import (
    CERTIFICATE_PROBLEM,
    MAX_CANDIDATES_PER_SOURCE,
    REQUEST_TIMEOUT,
    SDS_SOURCES,
    SdsCandidate,
    SourceFunction,
    certificate_store_description,
    create_session,
    describe_error,
    lookup_chemical_name,
    python_description,
)

# pypdf prints warnings for slightly malformed PDFs (very common for SDS files).
# They are harmless for our purpose, so we hide them.
logging.getLogger("pypdf").setLevel(logging.ERROR)

# Refuse files bigger than this (an SDS is normally well under 5 MB).
MAX_PDF_BYTES = 50 * 1024 * 1024

# Only the first pages are searched for the CAS number. It is printed in
# section 1 or section 3 of an SDS, which are always near the start.
PAGES_TO_SEARCH = 6

# Pause between chemicals so we do not flood the websites with requests.
PAUSE_BETWEEN_CHEMICALS_SECONDS = 1.0


class DownloadError(Exception):
    """Raised when a link does not give us a usable PDF."""


@dataclass
class CasResult:
    """What happened to one CAS number. One of these is written per line of the log."""

    cas: str
    status: str = "failed"             # "success" or "failed"
    chemical_name: str = ""            # from PubChem, may stay empty
    source: str = ""                   # website the SDS came from
    supplier: str = ""                 # manufacturer, when the website tells us
    cas_verified: str = ""             # "yes", "NO - check manually" or "-" (not checked)
    note: str = ""                     # extra information for the log
    attempts: list[str] = field(default_factory=list)  # what each source said
    info: SdsInfo | None = None        # hazard codes and SDS date; None = PDF text unreadable
    requested: str = ""                # requested manufacturer(s), e.g. "Gelest" ("" = any)
    maker_match: str = ""              # whether the saved SDS is from a requested manufacturer


@dataclass
class BatchResult:
    """Everything that happened during one run of the program."""

    successes: list[CasResult] = field(default_factory=list)
    failures: list[CasResult] = field(default_factory=list)
    not_processed: list[str] = field(default_factory=list)  # left over after "Stop"
    log_path: Path | None = None
    summary_path: Path | None = None   # the hazard summary spreadsheet (CSV)
    cancelled: bool = False
    certificate_warning_shown: bool = False  # the GUI warning is shown only once
    preferred: list[str] = field(default_factory=list)  # "Preferred manufacturers" box
    strict: bool = False


def manufacturer_preferences(on_line: str, preferred: Sequence[str]) -> list[str]:
    """
    The manufacturers to ask for, most wanted first: the one written on the line
    ("@ Gelest") and then the "Preferred manufacturers" box. Repeats are removed,
    also when written differently ("Merck" and "Sigma-Aldrich" are the same company).
    """
    result: list[str] = []
    for name in [on_line, *preferred]:
        name = name.strip()
        if name and display_name(name) not in [display_name(kept) for kept in result]:
            result.append(name)
    return result


# Explanation written to the progress box and the log when websites are refused
# because of their security certificate (see sources.use_system_certificates).
NETWORK_WARNING = [
    "websites refused the connection with a security-certificate error.",
    "This is a network problem, not a missing SDS. Company networks often",
    "re-sign HTTPS traffic with their own certificate. Fix: run",
    "    python -m pip install -r requirements.txt",
    "to install 'truststore' (needs Python 3.10 or newer), then run again.",
    "If the 'Certificates:' line of the log already names truststore, ask IT",
    "whether the proxy blocks these websites. See 'Company networks' in README.md.",
]


def failed_on_certificates(result: CasResult) -> bool:
    """True when a CAS number failed and at least one SDS website refused its certificate."""
    return result.status == "failed" and any(
        CERTIFICATE_PROBLEM in attempt for attempt in result.attempts if not attempt.startswith("PubChem:")
    )


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------

def download_pdf(url: str, session: requests.Session) -> bytes:
    """
    Download `url` and return the file contents if (and only if) it is a PDF.

    Every PDF file starts with the characters "%PDF" (its "magic number"). Many
    SDS links lead to a web page (a login page, an error page, a product page)
    instead of a PDF, and this check catches those.
    """
    # stream=True downloads the file piece by piece so we can stop early if it is huge.
    with session.get(url, timeout=REQUEST_TIMEOUT, stream=True) as response:
        if response.status_code != 200:
            raise DownloadError(f"HTTP {response.status_code}")

        chunks, size = [], 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            size += len(chunk)
            if size > MAX_PDF_BYTES:
                raise DownloadError("file is larger than 50 MB")
            chunks.append(chunk)
    content = b"".join(chunks)

    if b"%PDF" not in content[:1024]:
        raise DownloadError("link did not return a PDF (probably a web page)")
    return content


def pdf_mentions_cas(pdf_bytes: bytes, cas: str) -> bool | None:
    """
    Check whether the CAS number is printed in the PDF's text.

    Returns:
        True  -> the CAS number was found
        False -> the text was readable but the CAS number was not in it
        None  -> no text could be read (scanned image, encrypted or damaged PDF)
    """
    return cas_in_text(extract_pdf_text(pdf_bytes), cas)


def extract_pdf_text(pdf_bytes: bytes) -> str | None:
    """
    Return the text of the first PAGES_TO_SEARCH pages, or None when no text
    can be read (scanned image, encrypted or damaged PDF).
    """
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_texts = []
        for page in reader.pages[:PAGES_TO_SEARCH]:
            page_texts.append(page.extract_text() or "")
        text = "\n".join(page_texts)
    except Exception:  # pypdf can raise many different errors for broken files
        return None
    return text if text.strip() else None


def cas_in_text(text: str | None, cas: str) -> bool | None:
    """Same answers as pdf_mentions_cas, for text that was already extracted."""
    if text is None:
        return None

    # PDF text extraction sometimes puts spaces around hyphens ("64 - 17 - 5")
    # or uses a different dash character, so tidy those up before searching.
    text = re.sub(r"\s*-\s*", "-", normalize_dashes(text))
    return re.search(rf"(?<![\d-]){re.escape(cas)}(?![\d-])", text) is not None


def save_pdf(pdf_bytes: bytes, target: Path) -> None:
    """
    Save the PDF safely: write to a temporary ".part" file first, then rename it.

    If the program is interrupted half-way, you are left with a ".part" file
    rather than a broken "<CAS>.pdf" that looks complete.
    """
    temporary = target.with_name(target.name + ".part")
    temporary.write_bytes(pdf_bytes)
    os.replace(temporary, target)  # replaces an existing file on all systems


def report_sds_info(result: CasResult, report: Callable[[str], None]) -> None:
    """Show the signal word, H-codes and SDS date in the progress box."""
    info = result.info
    if info is None:
        return
    hazards = ", ".join(info.hazard_codes) or "no H-codes found"
    signal = f"{info.signal_word}: " if info.signal_word and info.signal_word != "none" else ""
    report(f"    Hazards: {signal}{hazards}")
    if info.sds_date:
        report(f"    SDS {info.date_kind} date {info.sds_date:%Y-%m-%d}"
               + (f" - OUTDATED (older than {SDS_MAX_AGE_YEARS} years)" if info.outdated else ""))
    else:
        report("    SDS date not found in the PDF text")


# ---------------------------------------------------------------------------
# One CAS number
# ---------------------------------------------------------------------------

def _maker_match_text(website_match: bool, pdf_match: bool) -> str:
    """How sure we are that the SDS comes from a requested manufacturer (for the log)."""
    if pdf_match:
        return "yes"                       # Section 1 of the PDF names the manufacturer
    if website_match:
        return "website only"              # the website said so, the PDF text does not
    return "no"


def process_one_cas(
    cas: str,
    download_folder: Path,
    session: requests.Session,
    overwrite: bool = False,
    report: Callable[[str], None] = print,
    sources: Sequence[tuple[str, SourceFunction]] = SDS_SOURCES,
    preferences: Sequence[str] = (),
    strict: bool = False,
) -> CasResult:
    """
    Find, check and save the SDS for a single CAS number (see steps at the top).

    preferences - manufacturers to prefer, most wanted first (empty = any)
    strict      - True = only save an SDS from one of `preferences`
    """
    result = CasResult(cas=cas)
    target = download_folder / f"{cas}.pdf"
    preferences = list(preferences)
    strict = strict and bool(preferences)
    result.requested = ", ".join(display_name(name) for name in preferences)

    def from_requested_maker(text: str | None) -> bool:
        """True when Section 1 of the SDS text names a requested manufacturer."""
        return bool(preferences and text) and preference_rank(preferences, identification_section(text)) is not None

    # --- Step 1: file already there? --------------------------------------
    if target.exists() and not overwrite:
        result.status = "success"
        result.source = "already in folder"
        result.cas_verified = "-"
        result.note = "file already existed, not downloaded again"
        report(f"  {cas}.pdf already exists - skipped (tick 'Overwrite' to replace it).")
        try:
            text = extract_pdf_text(target.read_bytes())
        except OSError:  # e.g. the file is open in another program and locked
            text = None
        result.info = read_sds_info(text) if text else None
        if preferences:
            result.maker_match = "yes" if from_requested_maker(text) else "not confirmed"
            if result.maker_match != "yes":
                result.note += f"; may not be from {result.requested} (tick 'Overwrite' to search again)"
                report(f"    The existing file does not name {result.requested} - tick 'Overwrite' to search again.")
        report_sds_info(result, report)
        return result

    # --- Step 2: chemical identity from PubChem ----------------------------
    try:
        result.chemical_name = lookup_chemical_name(cas, session) or ""
    except Exception as error:  # PubChem problems must never stop the SDS search
        result.attempts.append(f"PubChem: {describe_error(error)}")
    if result.chemical_name:
        report(f"  Identified as: {result.chemical_name}")
    else:
        report("  Not found in PubChem (normal for many polymers and mixtures) - searching anyway.")
    if preferences:
        report(f"  Requested manufacturer: {result.requested}"
               + (" (strict: no other manufacturer)" if strict else ""))

    # --- Step 3: try each SDS source ---------------------------------------
    # Remember the first PDF we could not verify, in case nothing better turns up.
    unverified_backup: tuple[bytes, SdsCandidate, bool] | None = None
    makers_offered: list[str] = []   # e.g. "TCI (VWR)", listed in the log in strict mode

    def try_candidate(source_name: str, candidate: SdsCandidate) -> bool:
        """Download one link, check it, and save it if it is good. True = saved."""
        nonlocal unverified_backup
        try:
            pdf_bytes = download_pdf(candidate.url, session)
        except Exception as error:
            message = describe_error(error)
            result.attempts.append(f"{source_name}: download failed ({message})")
            report(f"    {source_name}: download failed ({message})")
            return False

        text = extract_pdf_text(pdf_bytes)
        found = cas_in_text(text, cas)
        website_match = bool(preferences) and preference_rank(preferences, candidate.supplier) is not None
        pdf_match = from_requested_maker(text)

        if found:
            if strict and not (website_match or pdf_match):
                maker = candidate.supplier or "a manufacturer the website did not name"
                result.attempts.append(f"{source_name}: SDS is from {maker}, not {result.requested} - skipped")
                report(f"    {source_name}: SDS is from {maker}, not {result.requested} - skipped (strict)")
                return False
            save_pdf(pdf_bytes, target)
            result.status = "success"
            result.source = candidate.source
            result.supplier = candidate.supplier
            result.cas_verified = "yes"
            if preferences:
                result.maker_match = _maker_match_text(website_match, pdf_match)
                if result.maker_match == "no":
                    result.note = f"no SDS from {result.requested} found; saved another manufacturer's SDS"
            report(f"    Saved {target.name} from {candidate.source}"
                   + (f" ({candidate.supplier})" if candidate.supplier else "")
                   + " - CAS number confirmed in the PDF.")
            if preferences:
                report(f"    From a requested manufacturer: {result.maker_match}")
            result.info = read_sds_info(text)
            report_sds_info(result, report)
            return True

        if found is False:
            # Readable PDF, but for a different substance: never keep it.
            result.attempts.append(f"{source_name}: PDF did not contain {cas}, rejected")
            report(f"    {source_name}: PDF does not mention {cas} - rejected")
        elif candidate.exact_match and not (strict and not website_match):
            result.attempts.append(f"{source_name}: PDF text could not be read")
            report(f"    {source_name}: PDF text could not be read - kept as a backup")
            if unverified_backup is None:
                unverified_backup = (pdf_bytes, candidate, website_match)
        else:
            # Unreadable PDF from a loose search hit (or, in strict mode, from a
            # manufacturer we cannot confirm): too risky to keep.
            result.attempts.append(f"{source_name}: unreadable PDF that could not be confirmed, rejected")
            report(f"    {source_name}: PDF text could not be read and could not be confirmed - rejected")
        return False

    def rank(candidate: SdsCandidate) -> int:
        """
        Order in which links are tried when manufacturers are requested:
        0, 1, ... = requested manufacturers in the order given,
        len(preferences) = the website did not name the manufacturer,
        len(preferences) + 1 = a different manufacturer.
        """
        position = preference_rank(preferences, candidate.supplier)
        if position is not None:
            return position
        return len(preferences) if not candidate.supplier.strip() else len(preferences) + 1

    put_aside: list[tuple[int, int, str, SdsCandidate]] = []  # (rank, order found, source, link)

    for source_name, find_function in sources:
        report(f"  Searching {source_name} ...")
        try:
            candidates = find_function(cas, session)
        except Exception as error:  # network errors, blocked requests, changed pages
            message = describe_error(error)
            result.attempts.append(f"{source_name}: {message}")
            report(f"    {source_name}: search failed ({message})")
            continue

        if not candidates:
            result.attempts.append(f"{source_name}: no SDS found")
            report(f"    {source_name}: no SDS found")
            continue

        if not preferences:
            # No manufacturer requested: try the links in the order the website gave them.
            for candidate in candidates[:MAX_CANDIDATES_PER_SOURCE]:
                if try_candidate(source_name, candidate):
                    return result
            continue

        for candidate in candidates:
            label = f"{candidate.supplier or 'not named'} ({source_name})"
            if label not in makers_offered:
                makers_offered.append(label)

        # Up to MAX_CANDIDATES_PER_SOURCE links from requested manufacturers, and
        # as many from the rest, so a long list cannot slow the run down.
        ranked = [(rank(candidate), candidate) for candidate in candidates]
        wanted = [item for item in ranked if item[0] < len(preferences)][:MAX_CANDIDATES_PER_SOURCE]
        others = [item for item in ranked if item[0] >= len(preferences)][:MAX_CANDIDATES_PER_SOURCE]
        if wanted:
            report(f"    {source_name}: {len(wanted)} SDS link(s) from a requested manufacturer")
        for position, candidate in wanted + others:
            if position == 0:
                if try_candidate(source_name, candidate):
                    return result
            elif strict and position > len(preferences):
                continue  # strict mode: a manufacturer the website names, but not a requested one
            else:
                put_aside.append((position, len(put_aside), source_name, candidate))

    # Links put aside: next requested manufacturers, then unnamed, then others.
    announced = False
    for position, _, source_name, candidate in sorted(put_aside):
        if position >= len(preferences) and not announced:
            announced = True
            report(f"  No usable SDS from {result.requested} - "
                   + ("checking links whose manufacturer is not named." if strict
                      else "trying other manufacturers."))
        if try_candidate(source_name, candidate):
            return result

    # --- Step 4: use the unverified backup, if there is one -----------------
    if unverified_backup is not None:
        pdf_bytes, candidate, website_match = unverified_backup
        save_pdf(pdf_bytes, target)
        result.status = "success"
        result.source = candidate.source
        result.supplier = candidate.supplier
        result.cas_verified = "NO - check manually"
        result.note = "PDF text unreadable (scanned?); CAS match comes from the website only"
        if preferences:
            result.maker_match = _maker_match_text(website_match, False)
        report(f"    Saved {target.name} from {candidate.source}, but the CAS number could "
               "not be confirmed inside the PDF - please check it by eye.")
        return result

    result.status = "failed"
    if strict and makers_offered:
        result.attempts.append("SDS offered by: " + ", ".join(makers_offered))
        report(f"  No SDS from {result.requested}. Offered by: " + ", ".join(makers_offered))
    report(f"  No SDS found for {cas}.")
    return result


# ---------------------------------------------------------------------------
# The whole list
# ---------------------------------------------------------------------------

def run_batch(
    parsed: ParsedInput,
    download_folder: str | Path,
    input_description: str,
    overwrite: bool = False,
    report: Callable[[str], None] = print,
    progress: Callable[[int, int], None] | None = None,
    stop_event: threading.Event | None = None,
    session: requests.Session | None = None,
    sources: Sequence[tuple[str, SourceFunction]] = SDS_SOURCES,
    pause_seconds: float = PAUSE_BETWEEN_CHEMICALS_SECONDS,
    preferred: Sequence[str] = (),
    strict: bool = False,
) -> BatchResult:
    """
    Download SDS files for every valid CAS number in `parsed`, then write the log.

    Arguments:
        parsed            - the output of cas.parse_text_list() or cas.parse_csv_file()
        download_folder   - folder where "<CAS>.pdf" files and the log are saved
        input_description - short text for the log, e.g. "CSV file: C:/lists/bath.csv"
        overwrite         - True = replace PDFs that already exist in the folder
        preferred         - manufacturers to prefer for every CAS number, most wanted
                            first. A manufacturer given on the line itself ("@ Gelest")
                            comes before these.
        strict            - True = only save SDS files from a requested manufacturer
        report            - function that receives progress messages (default: print)
        progress          - function that receives (number done, total number)
        stop_event        - when the GUI's Stop button sets this, we stop after
                            the chemical currently being processed
        session, sources, pause_seconds - normally left at their defaults
                            (the tests replace them to work without internet)
    """
    download_folder = Path(download_folder)
    download_folder.mkdir(parents=True, exist_ok=True)
    session = session or create_session()
    started = datetime.now()
    report(f"Python: {python_description()}")
    report(f"Certificates: {certificate_store_description()}")
    batch = BatchResult()
    total = len(parsed.valid_cas)

    report(f"{total} valid CAS number(s) to process, {len(parsed.invalid)} invalid entr"
           f"{'y' if len(parsed.invalid) == 1 else 'ies'} skipped.")
    if progress:
        progress(0, total)

    for index, cas in enumerate(parsed.valid_cas):
        if stop_event is not None and stop_event.is_set():
            batch.cancelled = True
            batch.not_processed = parsed.valid_cas[index:]
            report("Stopped by user.")
            break

        report(f"[{index + 1}/{total}] {cas}")
        preferences = manufacturer_preferences(parsed.manufacturers.get(cas, ""), preferred)
        try:
            result = process_one_cas(cas, download_folder, session, overwrite, report, sources,
                                     preferences, strict)
        except Exception as error:  # safety net: one bad chemical must not stop the run
            result = CasResult(cas=cas, attempts=[f"unexpected error: {error}"])
            report(f"  Unexpected error for {cas}: {error}")

        (batch.successes if result.status == "success" else batch.failures).append(result)
        if not batch.certificate_warning_shown and failed_on_certificates(result):
            batch.certificate_warning_shown = True
            report("  WARNING: " + NETWORK_WARNING[0])
            for text in NETWORK_WARNING[1:]:
                report("  " + text)
        if progress:
            progress(index + 1, total)

        # Be polite to the websites: short pause before the next chemical.
        if index + 1 < total and result.source != "already in folder":
            time.sleep(pause_seconds)

    batch.summary_path = write_hazard_summary(batch, parsed, download_folder, started)
    batch.preferred = [display_name(name) for name in preferred]
    batch.strict = strict
    batch.log_path = write_log(batch, parsed, download_folder, input_description, started, datetime.now())
    report(f"Log file written: {batch.log_path}")
    report(f"Hazard summary written: {batch.summary_path}")
    return batch


# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------

def _date_cell(info: SdsInfo | None) -> str:
    """'2021-12-24 OUTDATED', '2024-03-15' or '?' for the log table."""
    if info is None or info.sds_date is None:
        return "?"
    return f"{info.sds_date:%Y-%m-%d}" + (" OUTDATED" if info.outdated else "")


def write_hazard_summary(
    batch: BatchResult,
    parsed: ParsedInput,
    download_folder: Path,
    started: datetime,
) -> Path:
    """
    Write the hazard summary spreadsheet (CSV, opens in Excel) and return its path.

    One row per CAS number, in the order they were entered, including the failed
    ones so the sheet covers the whole list.
    """
    summary_path = download_folder / f"SDS_hazard_summary_{started:%Y-%m-%d_%H-%M-%S}.csv"
    results = {r.cas: r for r in batch.successes + batch.failures}
    headers = [
        "CAS Number", "Chemical name (PubChem)", "Status", "SDS file", "Supplier", "Found via",
        "Requested manufacturer", "From requested manufacturer", "Signal word", "H-codes", "Hazard statements", "Hazards read from", "SDS date", "Date type",
        "Age (years)", f"Outdated (> {SDS_MAX_AGE_YEARS} years)", "CAS in PDF",
    ]

    # newline="" is required by the csv module; utf-8-sig makes Excel show
    # special characters correctly (see write_log).
    with summary_path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(headers)
        for cas in parsed.valid_cas:
            r = results.get(cas)
            if r is None:  # not processed because the user pressed Stop
                continue
            info = r.info
            if r.status != "success":
                status = "no SDS found"
            elif r.source == "already in folder":
                status = "already in folder"
            else:
                status = "downloaded"
            if info is None:
                # Failed, or the PDF text could not be read (scanned SDS).
                hazard_cells = ["", "", "", "PDF text unreadable" if r.status == "success" else "", "", "", "", ""]
            else:
                hazard_cells = [
                    info.signal_word or "not found",
                    ", ".join(info.hazard_codes) or "none found",
                    describe_codes(info.hazard_codes),
                    "Section 2" if info.section_2_found else "whole SDS (Section 2 not found)",
                    f"{info.sds_date:%Y-%m-%d}" if info.sds_date else "not found",
                    info.date_kind,
                    f"{info.age_years:.1f}" if info.sds_date else "",
                    "YES" if info.outdated else ("no" if info.sds_date else "unknown"),
                ]
            writer.writerow(
                [r.cas, r.chemical_name, status,
                 f"{r.cas}.pdf" if r.status == "success" else "",
                 r.supplier, r.source, r.requested, r.maker_match]
                + hazard_cells
                + [r.cas_verified]
            )
    return summary_path


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Format rows as a plain-text table with aligned columns (readable in Notepad)."""
    widths = [len(header) for header in headers]
    for row in rows:
        widths = [max(width, len(cell)) for width, cell in zip(widths, row)]

    def line(cells: list[str]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths)).rstrip()

    return [line(headers), line(["-" * width for width in widths])] + [line(row) for row in rows]


def write_log(
    batch: BatchResult,
    parsed: ParsedInput,
    download_folder: Path,
    input_description: str,
    started: datetime,
    finished: datetime,
) -> Path:
    """Write the log file into the download folder and return its path."""
    log_path = download_folder / f"SDS_download_log_{started:%Y-%m-%d_%H-%M-%S}.txt"

    lines = [
        "SDS DOWNLOAD LOG",
        "=" * 16,
        f"Started:          {started:%Y-%m-%d %H:%M:%S}",
        f"Finished:         {finished:%Y-%m-%d %H:%M:%S}",
        f"Input:            {input_description}",
        f"Download folder:  {download_folder}",
        f"Python:           {python_description()}",
        f"Certificates:     {certificate_store_description()}",
        f"Entries read:     {parsed.entries_read}"
        + (f"  (duplicates ignored: {parsed.duplicates_ignored})" if parsed.duplicates_ignored else ""),
    ]
    if batch.summary_path:
        lines.append(f"Hazard summary:   {batch.summary_path.name}")
    any_requested = any(r.requested for r in batch.successes + batch.failures)
    if any_requested:
        lines.append("Manufacturers:    "
                     + (f"preferred {', '.join(batch.preferred)}" if batch.preferred else "as given per line")
                     + (" - strict (no other manufacturer)" if batch.strict else
                        " - other manufacturers used when none found"))
    lines += [
        "",
        "SUMMARY",
        f"  Successful: {len(batch.successes)}",
        f"  Failed:     {len(batch.failures)}",
        f"  Invalid:    {len(parsed.invalid)}",
    ]
    outdated = [r for r in batch.successes if r.info and r.info.outdated]
    if outdated:
        lines.append(f"  Outdated SDS (older than {SDS_MAX_AGE_YEARS} years): {len(outdated)}")
    if batch.cancelled:
        lines.append(f"  Not processed (run stopped by user): {len(batch.not_processed)}")

    certificate_failures = sum(failed_on_certificates(r) for r in batch.failures)
    if certificate_failures:
        lines += ["", f"NETWORK PROBLEM ({certificate_failures} CAS number(s)): " + NETWORK_WARNING[0]]
        lines += [f"  {text}" for text in NETWORK_WARNING[1:]]

    lines += ["", f"SUCCESSFUL ({len(batch.successes)})"]
    if batch.successes:
        headers = ["CAS Number", "File", "Chemical name (PubChem)", "Found via", "Supplier", "CAS in PDF",
                   "SDS date", "Note"]
        rows = [[r.cas, f"{r.cas}.pdf", r.chemical_name or "-", r.source, r.supplier or "-",
                 r.cas_verified, _date_cell(r.info), r.note] for r in batch.successes]
        if any_requested:
            # Two extra columns before "Note": what was asked for, and whether we got it.
            headers[-1:-1] = ["Requested", "From requested"]
            for row, r in zip(rows, batch.successes):
                row[-1:-1] = [r.requested or "-", r.maker_match or "-"]
        lines += _table(headers, rows)
    else:
        lines.append("  (none)")

    if outdated:
        lines += ["", f"OUTDATED SDS - older than {SDS_MAX_AGE_YEARS} years ({len(outdated)})",
                  "  Ask the supplier for the current version, or tick 'Overwrite' and run again.",
                  "  A newer SDS may not exist when the product is unchanged."]
        lines += [f"  {r.cas}  {r.chemical_name or '(no PubChem name)'}  -  {r.info.date_kind} date "
                  f"{r.info.sds_date:%Y-%m-%d} ({r.info.age_years:.1f} years)" for r in outdated]

    lines += ["", f"FAILED ({len(batch.failures)})"]
    if batch.failures:
        # Each CAS number gets a heading line, then one indented line per source.
        for r in batch.failures:
            lines.append(f"  {r.cas}  {r.chemical_name or '(no PubChem name)'}"
                         + (f"  -  requested: {r.requested}" if r.requested else ""))
            lines += [f"      {attempt}" for attempt in r.attempts] or ["      -"]
    else:
        lines.append("  (none)")

    lines += ["", f"INVALID ({len(parsed.invalid)})"]
    if parsed.invalid:
        lines += _table(["Entry as given", "Reason"], [[e.text, e.reason] for e in parsed.invalid])
    else:
        lines.append("  (none)")

    if batch.not_processed:
        lines += ["", f"NOT PROCESSED - run stopped by user ({len(batch.not_processed)})"]
        lines += [f"  {cas}" for cas in batch.not_processed]

    lines += [
        "",
        "REMINDER: an SDS downloaded here may come from a different supplier than the",
        "product you actually use. Composition, hazard classification and revision date",
        "can differ between suppliers; keep the SDS supplied with your purchased product",
        "for regulatory (e.g. OSHA HazCom / REACH) purposes.",
    ]

    # utf-8-sig adds an invisible marker that makes Windows Notepad and Excel
    # display special characters (e.g. Greek letters in names) correctly.
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return log_path
