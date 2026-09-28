"""Tests for downloading, checking and saving SDS files, and for the log (no internet needed)."""

import threading

import requests

from fakes import FakeResponse, FakeSession, make_pdf
from sds_fetch.cas import parse_text_list
from sds_fetch.downloader import pdf_mentions_cas, process_one_cas, run_batch
from sds_fetch.sources import SdsCandidate, SourceError

PUBCHEM_ETHANOL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/64-17-5/property/Title/JSON"


def source_returning(*candidates):
    """Make a fake SDS source that always returns the given candidates."""
    return lambda cas, session: list(candidates)


def silent(message):
    """A 'report' function that ignores progress messages."""


# --- PDF text check ----------------------------------------------------------

def test_pdf_mentions_cas():
    assert pdf_mentions_cas(make_pdf("Ethanol CAS-No. 64-17-5 EC 200-578-6"), "64-17-5") is True
    assert pdf_mentions_cas(make_pdf("Acetone CAS-No. 67-64-1"), "64-17-5") is False
    assert pdf_mentions_cas(make_pdf("CAS 164-17-5"), "64-17-5") is False   # must not match inside a longer number
    assert pdf_mentions_cas(make_pdf(""), "64-17-5") is None                 # no text layer
    assert pdf_mentions_cas(b"%PDF-1.4 garbage", "64-17-5") is None          # damaged file


# --- One CAS number ----------------------------------------------------------

def test_verified_pdf_is_saved_with_cas_file_name(tmp_path):
    session = FakeSession({
        PUBCHEM_ETHANOL: FakeResponse(200, '{"PropertyTable": {"Properties": [{"Title": "Ethanol"}]}}'),
        "https://a.test/sds.pdf": FakeResponse(200, make_pdf("Ethanol CAS 64-17-5")),
    })
    sources = [("A", source_returning(SdsCandidate("https://a.test/sds.pdf", "Site A", "Maker A")))]

    result = process_one_cas("64-17-5", tmp_path, session, report=silent, sources=sources)

    assert result.status == "success"
    assert result.chemical_name == "Ethanol"
    assert (result.source, result.supplier, result.cas_verified) == ("Site A", "Maker A", "yes")
    assert (tmp_path / "64-17-5.pdf").read_bytes().startswith(b"%PDF")
    assert not list(tmp_path.glob("*.part"))   # temporary file was renamed


def test_falls_through_broken_sources_and_rejects_wrong_chemical(tmp_path):
    def broken_source(cas, session):
        raise SourceError("HTTP 403 from https://blocked.test")

    session = FakeSession({
        "https://b.test/web-page": FakeResponse(200, "<html>login</html>"),
        "https://b.test/acetone.pdf": FakeResponse(200, make_pdf("Acetone CAS 67-64-1")),
        "https://c.test/ethanol.pdf": FakeResponse(200, make_pdf("Ethanol CAS 64-17-5")),
    })
    sources = [
        ("Broken", broken_source),
        ("Empty", source_returning()),
        ("B", source_returning(SdsCandidate("https://b.test/web-page", "B"),
                               SdsCandidate("https://b.test/acetone.pdf", "B"))),
        ("C", source_returning(SdsCandidate("https://c.test/ethanol.pdf", "C"))),
    ]

    result = process_one_cas("64-17-5", tmp_path, session, report=silent, sources=sources)

    assert result.status == "success" and result.source == "C"
    assert (tmp_path / "64-17-5.pdf").exists()
    assert any("HTTP 403" in attempt for attempt in result.attempts)
    assert any("Empty: no SDS found" in attempt for attempt in result.attempts)
    assert any("did not return a PDF" in attempt for attempt in result.attempts)
    assert any("did not contain 64-17-5" in attempt for attempt in result.attempts)


def test_unreadable_pdf_kept_only_when_website_matched_exact_cas(tmp_path):
    session = FakeSession({"https://a.test/scan.pdf": FakeResponse(200, make_pdf(""))})

    loose = [("A", source_returning(SdsCandidate("https://a.test/scan.pdf", "A", exact_match=False)))]
    result = process_one_cas("64-17-5", tmp_path, session, report=silent, sources=loose)
    assert result.status == "failed"
    assert not (tmp_path / "64-17-5.pdf").exists()

    exact = [("A", source_returning(SdsCandidate("https://a.test/scan.pdf", "A", exact_match=True)))]
    result = process_one_cas("64-17-5", tmp_path, session, report=silent, sources=exact)
    assert result.status == "success"
    assert result.cas_verified.startswith("NO")
    assert (tmp_path / "64-17-5.pdf").exists()


