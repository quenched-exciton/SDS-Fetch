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

The GUI calls run_batch() from a background thread. run_batch() reports what it
is doing through two "callback" functions supplied by the caller:
    report(message)          -> one line of text for the progress box
    progress(done, total)    -> how many CAS numbers are finished
"""

from __future__ import annotations

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
from .sources import (
    MAX_CANDIDATES_PER_SOURCE,
    REQUEST_TIMEOUT,
    SDS_SOURCES,
    SdsCandidate,
    SourceFunction,
    create_session,
    lookup_chemical_name,
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


@dataclass
class BatchResult:
    """Everything that happened during one run of the program."""

    successes: list[CasResult] = field(default_factory=list)
    failures: list[CasResult] = field(default_factory=list)
    not_processed: list[str] = field(default_factory=list)  # left over after "Stop"
    log_path: Path | None = None
    cancelled: bool = False


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
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_texts = []
        for page in reader.pages[:PAGES_TO_SEARCH]:
            page_texts.append(page.extract_text() or "")
        text = "\n".join(page_texts)
    except Exception:  # pypdf can raise many different errors for broken files
        return None

    if not text.strip():
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


# ---------------------------------------------------------------------------
# One CAS number
# ---------------------------------------------------------------------------

def process_one_cas(
    cas: str,
    download_folder: Path,
    session: requests.Session,
    overwrite: bool = False,
    report: Callable[[str], None] = print,
    sources: Sequence[tuple[str, SourceFunction]] = SDS_SOURCES,
) -> CasResult:
    """Find, check and save the SDS for a single CAS number (see steps at the top)."""
    result = CasResult(cas=cas)
    target = download_folder / f"{cas}.pdf"

    # --- Step 1: file already there? --------------------------------------
    if target.exists() and not overwrite:
        result.status = "success"
        result.source = "already in folder"
        result.cas_verified = "-"
        result.note = "file already existed, not downloaded again"
        report(f"  {cas}.pdf already exists - skipped (tick 'Overwrite' to replace it).")
        return result

    # --- Step 2: chemical identity from PubChem ----------------------------
    try:
        result.chemical_name = lookup_chemical_name(cas, session) or ""
    except Exception as error:  # PubChem problems must never stop the SDS search
        result.attempts.append(f"PubChem: {error}")
    if result.chemical_name:
        report(f"  Identified as: {result.chemical_name}")
    else:
        report("  Not found in PubChem (normal for many polymers and mixtures) - searching anyway.")

    # --- Step 3: try each SDS source ---------------------------------------
    # Remember the first PDF we could not verify, in case nothing better turns up.
    unverified_backup: tuple[bytes, SdsCandidate] | None = None

    for source_name, find_function in sources:
        report(f"  Searching {source_name} ...")
        try:
            candidates = find_function(cas, session)
        except Exception as error:  # network errors, blocked requests, changed pages
            result.attempts.append(f"{source_name}: {error}")
            report(f"    {source_name}: search failed ({error})")
            continue

        if not candidates:
            result.attempts.append(f"{source_name}: no SDS found")
            report(f"    {source_name}: no SDS found")
            continue

        for candidate in candidates[:MAX_CANDIDATES_PER_SOURCE]:
            try:
                pdf_bytes = download_pdf(candidate.url, session)
            except Exception as error:
                result.attempts.append(f"{source_name}: download failed ({error})")
                report(f"    {source_name}: download failed ({error})")
                continue

            found = pdf_mentions_cas(pdf_bytes, cas)
            if found:
                save_pdf(pdf_bytes, target)
                result.status = "success"
                result.source = candidate.source
                result.supplier = candidate.supplier
                result.cas_verified = "yes"
                report(f"    Saved {target.name} from {candidate.source}"
                       + (f" ({candidate.supplier})" if candidate.supplier else "")
                       + " - CAS number confirmed in the PDF.")
                return result

            if found is False:
                # Readable PDF, but for a different substance: never keep it.
                result.attempts.append(f"{source_name}: PDF did not contain {cas}, rejected")
                report(f"    {source_name}: PDF does not mention {cas} - rejected")
            elif candidate.exact_match:
                result.attempts.append(f"{source_name}: PDF text could not be read")
                report(f"    {source_name}: PDF text could not be read - kept as a backup")
                if unverified_backup is None:
                    unverified_backup = (pdf_bytes, candidate)
            else:
                # Unreadable PDF from a loose search hit: too risky to keep.
                result.attempts.append(f"{source_name}: unreadable PDF from an inexact match, rejected")
                report(f"    {source_name}: PDF text could not be read and the website did not "
                       "confirm the CAS number - rejected")

    # --- Step 4: use the unverified backup, if there is one -----------------
    if unverified_backup is not None:
        pdf_bytes, candidate = unverified_backup
        save_pdf(pdf_bytes, target)
        result.status = "success"
        result.source = candidate.source
        result.supplier = candidate.supplier
        result.cas_verified = "NO - check manually"
        result.note = "PDF text unreadable (scanned?); CAS match comes from the website only"
        report(f"    Saved {target.name} from {candidate.source}, but the CAS number could "
               "not be confirmed inside the PDF - please check it by eye.")
        return result

    result.status = "failed"
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
) -> BatchResult:
    """
    Download SDS files for every valid CAS number in `parsed`, then write the log.

    Arguments:
        parsed            - the output of cas.parse_text_list() or cas.parse_csv_file()
        download_folder   - folder where "<CAS>.pdf" files and the log are saved
        input_description - short text for the log, e.g. "CSV file: C:/lists/bath.csv"
        overwrite         - True = replace PDFs that already exist in the folder
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
        try:
            result = process_one_cas(cas, download_folder, session, overwrite, report, sources)
        except Exception as error:  # safety net: one bad chemical must not stop the run
            result = CasResult(cas=cas, attempts=[f"unexpected error: {error}"])
            report(f"  Unexpected error for {cas}: {error}")

        (batch.successes if result.status == "success" else batch.failures).append(result)
        if progress:
            progress(index + 1, total)

        # Be polite to the websites: short pause before the next chemical.
        if index + 1 < total and result.source != "already in folder":
            time.sleep(pause_seconds)

    batch.log_path = write_log(batch, parsed, download_folder, input_description, started, datetime.now())
    report(f"Log file written: {batch.log_path}")
    return batch


# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------

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
        f"Entries read:     {parsed.entries_read}"
        + (f"  (duplicates ignored: {parsed.duplicates_ignored})" if parsed.duplicates_ignored else ""),
        "",
        "SUMMARY",
        f"  Successful: {len(batch.successes)}",
        f"  Failed:     {len(batch.failures)}",
        f"  Invalid:    {len(parsed.invalid)}",
    ]
    if batch.cancelled:
        lines.append(f"  Not processed (run stopped by user): {len(batch.not_processed)}")

    lines += ["", f"SUCCESSFUL ({len(batch.successes)})"]
    if batch.successes:
        lines += _table(
            ["CAS Number", "File", "Chemical name (PubChem)", "Found via", "Supplier", "CAS in PDF", "Note"],
            [[r.cas, f"{r.cas}.pdf", r.chemical_name or "-", r.source, r.supplier or "-",
              r.cas_verified, r.note] for r in batch.successes],
        )
    else:
        lines.append("  (none)")

    lines += ["", f"FAILED ({len(batch.failures)})"]
    if batch.failures:
        lines += _table(
            ["CAS Number", "Chemical name (PubChem)", "What each source reported"],
            [[r.cas, r.chemical_name or "-", "; ".join(r.attempts) or "-"] for r in batch.failures],
        )
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
