import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from profiles import ProfileError, assign, parse_profiles, retention_languages


def names(profiles):
    return [p.name for p in profiles]


def test_empty_setting_is_one_implicit_all_profile_with_the_plain_subfolders():
    for text in ("", None, "  \n# just a comment\n"):
        (p,) = parse_profiles(text, "films", "shows")
        assert p.name == "principal" and p.languages is None
        assert (p.movies_dir, p.series_dir) == ("films", "shows")
        assert not p.exclusive


def test_parses_languages_folders_and_exclusive_flag():
    arabe, principal = parse_profiles(
        "arabe : ara, TUR : movies-ar : series-ar : exclusif\n"
        "# comment\n"
        "principal : all : movies : series\n"
    )
    assert arabe.languages == frozenset({"ara", "tur"})
    assert (arabe.movies_dir, arabe.series_dir, arabe.exclusive) == ("movies-ar", "series-ar", True)
    assert principal.languages is None and not principal.exclusive


def test_star_is_an_alias_for_all():
    (p,) = parse_profiles("p : * : m : s")
    assert p.languages is None


@pytest.mark.parametrize("text, fragment", [
    ("a : ara : m", "line 1"),
    ("a : ara : m : s : maybe", "exclusif"),
    (": ara : m : s", "name is empty"),
    ("a : : m : s", "no language"),
    ("a : arab : m : s", "3-letter"),
    ("a : fr : m : s", "3-letter"),
    ("a : all : m : s : exclusif", "cannot be"),
    ("a : ara : : s", "movies folder name is empty"),
    ("a : ara : m : ../s", "plain folder name"),
    ("a : ara : m/x : s", "plain folder name"),
    ("a : ara : m : s\nb : eng : m : t", "used by both"),
    ("a : ara : m : s\nb : eng : s : t", "used by both"),
    ("a : ara : m : s\nA : eng : n : t", "used twice"),
    ("a : all : m : s\nb : all : n : t", "more than one 'all'"),
])
def test_malformed_lines_are_refused_with_a_reason(text, fragment):
    with pytest.raises(ProfileError) as exc:
        parse_profiles(text)
    assert fragment in str(exc.value)


def test_error_names_the_line_after_blank_and_comment_lines():
    with pytest.raises(ProfileError) as exc:
        parse_profiles("\n# c\nbad line")
    assert "line 3" in str(exc.value)


def test_non_exclusive_profiles_all_receive_a_matching_version():
    profiles = parse_profiles("arabe : ara : ma : sa\nprincipal : all : m : s")
    assert names(assign(profiles, ["eng", "ara"])) == ["arabe", "principal"]
    assert names(assign(profiles, ["eng", "fre"])) == ["principal"]
    assert names(assign(profiles, [])) == ["principal"]


def test_exclusive_profile_claims_the_version():
    profiles = parse_profiles("arabe : ara : ma : sa : exclusif\nprincipal : all : m : s")
    assert names(assign(profiles, ["fre", "eng", "ara", "chi"])) == ["arabe"]
    assert names(assign(profiles, ["eng", "fre"])) == ["principal"]


def test_first_matching_exclusive_wins_and_order_ignored_for_non_exclusive():
    profiles = parse_profiles(
        "principal : all : m : s\n"
        "turc : tur : mt : st : exclusif\n"
        "arabe : ara : ma : sa : exclusif\n"
    )
    assert names(assign(profiles, ["ara", "tur"])) == ["turc"]
    assert names(assign(profiles, ["ara"])) == ["arabe"]


def test_version_matching_nothing_goes_nowhere_without_an_all_profile():
    profiles = parse_profiles("arabe : ara : ma : sa")
    assert assign(profiles, ["eng"]) == []
    assert assign(profiles, []) == []


def test_retention_adds_profile_languages_after_the_targets():
    profiles = parse_profiles("arabe : ara,tur : ma : sa\nprincipal : all : m : s")
    assert retention_languages(["fre", "eng"], profiles) == ["fre", "eng", "ara", "tur"]
    assert retention_languages(["fre", "ara"], profiles) == ["fre", "ara", "tur"]


def test_retention_stays_empty_when_no_target_language_is_set():
    profiles = parse_profiles("arabe : ara : ma : sa")
    assert retention_languages([], profiles) == []
    assert retention_languages(None, profiles) == []
