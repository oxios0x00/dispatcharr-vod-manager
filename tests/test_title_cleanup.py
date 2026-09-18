import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from title_cleanup import parse_tag_list, strip_title_tags

TAGS = parse_tag_list(
    """
NF -
TOP -
4K-FR -
AMZ -
FR -
FR .
D+ -
UNV -
4K-NF -
PRMT -
4K-AMZ -
4K-D+ -
4K-FR-HDR -
4K-FR-
A+ -
A+
4K-A+ -
4K-MRVL -
DWA -
007 -
K-FR -
AR-SUBS -
4M-AMZ -
4K-
"""
)


def test_simple_prefix():
    assert strip_title_tags("NF - The Matrix", TAGS) == "The Matrix"


def test_longest_match_wins_over_generic_prefix():
    # "4K-FR-HDR -" must win over the shorter "4K-" and "4K-FR-"
    assert strip_title_tags("4K-FR-HDR - Dune", TAGS) == "Dune"


def test_generic_4k_dash_prefix():
    assert strip_title_tags("4K- Oppenheimer", TAGS) == "Oppenheimer"


def test_tag_without_trailing_separator():
    assert strip_title_tags("A+ Avengers", TAGS) == "Avengers"


def test_tag_with_trailing_separator_preferred_when_present():
    assert strip_title_tags("A+ - Avengers", TAGS) == "Avengers"


def test_stacked_tags_are_fully_removed():
    assert strip_title_tags("NF - 4K-FR - Inception", TAGS) == "Inception"


def test_case_insensitive_matching():
    assert strip_title_tags("nf - The Matrix", TAGS) == "The Matrix"


def test_no_matching_tag_leaves_title_untouched():
    assert strip_title_tags("The Matrix", TAGS) == "The Matrix"


def test_never_returns_empty_title():
    # Pathological: title is *only* a tag with nothing after it.
    assert strip_title_tags("NF -", TAGS) == "NF -"


def test_empty_tag_list_is_noop():
    assert strip_title_tags("NF - The Matrix", []) == "NF - The Matrix"


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
