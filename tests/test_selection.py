import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from selection import Candidate, select_winners

TARGET_LANGUAGES = ["fr", "en"]


def ids(winners):
    return sorted(c.relation_id for c in winners)


def test_keeps_one_per_requested_tier_when_both_available():
    candidates = [
        Candidate(1, ["fr", "en"], "2160p", bitrate=20_000_000),
        Candidate(2, ["fr", "en"], "1080p", bitrate=8_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p", "1080p"])
    assert ids(winners) == [1, 2], winners


def test_missing_requested_tier_falls_back_instead_of_eliminating():
    # target_qualities = [2160p] only, but title has just a 1080p version.
    candidates = [Candidate(1, ["fr", "en"], "1080p", bitrate=8_000_000)]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"])
    assert ids(winners) == [1], winners


def test_fallback_picks_best_available_tier_on_ladder():
    # Neither requested tier (2160p/1080p) exists; both 720p and 480p do.
    # Fallback should prefer 720p (higher on the ladder), not just "first".
    candidates = [
        Candidate(1, ["fr", "en"], "480p", bitrate=50_000_000),  # deliberately high bitrate
        Candidate(2, ["fr", "en"], "720p", bitrate=1_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p", "1080p"])
    assert ids(winners) == [2], winners


def test_bitrate_breaks_tie_within_same_tier_when_language_coverage_equal():
    candidates = [
        Candidate(1, ["fr", "en"], "2160p", bitrate=15_000_000),
        Candidate(2, ["fr", "en"], "2160p", bitrate=35_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"])
    assert ids(winners) == [2], winners


def test_language_coverage_beats_bitrate_when_not_tied():
    # relation 2 has higher bitrate but doesn't cover French — coverage
    # must win, bitrate only breaks *equal*-coverage ties.
    candidates = [
        Candidate(1, ["fr", "en"], "2160p", bitrate=10_000_000),
        Candidate(2, ["en"], "2160p", bitrate=40_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"])
    assert ids(winners) == [1], winners


def test_minimal_combination_still_applies_within_a_tier():
    candidates = [
        Candidate(1, ["en"], "2160p", bitrate=10_000_000),
        Candidate(2, ["fr"], "2160p", bitrate=10_000_000),
        Candidate(3, ["de"], "2160p", bitrate=10_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"])
    assert ids(winners) == [1, 2], winners


def test_three_requested_tiers_each_contribute_independently():
    candidates = [
        Candidate(1, ["en"], "2160p", bitrate=1),
        Candidate(2, ["fr"], "1080p", bitrate=1),
        Candidate(3, ["fr", "en"], "720p", bitrate=1),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p", "1080p", "720p"])
    assert ids(winners) == [1, 2, 3], winners


def test_no_target_languages_falls_back_to_bitrate_within_tier():
    candidates = [
        Candidate(1, ["ja"], "1080p", bitrate=5_000_000),
        Candidate(2, ["ja"], "1080p", bitrate=9_000_000),
    ]
    winners = select_winners(candidates, [], ["1080p"])
    assert ids(winners) == [2], winners


def test_no_target_qualities_configured_falls_back_to_best_available_tier():
    candidates = [
        Candidate(1, ["fr", "en"], "720p", bitrate=1),
        Candidate(2, ["fr", "en"], "1080p", bitrate=1),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, [])
    assert ids(winners) == [2], winners


def test_empty_candidates_returns_empty():
    assert select_winners([], TARGET_LANGUAGES, ["2160p"]) == []


def test_zero_language_overlap_falls_back_to_bitrate_by_default():
    # No candidate has fr or en at all — default behaviour (flag off)
    # keeps the title anyway, ignoring the language filter for this
    # tier, same as if no target languages were configured.
    candidates = [
        Candidate(1, ["de"], "2160p", bitrate=5_000_000),
        Candidate(2, ["tr"], "2160p", bitrate=9_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"])
    assert ids(winners) == [2], winners


def test_exclude_unmatched_language_drops_a_tier_with_zero_overlap():
    candidates = [
        Candidate(1, ["de"], "2160p", bitrate=5_000_000),
        Candidate(2, ["tr"], "2160p", bitrate=9_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"], exclude_unmatched_language=True)
    assert winners == []


def test_exclude_unmatched_language_only_affects_the_tier_with_no_match():
    # 2160p has zero fr/en coverage (excluded), 1080p has a real match
    # (kept) — exclusion is per-tier, not all-or-nothing for the title.
    candidates = [
        Candidate(1, ["de"], "2160p", bitrate=9_000_000),
        Candidate(2, ["fr", "en"], "1080p", bitrate=5_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p", "1080p"], exclude_unmatched_language=True)
    assert ids(winners) == [2], winners


def test_exclude_unmatched_language_can_empty_out_a_whole_title():
    # Every requested tier has zero language match — nothing survives.
    candidates = [
        Candidate(1, ["de"], "2160p", bitrate=9_000_000),
        Candidate(2, ["tr"], "1080p", bitrate=5_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p", "1080p"], exclude_unmatched_language=True)
    assert winners == []


def test_exclude_unmatched_language_does_not_affect_partial_coverage():
    # At least one target language is covered (by the combination) — this
    # is not the "zero match" case, so exclusion must not kick in even
    # with the flag on.
    candidates = [
        Candidate(1, ["fr"], "2160p", bitrate=5_000_000),
        Candidate(2, ["de"], "2160p", bitrate=9_000_000),
    ]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"], exclude_unmatched_language=True)
    assert ids(winners) == [1], winners


def test_exclude_unmatched_language_does_not_affect_fully_covering_candidate():
    candidates = [Candidate(1, ["fr", "en"], "2160p", bitrate=5_000_000)]
    winners = select_winners(candidates, TARGET_LANGUAGES, ["2160p"], exclude_unmatched_language=True)
    assert ids(winners) == [1], winners


def test_exclude_unmatched_language_is_a_noop_with_no_target_languages():
    # No target languages configured at all: the "no target languages"
    # branch returns early and is unaffected by this flag either way.
    candidates = [Candidate(1, ["ja"], "1080p", bitrate=5_000_000)]
    winners = select_winners(candidates, [], ["1080p"], exclude_unmatched_language=True)
    assert ids(winners) == [1], winners


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
