"""
Test helpers: a tiny PDF maker and a fake internet connection.

The tests must not depend on real websites (they change, and they are slow), so
FakeSession pretends to be a requests.Session and answers from a dictionary.
"""

from __future__ import annotations

import json


def make_pdf(text: str) -> bytes:
    """
    Build a minimal, valid one-page PDF that contains `text`.

    Pass an empty string to get a PDF with no text at all (like a scanned SDS).
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1") if text else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_position = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        pdf += b"%010d 00000 n \n" % offset
    pdf += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_position)
    return bytes(pdf)


class FakeResponse:
    """Just enough of requests.Response for our code."""

    def __init__(self, status_code: int = 200, content: bytes | str = b"") -> None:
        self.status_code = status_code
        self.content = content.encode() if isinstance(content, str) else content
        self.text = self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def iter_content(self, chunk_size: int = 1024):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]

    # "with session.get(...) as response:" needs these two methods
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeSession:
    """
    Pretends to be a requests.Session.

    `pages` maps a URL (the part before any "?") to a FakeResponse.
    Any other URL answers "404 Not Found". Every requested URL is recorded.
    """

    def __init__(self, pages: dict[str, FakeResponse] | None = None) -> None:
        self.pages = pages or {}
        self.requested: list[str] = []

    def get(self, url: str, **kwargs) -> FakeResponse:
        self.requested.append(url)
        return self.pages.get(url.split("?")[0], FakeResponse(404))

    post = get
