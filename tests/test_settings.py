"""Tests for remembering the window's settings."""

from sds_fetch.settings import DEFAULTS, load_settings, save_settings


def test_settings_round_trip(tmp_path):
    path = tmp_path / "settings.json"
    save_settings({"download_folder": "C:/SDS", "strict": True, "chemical_list": "64-17-5 @ TCI"}, path)
    settings = load_settings(path)
    assert settings["download_folder"] == "C:/SDS"
    assert settings["strict"] is True
    assert settings["chemical_list"] == "64-17-5 @ TCI"
    assert settings["preferred_manufacturers"] == ""   # not saved -> default


def test_missing_or_damaged_settings_give_defaults(tmp_path):
    assert load_settings(tmp_path / "missing.json") == DEFAULTS
    damaged = tmp_path / "damaged.json"
    damaged.write_text("{not json", encoding="utf-8")
    assert load_settings(damaged) == DEFAULTS
    wrong_types = tmp_path / "wrong.json"
    wrong_types.write_text('{"strict": "yes", "download_folder": 5}', encoding="utf-8")
    assert load_settings(wrong_types) == DEFAULTS
