"""Tests for recognising manufacturer names and their brands (no internet needed)."""

from sds_fetch.manufacturers import display_name, mentions, names_for, preference_rank


def test_brand_names_belong_to_their_company():
    assert display_name("merck") == "Sigma-Aldrich"
    assert display_name("Fluka") == "Sigma-Aldrich"
    assert display_name("Alfa Aesar") == "Thermo Fisher"
    assert display_name("j.t. baker") == "Avantor / VWR"
    # A known name inside a longer one; the longest known name decides.
    assert display_name("Sigma-Aldrich Inc.") == "Sigma-Aldrich"
    assert display_name("Thermo Fisher Scientific") == "Thermo Fisher"


def test_unknown_names_are_matched_as_typed():
    assert names_for("Acme Plating") == ("Acme Plating", ["acme plating"])
    assert mentions("Acme Plating", "ACME PLATING, INC.")
    assert not mentions("Acme Plating", "Acme Chemicals")


def test_mentions_matches_whole_words_and_other_brand_names():
    assert mentions("Sigma-Aldrich", "MilliporeSigma")
    assert mentions("sigma", "Product of Merck KGaA, Darmstadt")
    assert mentions("TCI", "TCI America")
    assert not mentions("TCI", "Botcing Chemicals")   # "tci" inside another word
    assert not mentions("Gelest", "")


def test_preference_rank():
    preferences = ["Gelest", "Sigma-Aldrich"]
    assert preference_rank(preferences, "Gelest, Inc.") == 0
    assert preference_rank(preferences, "Aldrich") == 1
    assert preference_rank(preferences, "TCI") is None
