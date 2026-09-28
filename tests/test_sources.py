"""
Tests for the website-reading code, using small made-up pages (no internet needed).

These check our parsing logic only. They cannot tell us whether the real websites
still look like this; see the README section "When a source stops working".
"""

import pytest

from fakes import FakeResponse, FakeSession
from sds_downloader.sources import (
    SourceError,
    lookup_chemical_name,
    parse_chemblink_page,
    parse_chemicalsafety_results,
    parse_fisher_results,
    parse_fluorochem_results,
    parse_vwr_results,
)


def test_pubchem_name_lookup():
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/64-17-5/property/Title/JSON"
    answer = '{"PropertyTable": {"Properties": [{"CID": 702, "Title": "Ethanol"}]}}'
    session = FakeSession({url: FakeResponse(200, answer)})
    assert lookup_chemical_name("64-17-5", session) == "Ethanol"


def test_pubchem_unknown_cas_gives_none():
    assert lookup_chemical_name("64-17-5", FakeSession()) is None   # FakeSession answers 404


def test_pubchem_server_error_raises():
    url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/64-17-5/property/Title/JSON"
    with pytest.raises(SourceError):
        lookup_chemical_name("64-17-5", FakeSession({url: FakeResponse(503)}))


def test_fisher_keeps_only_results_for_our_cas():
    html = """
    <div class="result">
      <div class="msds_img"><img src="/structures/67-64-1.gif"></div>
      <div class="catalog_data"><div class="catlog_items"><a href="/store/msds?partNumber=A18">A18</a></div></div>
    </div>
    <div class="result">
      <div class="msds_img"><img src="/structures/64-17-5.gif"></div>
      <div class="catalog_data"><div class="catlog_items">
        <a href="/store/msds?partNumber=A407">A407</a><a href="/store/msds?partNumber=A962">A962</a>
      </div></div>
    </div>"""
    assert parse_fisher_results(html, "64-17-5") == [
        "https://www.fishersci.com/store/msds?partNumber=A407",
        "https://www.fishersci.com/store/msds?partNumber=A962",
    ]


def test_chemicalsafety_filters_exact_cas_and_sorts_newest_pdf_first():
    data = {
        "cols": [{"name": "CAS"}, {"name": "MANUFACT"}, {"name": "HTTPMSDSREF"}, {"name": "revision_date"}],
        "rows": [
            ["64-17-5", "Old Maker", "https://example.com/old.pdf", "2015-01-01"],
            ["64-17-5", "New Maker", "https://example.com/new.pdf", "2023-06-30"],
            ["64-17-5", "Web Page", "https://example.com/sds?id=1", "2024-01-01"],
            ["67-64-1", "Acetone Maker", "https://example.com/acetone.pdf", "2024-01-01"],
            ["64-17-5", "No Link", "", "2024-01-01"],
        ],
    }
    assert parse_chemicalsafety_results(data, "64-17-5") == [
        ("https://example.com/new.pdf", "New Maker"),
        ("https://example.com/old.pdf", "Old Maker"),
        ("https://example.com/sds?id=1", "Web Page"),
    ]


def test_chemicalsafety_missing_columns_raise():
    with pytest.raises(SourceError):
        parse_chemicalsafety_results({"cols": [{"name": "CAS"}], "rows": []}, "64-17-5")


def test_vwr_lists_rows_with_our_cas_first():
    html = """<table>
      <tr><td data-title="Manufacturer">Other</td><td data-title="CAS">67-64-1</td>
          <td data-title="SDS"><a href="https://example.com/other.pdf">SDS</a></td></tr>
      <tr><td data-title="Manufacturer">VWR Chemicals</td><td data-title="CAS">64-17-5</td>
          <td data-title="SDS"><a href="/sds/ours.pdf">SDS</a></td></tr>
      <tr><td>header row without SDS link</td></tr>
    </table>"""
    candidates = parse_vwr_results(html, "64-17-5")
    assert [(c.url, c.supplier, c.exact_match) for c in candidates] == [
        ("https://us.vwr.com/sds/ours.pdf", "VWR Chemicals", True),
        ("https://example.com/other.pdf", "Other", False),
    ]


def test_fluorochem_builds_full_links():
    data = {"data": [
        {"molecule": {"cas": "67-64-1", "sds": {"custrecord_sdslink_en": "/core/media/other.pdf"}}},
        {"molecule": {"cas": "64-17-5", "sds": {"custrecord_sdslink_en": "/core/media/ours.pdf"}}},
        {"molecule": {"cas": "64-17-5", "sds": None}},
    ]}
    candidates = parse_fluorochem_results(data, "64-17-5")
    assert [(c.url, c.exact_match) for c in candidates] == [
        ("https://7128445.app.netsuite.com/core/media/ours.pdf", True),
        ("https://7128445.app.netsuite.com/core/media/other.pdf", False),
    ]


def test_chemblink_finds_download_links_and_supplier():
    html = '<a href="/other.htm">Home</a><a href="MSDSFiles/64-17-5_Sigma-Aldrich.pdf">View / download</a>'
    page_url = "https://www.chemblink.com/MSDS/64-17-5MSDS.htm"
    assert parse_chemblink_page(html, page_url) == [
        ("https://www.chemblink.com/MSDS/MSDSFiles/64-17-5_Sigma-Aldrich.pdf", "Sigma-Aldrich"),
    ]
