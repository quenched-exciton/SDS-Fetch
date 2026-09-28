"""
sources.py - Where the program looks for chemical names and SDS PDF files.

Every SDS "source" below is a function with the same shape:

    find_on_<website>(cas, session)  ->  list of SdsCandidate

It searches one website for the CAS number and returns links that SHOULD point to
an SDS PDF. It does not download anything; downloader.py downloads each link,
checks that it really is a PDF, and checks that the CAS number is printed inside it.

IMPORTANT - websites change. Supplier websites are redesigned from time to time
and some of them block automated requests. When that happens a source simply
returns no results (the reason is written to the log file) and the program moves
on to the next source. If one source stops working permanently, fix or remove
its function and its line in SDS_SOURCES at the bottom of this file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------------------------------------------------------------------
# General settings
# ---------------------------------------------------------------------------

# Seconds to wait for a website to answer before giving up on that request.
REQUEST_TIMEOUT = 20

# How many links to try from a single website before moving to the next website.
MAX_CANDIDATES_PER_SOURCE = 3

# Many websites refuse requests that do not look like they come from a normal
# web browser, so we send the same headers a desktop browser would send.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class SourceError(Exception):
    """Raised when a website answers with an error or with something unexpected."""


@dataclass
class SdsCandidate:
    """A link that probably leads to an SDS PDF."""

    url: str             # full web address of the (hopefully) PDF file
    source: str          # website that gave us the link, e.g. "Fisher Scientific"
    supplier: str = ""   # manufacturer named on that website, if it tells us
    exact_match: bool = True
    # exact_match is True when the website itself linked this file to our exact
    # CAS number. If a PDF's text cannot be read (e.g. a scanned image) we only
    # keep it when exact_match is True, so we never save a different chemical's
    # SDS under the wrong CAS number.


def create_session() -> requests.Session:
    """
    Create a requests "Session".

    A session re-uses network connections and remembers cookies between requests,
    which makes repeated requests faster and helps with websites that set a cookie
    on the first visit. It is also set up to retry automatically (up to 2 times,
    waiting a little longer each time) when a server is temporarily unavailable.
    """
    session = requests.Session()
    session.headers.update(BROWSER_HEADERS)

    retry_rule = Retry(
        total=2,
        backoff_factor=1.0,                      # wait 1 s, then 2 s between retries
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(max_retries=retry_rule)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_ok(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """GET a web page and raise SourceError unless the server answered '200 OK'."""
    response = session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code} from {url.split('?')[0]}")
    return response


def _post_ok(session: requests.Session, url: str, **kwargs) -> requests.Response:
    """POST to a web address and raise SourceError unless the answer is '200 OK'."""
    response = session.post(url, timeout=REQUEST_TIMEOUT, **kwargs)
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code} from {url.split('?')[0]}")
    return response


# ---------------------------------------------------------------------------
# Chemical identity: PubChem (US National Library of Medicine)
# ---------------------------------------------------------------------------

def lookup_chemical_name(cas: str, session: requests.Session) -> str | None:
    """
    Ask PubChem which chemical a CAS number belongs to.

    Uses the PubChem PUG-REST service, which is free and documented at
    https://pubchem.ncbi.nlm.nih.gov/docs/pug-rest
    Example: 64-17-5 -> "Ethanol".

    Returns None when PubChem does not know the CAS number. That does NOT mean the
    CAS number is wrong: many polymers, mixtures and industrial products are not
    in PubChem. The SDS search still runs.
    """
    url = (
        "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
        f"{quote(cas)}/property/Title/JSON"
    )
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return None  # PubChem has no compound with this CAS number as a synonym
    if response.status_code != 200:
        raise SourceError(f"PubChem answered HTTP {response.status_code}")

    properties = response.json().get("PropertyTable", {}).get("Properties", [])
    if properties and properties[0].get("Title"):
        return properties[0]["Title"]
    return None


# ---------------------------------------------------------------------------
# SDS source 1: Fisher Scientific (SDS search page)
# ---------------------------------------------------------------------------

FISHER_SEARCH_URL = "https://www.fishersci.com/us/en/catalog/search/sds"


def parse_fisher_results(html: str, cas: str) -> list[str]:
    """
    Pull SDS links for `cas` out of a Fisher Scientific SDS search results page.

    Each search result on that page shows a structure image whose file name
    contains the CAS number (inside a block with class "msds_img"). Next to it is
    a block with class "catalog_data" that lists catalog numbers; each catalog
    number is a link that downloads that product's SDS PDF.
    """
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []

    for image_block in soup.find_all(class_="msds_img"):
        # Keep only results whose structure image belongs to OUR CAS number.
        image = image_block.find("img")
        if image is None or cas not in (image.get("src") or ""):
            continue

        catalog_block = image_block.find_next_sibling(class_="catalog_data")
        if catalog_block is None:
            continue
        for anchor in catalog_block.find_all("a", href=True):
            links.append(urljoin("https://www.fishersci.com", anchor["href"]))

    return links


def find_on_fisher(cas: str, session: requests.Session) -> list[SdsCandidate]:
    """Search the Fisher Scientific SDS database for the CAS number."""
    response = _get_ok(
        session,
        FISHER_SEARCH_URL,
        params={"selectLang": "", "store": "", "msdsKeyword": cas},
    )
    links = parse_fisher_results(response.text, cas)
    return [SdsCandidate(url, "Fisher Scientific", "Fisher Scientific") for url in links]


# ---------------------------------------------------------------------------
# SDS source 2: ChemicalSafety.com free SDS database
# (collects SDS files from many manufacturers, e.g. Sigma-Aldrich, Fisher, VWR)
# ---------------------------------------------------------------------------

CHEMICALSAFETY_SEARCH_URL = "https://chemicalsafety.com/sds1/sds_retriever.php?action=search"


def _parse_date(text: str) -> datetime:
    """Read a revision date in any common format; unknown formats sort as oldest."""
    for date_format in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%d.%m.%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text.strip(), date_format)
        except (ValueError, AttributeError):
            continue
    return datetime.min


def parse_chemicalsafety_results(data: dict, cas: str) -> list[tuple[str, str]]:
    """
    Pick SDS links for `cas` out of a ChemicalSafety.com search answer.

    The answer is a table: data["cols"] lists the column names and data["rows"]
    holds the rows. Columns we use:
        CAS          -> CAS number of the product
        MANUFACT     -> manufacturer
        HTTPMSDSREF  -> web address of the SDS file
        (a column whose name contains "revision") -> SDS revision date

    Returns a list of (url, manufacturer), newest revision first. Links that end
    in ".pdf" are listed before other links.
    """
    column_names = [str(column.get("name", "")).upper() for column in data.get("cols", [])]
    try:
        cas_col = column_names.index("CAS")
        maker_col = column_names.index("MANUFACT")
        url_col = column_names.index("HTTPMSDSREF")
    except ValueError as error:
        raise SourceError("ChemicalSafety answer is missing an expected column") from error
    revision_col = next((i for i, name in enumerate(column_names) if "REVISION" in name), None)

    matches = []
    for row in data.get("rows", []):
        row_cas = str(row[cas_col]).strip()
        url = str(row[url_col]).strip()
        if row_cas != cas or not url.lower().startswith("http"):
            continue
        revision = _parse_date(str(row[revision_col])) if revision_col is not None else datetime.min
        matches.append((url, str(row[maker_col]).strip(), revision))

    # Sort: ".pdf" links first, then the newest revision first.
    matches.sort(key=lambda match: (not match[0].lower().endswith(".pdf"), -match[2].toordinal()))
    return [(url, maker) for url, maker, _ in matches]


def find_on_chemicalsafety(cas: str, session: requests.Session) -> list[SdsCandidate]:
    """Search the free ChemicalSafety.com SDS database for the CAS number."""
    # This is the same request the search box on https://chemicalsafety.com/sds-search/
    # sends. The odd-looking fields ("Bee", "HostName", ...) are required by their server.
    search_request = {
        "IsContains": "false",
        "IncludeSynonyms": "false",
        "SearchSdsServer": "false",
        "Criteria": [f"cas|{cas}"],
        "HostName": "sfs website",
        "Bee": "stevia",
        "Action": "search",
        "SearchUrl": "",
        "ResultColumns": ["revision_date"],
    }
    response = _post_ok(session, CHEMICALSAFETY_SEARCH_URL, json=search_request)
    try:
        data = response.json()
    except json.JSONDecodeError as error:
        raise SourceError("ChemicalSafety did not answer with JSON") from error

    return [
        SdsCandidate(url, "ChemicalSafety.com", maker)
        for url, maker in parse_chemicalsafety_results(data, cas)
    ]


# ---------------------------------------------------------------------------
# SDS source 3: VWR / Avantor (SDS search page)
# ---------------------------------------------------------------------------

VWR_SEARCH_URL = "https://us.vwr.com/store/msds"


def parse_vwr_results(html: str, cas: str) -> list[SdsCandidate]:
    """
    Pull SDS links out of a VWR SDS search results page.

    The results are a table. The SDS link sits in a cell marked
    data-title="SDS" and the manufacturer in a cell marked data-title="Manufacturer".
    Rows that mention our CAS number are listed first.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows_with_cas, other_rows = [], []

    for row in soup.find_all("tr"):
        sds_cell = row.find("td", attrs={"data-title": "SDS"})
        link = sds_cell.find("a", href=True) if sds_cell else None
        if link is None:
            continue
        maker_cell = row.find("td", attrs={"data-title": "Manufacturer"})
        maker = maker_cell.get_text(strip=True) if maker_cell else ""
        mentions_cas = cas in row.get_text(" ")
        candidate = SdsCandidate(urljoin("https://us.vwr.com", link["href"]), "VWR", maker, mentions_cas)
        (rows_with_cas if mentions_cas else other_rows).append(candidate)

    return rows_with_cas + other_rows


