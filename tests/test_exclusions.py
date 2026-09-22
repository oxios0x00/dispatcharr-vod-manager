import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from exclusions import parse_excluded_ids


def test_empty_and_none_return_empty_set():
    assert parse_excluded_ids("") == set()
    assert parse_excluded_ids(None) == set()


def test_one_id_per_line():
    assert parse_excluded_ids("1396\n603") == {"1396", "603"}


def test_free_comment_after_the_id_is_ignored():
    assert parse_excluded_ids("1396 Breaking Bad (wrong match)") == {"1396"}


def test_blank_lines_are_skipped():
    assert parse_excluded_ids("1396\n\n\n603\n") == {"1396", "603"}


def test_surrounding_whitespace_is_stripped():
    assert parse_excluded_ids("  1396  \n\t603\t") == {"1396", "603"}


def test_line_not_starting_with_digits_is_skipped():
    assert parse_excluded_ids("not-an-id some title\n1396") == {"1396"}


def test_duplicate_ids_collapse():
    assert parse_excluded_ids("1396 first note\n1396 second note") == {"1396"}


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