def test_existing_file_is_skipped_unless_overwrite(tmp_path):
    (tmp_path / "64-17-5.pdf").write_bytes(b"old file")
    session = FakeSession({"https://a.test/sds.pdf": FakeResponse(200, make_pdf("CAS 64-17-5"))})
    sources = [("A", source_returning(SdsCandidate("https://a.test/sds.pdf", "A")))]

    result = process_one_cas("64-17-5", tmp_path, session, overwrite=False, report=silent, sources=sources)
    assert result.source == "already in folder"
    assert session.requested == []                      # nothing was downloaded
    assert (tmp_path / "64-17-5.pdf").read_bytes() == b"old file"

    result = process_one_cas("64-17-5", tmp_path, session, overwrite=True, report=silent, sources=sources)
    assert result.source == "A"
    assert (tmp_path / "64-17-5.pdf").read_bytes().startswith(b"%PDF")


# --- Whole list + log file ---------------------------------------------------

def test_run_batch_writes_log_with_all_three_lists(tmp_path):
    parsed = parse_text_list("64-17-5\n7732-18-5\n7732-18-4\nhello\n")
    session = FakeSession({"https://a.test/64-17-5.pdf": FakeResponse(200, make_pdf("CAS 64-17-5"))})

    def source(cas, session):
        return [SdsCandidate(f"https://a.test/{cas}.pdf", "Site A")]

    progress_calls = []
    batch = run_batch(parsed, tmp_path / "SDS", "typed/pasted list", report=silent,
                      progress=lambda done, total: progress_calls.append((done, total)),
                      session=session, sources=[("A", source)], pause_seconds=0)

    assert [r.cas for r in batch.successes] == ["64-17-5"]
    assert [r.cas for r in batch.failures] == ["7732-18-5"]
    assert progress_calls == [(0, 2), (1, 2), (2, 2)]
    assert (tmp_path / "SDS" / "64-17-5.pdf").exists()        # folder was created

    log_text = batch.log_path.read_text(encoding="utf-8-sig")
    assert batch.log_path.parent == tmp_path / "SDS"
    assert "Successful: 1" in log_text and "Failed:     1" in log_text and "Invalid:    2" in log_text
    successful_part = log_text.split("SUCCESSFUL")[1].split("FAILED")[0]
    failed_part = log_text.split("FAILED (")[1].split("INVALID")[0]
    invalid_part = log_text.split("INVALID (")[1]
    assert "64-17-5.pdf" in successful_part
    assert "7732-18-5" in failed_part and "A: download failed (HTTP 404)" in failed_part
    assert "7732-18-4" in invalid_part and "check digit" in invalid_part and "hello" in invalid_part


def test_stop_button_leaves_rest_unprocessed(tmp_path):
    parsed = parse_text_list("64-17-5\n7732-18-5\n")
    stop = threading.Event()

    def source(cas, session):
        stop.set()   # the user presses Stop while the first chemical is running
        return []

    batch = run_batch(parsed, tmp_path, "test", report=silent, stop_event=stop,
                      session=FakeSession(), sources=[("A", source)], pause_seconds=0)

    assert batch.cancelled
    assert [r.cas for r in batch.failures] == ["64-17-5"]
    assert batch.not_processed == ["7732-18-5"]
    assert "NOT PROCESSED" in batch.log_path.read_text(encoding="utf-8-sig")


def test_certificate_errors_give_short_messages_and_a_network_warning(tmp_path):
    # The same error a company security proxy caused in a real run on Windows.
    raw_error = (
        "HTTPSConnectionPool(host='www.fishersci.com', port=443): Max retries exceeded with url: "
        "/us/en/catalog/search/sds?msdsKeyword=64-17-5 (Caused by SSLError(SSLCertVerificationError(1, "
        "'[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate in "
        "certificate chain (_ssl.c:1007)')))"
    )

    def proxied_source(cas, session):
        raise requests.exceptions.SSLError(raw_error)

    def slow_source(cas, session):
        raise requests.exceptions.ReadTimeout("Read timed out. (read timeout=20)")

    messages = []
    parsed = parse_text_list("64-17-5\n7732-18-5\n")
    batch = run_batch(parsed, tmp_path, "test", report=messages.append, session=FakeSession(),
                      sources=[("A", proxied_source), ("B", proxied_source), ("C", slow_source)],
                      pause_seconds=0)

    # Each source gets one short line instead of the raw error text.
    attempts = batch.failures[0].attempts
    assert [a for a in attempts if not a.startswith("PubChem")] == [
        "A: security certificate refused (a company network proxy is probably re-signing HTTPS traffic)",
        "B: security certificate refused (a company network proxy is probably re-signing HTTPS traffic)",
        "C: no answer within 20 s (timed out)",
    ]

    # The progress box warns once, not once per chemical.
    assert sum("WARNING" in m for m in messages) == 1

    log_text = batch.log_path.read_text(encoding="utf-8-sig")
    assert "NETWORK PROBLEM (2 CAS number(s))" in log_text
    assert "Certificates:" in log_text
    assert max(len(line) for line in log_text.splitlines()) < 150   # no more giant rows
    assert "      A: security certificate refused" in log_text     # one indented line per source


