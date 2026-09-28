"""Tests for downloading, checking and saving SDS files, and for the log (no internet needed)."""

import threading

from fakes import FakeResponse, FakeSession, make_pdf
from sds_downloader.cas import parse_text_list
from sds_downloader.downloader import pdf_mentions_cas, process_one_cas, run_batch
from sds_downloader.sources import SdsCandidate, SourceError

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
