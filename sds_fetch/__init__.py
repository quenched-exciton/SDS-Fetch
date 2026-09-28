"""
SDS Fetch - downloads Safety Data Sheet PDFs by CAS Registry Number.

Modules:
    cas.py        - reads the CAS list (typed text or CSV) and checks each number
    sources.py    - searches PubChem (chemical name) and supplier websites (SDS links)
    downloader.py - downloads, checks and saves "<CAS>.pdf" files and writes the log
    gui.py        - the program window
"""

__version__ = "1.0.0"