def test_other_failures_do_not_trigger_network_warning(tmp_path):
    def blocked_source(cas, session):
        raise SourceError("HTTP 403 from https://blocked.test")

    batch = run_batch(parse_text_list("64-17-5\n"), tmp_path, "test", report=silent,
                      session=FakeSession(), sources=[("A", blocked_source)], pause_seconds=0)

    assert "NETWORK PROBLEM" not in batch.log_path.read_text(encoding="utf-8-sig")


# --- Hazard summary spreadsheet and outdated SDS ------------------------------

# One-line SDS texts for make_pdf (it cannot handle brackets in the text).
CURRENT_SDS = ("CAS 64-17-5 Revision Date 2025-06-01 SECTION 2: Hazards identification "
               "Signal word Danger H225 Highly flammable liquid and vapour. "
               "H319 Causes serious eye irritation. SECTION 3: Composition")
OLD_SDS = ("CAS 7732-18-5 Revision Date 24-Dec-2015 SECTION 2: Hazards identification "
           "Not a hazardous substance or mixture according to Regulation. Signal word none "
           "SECTION 3: Composition")


def test_run_batch_writes_hazard_summary_and_flags_old_sds(tmp_path):
    import csv

    parsed = parse_text_list("64-17-5\n7732-18-5\n67-64-1\n")
    session = FakeSession({
        "https://a.test/64-17-5.pdf": FakeResponse(200, make_pdf(CURRENT_SDS)),
        "https://a.test/7732-18-5.pdf": FakeResponse(200, make_pdf(OLD_SDS)),
    })

    def source(cas, session):
        return [SdsCandidate(f"https://a.test/{cas}.pdf", "Site A", "Maker A")]

    messages = []
    batch = run_batch(parsed, tmp_path, "test", report=messages.append, session=session,
                      sources=[("A", source)], pause_seconds=0)

    with batch.summary_path.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["CAS Number"] for row in rows] == ["64-17-5", "7732-18-5", "67-64-1"]  # input order

    ethanol, water, acetone = rows
    assert ethanol["Status"] == "downloaded" and ethanol["SDS file"] == "64-17-5.pdf"
    assert ethanol["Signal word"] == "Danger"
    assert ethanol["H-codes"] == "H225, H319"
    assert ethanol["Hazard statements"].startswith("H225 Highly flammable liquid and vapor;")
    assert ethanol["Hazards read from"] == "Section 2"
    assert (ethanol["SDS date"], ethanol["Date type"]) == ("2025-06-01", "revision")
    assert water["H-codes"] == "none found" and water["Signal word"] == "none"
    assert water["Hazards read from"] == "Section 2"   # short Section 2 of a non-hazardous substance
    assert water["Outdated (> 3 years)"] == "YES"
    assert acetone["Status"] == "no SDS found" and acetone["H-codes"] == ""

    log_text = batch.log_path.read_text(encoding="utf-8-sig")
    assert "Outdated SDS (older than 3 years): 1" in log_text
    assert "OUTDATED SDS - older than 3 years (1)" in log_text
    assert "2015-12-24 OUTDATED" in log_text
    assert f"Hazard summary:   {batch.summary_path.name}" in log_text
    assert "Python:" in log_text
    assert any("Hazards: Danger: H225, H319" in m for m in messages)


def test_existing_file_still_gets_hazard_info(tmp_path):
    (tmp_path / "64-17-5.pdf").write_bytes(make_pdf(CURRENT_SDS))
    result = process_one_cas("64-17-5", tmp_path, FakeSession(), report=silent, sources=[])
    assert result.source == "already in folder"
    assert result.info.hazard_codes == ["H225", "H319"]