def find_on_vwr(cas: str, session: requests.Session) -> list[SdsCandidate]:
    """Search the VWR (Avantor) SDS database for the CAS number."""
    response = _get_ok(session, VWR_SEARCH_URL, params={"keyword": cas})
    return parse_vwr_results(response.text, cas)


# ---------------------------------------------------------------------------
# SDS source 4: Fluorochem (their product search service)
# ---------------------------------------------------------------------------

FLUOROCHEM_SEARCH_URL = "https://dougdiscovery.com/api/v1/molecules/search"
FLUOROCHEM_FILE_HOST = "https://7128445.app.netsuite.com"


def parse_fluorochem_results(data: dict, cas: str) -> list[SdsCandidate]:
    """
    Pull English SDS links out of a Fluorochem search answer.

    Each entry in data["data"] describes a product; its English SDS link is at
    entry["molecule"]["sds"]["custrecord_sdslink_en"]. Products whose data
    mention our CAS number are listed first.
    """
    matching, others = [], []
    for entry in data.get("data") or []:
        sds_info = (entry.get("molecule") or {}).get("sds") or {}
        partial_link = sds_info.get("custrecord_sdslink_en")
        if not partial_link:
            continue
        url = urljoin(FLUOROCHEM_FILE_HOST, partial_link)
        mentions_cas = cas in json.dumps(entry)
        candidate = SdsCandidate(url, "Fluorochem", "Fluorochem", mentions_cas)
        (matching if mentions_cas else others).append(candidate)
    return matching + others


