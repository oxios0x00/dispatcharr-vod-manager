import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from episodes import count_provider_episodes, is_incomplete, needs_provider_check


def test_count_provider_episodes_dict_keyed_by_season():
    info = {"episodes": {"1": [{}, {}, {}], "2": [{}, {}]}}
    assert count_provider_episodes(info) == 5


def test_count_provider_episodes_list_of_seasons():
    info = {"episodes": [[{}, {}], [{}]]}
    assert count_provider_episodes(info) == 3


def test_count_provider_episodes_ignores_non_list_seasons():
    info = {"episodes": {"1": [{}, {}], "2": "oops", "3": None}}
    assert count_provider_episodes(info) == 2


def test_count_provider_episodes_empty_or_missing():
    assert count_provider_episodes(None) == 0
    assert count_provider_episodes({}) == 0
    assert count_provider_episodes({"episodes": []}) == 0
    assert count_provider_episodes({"episodes": {}}) == 0


def test_needs_provider_check_only_for_partial_against_a_richer_sibling():
    # The Severance 4K case: 1 episode held, its sibling source holds 18.
    assert needs_provider_check(1, 18) is True
    # Zero is the caller's separate, no-confirmation case.
    assert needs_provider_check(0, 18) is False
    # Equal or larger than every sibling: nothing suspicious.
    assert needs_provider_check(18, 18) is False
    assert needs_provider_check(18, 1) is False
    # A single-source series has no sibling to compare against.
    assert needs_provider_check(5, 0) is False


def test_is_incomplete():
    assert is_incomplete(1, 19) is True
    assert is_incomplete(19, 19) is False
    assert is_incomplete(20, 19) is False
