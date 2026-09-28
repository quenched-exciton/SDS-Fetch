"""Tests for reading and checking CAS numbers (no internet needed)."""

import pytest

from sds_downloader.cas import (
    check_cas,
    extract_cas_numbers,
    has_valid_check_digit,
    parse_csv_file,
    parse_text_list,
)


@pytest.mark.parametrize("cas", [
    "7732-18-5",    # water
    "64-17-5",      # ethanol
    "7758-99-8",    # copper(II) sulfate pentahydrate
    "7664-93-9",    # sulfuric acid
    "7647-01-0",    # hydrochloric acid
    "25322-68-3",   # polyethylene glycol
    "27206-35-5",   # SPS, bis(3-sulfopropyl) disulfide disodium salt
    "2869-83-2",    # Janus Green B
    "1333-74-0",    # hydrogen
])
def test_real_cas_numbers_pass_the_check_digit(cas):
    assert has_valid_check_digit(cas)


@pytest.mark.parametrize("cas", ["7732-18-4", "64-17-6", "7758-99-9"])
def test_typos_fail_the_check_digit(cas):
    assert not has_valid_check_digit(cas)


def test_check_cas_accepts_and_cleans_valid_numbers():
    assert check_cas(" 64-17-5 ") == ("64-17-5", "")
    assert check_cas("64–17–5") == ("64-17-5", "")   # en dashes from Word
    assert check_cas("064-17-5") == ("64-17-5", "")            # leading zero removed


@pytest.mark.parametrize("text", ["64175", "64-17", "abc", "64-17-55", "12345678-12-3"])
def test_check_cas_rejects_bad_formats(text):
    cas, reason = check_cas(text)
    assert cas is None and reason


def test_extract_ignores_names_on_the_same_line():
    valid, invalid = extract_cas_numbers("Ethanol, absolute   64-17-5")
    assert valid == ["64-17-5"] and invalid == []


def test_extract_reports_line_without_cas():
    valid, invalid = extract_cas_numbers("ethanol")
    assert valid == []
    assert invalid[0].text == "ethanol"


def test_parse_text_list_handles_separators_duplicates_and_errors():
    text = "64-17-5, 7732-18-5\n\n7758-99-8; 64-17-5\n7732-18-4\nnot a number\n"
    parsed = parse_text_list(text)
    assert parsed.valid_cas == ["64-17-5", "7732-18-5", "7758-99-8"]
    assert parsed.duplicates_ignored == 1
    assert [entry.text for entry in parsed.invalid] == ["7732-18-4", "not a number"]
    assert parsed.entries_read == 4   # blank line not counted


def test_csv_uses_column_with_cas_header(tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("Chemical,CAS Number,Amount\nEthanol,64-17-5,1 L\nWater,7732-18-5,5 L\n",
                        encoding="utf-8")
    assert parse_csv_file(csv_file).valid_cas == ["64-17-5", "7732-18-5"]


def test_csv_without_header_uses_first_column(tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("64-17-5,Ethanol\n7732-18-5,Water\n", encoding="utf-8")
    parsed = parse_csv_file(csv_file)
    assert parsed.valid_cas == ["64-17-5", "7732-18-5"]
    assert parsed.invalid == []


def test_csv_skips_first_row_without_digits(tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("Registry number\n64-17-5\n", encoding="utf-8")
    parsed = parse_csv_file(csv_file)
    assert parsed.valid_cas == ["64-17-5"] and parsed.invalid == []


def test_csv_semicolon_separated_with_bom_and_invalid_entry(tmp_path):
    # European Excel: semicolons, UTF-8 "BOM" marker at the start of the file.
    csv_file = tmp_path / "list.csv"
    csv_file.write_text("Name;CAS\nEthanol;64-17-5\nTypo;64-17-6\nEmpty;\n", encoding="utf-8-sig")
    parsed = parse_csv_file(csv_file)
    assert parsed.valid_cas == ["64-17-5"]
    assert [entry.text for entry in parsed.invalid] == ["64-17-6"]


def test_csv_in_windows_1252_encoding(tmp_path):
    csv_file = tmp_path / "list.csv"
    csv_file.write_bytes("CAS;Name\n64-17-5;Ethanol \xb5L\n".encode("cp1252"))
    assert parse_csv_file(csv_file).valid_cas == ["64-17-5"]