def find_on_fluorochem(cas: str, session: requests.Session) -> list[SdsCandidate]:
    """Search Fluorochem's catalogue for the CAS number."""
    response = _post_ok(session, FLUOROCHEM_SEARCH_URL, json={"q": cas, "offset": 0, "limit": 12})
    try:
        data = response.json()
    except json.JSONDecodeError as error:
        raise SourceError("Fluorochem did not answer with JSON") from error
    return parse_fluorochem_results(data, cas)


# ---------------------------------------------------------------------------
# SDS source 5: ChemBlink (one SDS page per CAS number)
# ---------------------------------------------------------------------------

def parse_chemblink_page(html: str, page_url: str) -> list[tuple[str, str]]:
    """
    Pull (url, supplier) pairs out of a ChemBlink SDS page.

    The page has links with the text "View / download". The supplier name is
    part of the PDF file name, e.g. ".../64-17-5_Sigma-Aldrich.pdf".
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for anchor in soup.find_all("a", href=True):
        if not re.search(r"view\s*/\s*download", anchor.get_text(" "), re.IGNORECASE):
            continue
        url = urljoin(page_url, anchor["href"])
        supplier_match = re.search(r"_([A-Za-z][A-Za-z\- ]+)\.pdf$", url)
        results.append((url, supplier_match.group(1) if supplier_match else ""))
    return results


def find_on_chemblink(cas: str, session: requests.Session) -> list[SdsCandidate]:
    """Look up ChemBlink's SDS page for the CAS number."""
    page_url = f"https://www.chemblink.com/MSDS/{quote(cas)}MSDS.htm"
    response = session.get(page_url, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return []  # ChemBlink simply has no page for this CAS number
    if response.status_code != 200:
        raise SourceError(f"HTTP {response.status_code} from ChemBlink")
    return [
        SdsCandidate(url, "ChemBlink", supplier)
        for url, supplier in parse_chemblink_page(response.text, page_url)
    ]


# ---------------------------------------------------------------------------
# The list of sources, in the order they are tried
# ---------------------------------------------------------------------------
# The program stops at the first source that gives a PDF containing the CAS number.
# To change the order, move lines up or down. To switch a source off, put a "#"
# in front of its line.

SourceFunction = Callable[[str, requests.Session], "list[SdsCandidate]"]

SDS_SOURCES: list[tuple[str, SourceFunction]] = [
    ("Fisher Scientific", find_on_fisher),
    ("ChemicalSafety.com", find_on_chemicalsafety),
    ("VWR", find_on_vwr),
    ("Fluorochem", find_on_fluorochem),
    ("ChemBlink", find_on_chemblink),
]
