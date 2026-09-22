"""Automatic quality + language selection.

Two modes, chosen per call by `keep_one_per_tier`:
  - True (cleanup): one winner per quality tier. Within a tier, the
    minimal combination of versions covering target_languages is kept
    (ties broken by real bitrate) — the original "pick the fewest files
    that give you every target language" rule.
  - False (default): every version in scope is kept. Within a tier,
    every candidate whose audio includes at least one target language is
    kept (all of them, not a minimal covering set); with no target
    languages, every candidate in the tier is kept.

target_qualities is the list of tiers in scope, most preferred first —
not a single-winner tie-break: "2160p,1080p" means both tiers are
handled, independently, if the title has candidates in either. An EMPTY
target_qualities means every tier the title has is in scope (no quality
filter at all) — this applies in both modes; leaving both
target_qualities and target_languages empty keeps literally everything
vod-probe measured.

A title is never eliminated for lacking a *requested* (non-empty)
target_qualities: if none of the requested tiers have any candidate, it
falls back to the title's single best available tier, unless
exclude_unmatched_quality asks to drop it instead. Likewise a tier with
no language match falls back to keeping everything in it, unless
exclude_unmatched_language asks to drop that tier instead.
"""
from itertools import combinations

# Only used to pick a fallback tier when none of the user's requested
# qualities have any candidate for a title — never used to filter/exclude.
_QUALITY_LADDER = ["2160p", "1080p", "720p", "480p", "sd", "unknown"]


class Candidate:
    """A single M3UMovieRelation (or episode relation) plus its measured
    quality, in the shape selection() needs. Callers build these from
    measurements.py."""

    __slots__ = ("relation_id", "languages", "quality_label", "bitrate")

    def __init__(self, relation_id, languages, quality_label, bitrate=None):
        self.relation_id = relation_id
        self.languages = frozenset(languages or [])
        self.quality_label = quality_label
        self.bitrate = bitrate or 0

    def __repr__(self):
        return (
            f"Candidate({self.relation_id}, {sorted(self.languages)}, "
            f"{self.quality_label}, {self.bitrate}bps)"
        )


def _combo_bitrate_key(combo):
    """Sort key for min(): higher total bitrate wins, so negate."""
    return -sum(c.bitrate for c in combo)


def _select_within_pool(candidates, target_languages, exclude_unmatched_language=False):
    """Minimal-combination-covering-target-languages search within a
    single pool of same-tier candidates, tie-broken by bitrate. Used in
    the "one winner per tier" mode; see select_all_in_pool for the
    "keep every matching version" mode.

    exclude_unmatched_language: when a pool has zero coverage of any
    target language at all (every candidate is in a language nobody
    asked for), the default (False) keeps the best-bitrate candidate
    anyway rather than dropping the tier — same fallback as having no
    target languages configured. Setting this True instead excludes the
    whole pool (returns []) in that specific case, so a tier — or, if
    every requested tier hits this, the whole title — can end up with
    nothing kept. Never affects a pool that has at least partial
    coverage; only the true zero-match case."""
    if not candidates:
        return []

    target_set = frozenset(target_languages or [])

    if not target_set:
        best = max(candidates, key=lambda c: c.bitrate)
        return [best]

    fully_covering = [c for c in candidates if target_set <= c.languages]
    if fully_covering:
        best = max(fully_covering, key=lambda c: c.bitrate)
        return [best]

    n = len(candidates)
    max_k = min(n, len(target_set))
    best_combo = None
    for k in range(1, max_k + 1):
        combos_at_k = []
        for combo in combinations(candidates, k):
            covered = frozenset().union(*(c.languages for c in combo))
            coverage = len(covered & target_set)
            if coverage:
                combos_at_k.append((coverage, combo))
        if not combos_at_k:
            continue
        best_coverage = max(cov for cov, _ in combos_at_k)
        best_at_k = [combo for cov, combo in combos_at_k if cov == best_coverage]
        best_combo_at_k = min(best_at_k, key=_combo_bitrate_key)
        if best_combo is None:
            best_combo = (best_coverage, best_combo_at_k)
        if best_coverage == len(target_set):
            best_combo = (best_coverage, best_combo_at_k)
            break

    if best_combo is None:
        if exclude_unmatched_language:
            return []
        best = max(candidates, key=lambda c: c.bitrate)
        return [best]

    return list(best_combo[1])


def select_all_in_pool(candidates, target_languages, exclude_unmatched_language=False):
    """Every candidate worth keeping within a single pool of same-tier
    candidates, for the "keep every matching version" mode: no minimal
    covering search, since there is no single-winner constraint to
    minimize against.

    With no target_languages, every candidate is kept. Otherwise, a
    candidate is kept when its audio includes at least one target
    language. If none does, the pool falls back to keeping everyone
    (same "never drop silently" default as the one-winner mode) unless
    exclude_unmatched_language asks to drop the pool instead."""
    if not candidates:
        return []

    target_set = frozenset(target_languages or [])
    if not target_set:
        return list(candidates)

    matched = [c for c in candidates if c.languages & target_set]
    if matched:
        return matched
    return [] if exclude_unmatched_language else list(candidates)


def _best_available_tier(by_tier):
    for tier in _QUALITY_LADDER:
        if tier in by_tier:
            return tier
    return next(iter(by_tier))


def select_winners(
    candidates, target_languages, target_qualities, exclude_unmatched_language=False,
    exclude_unmatched_quality=False, keep_one_per_tier=True,
):
    """Return the Candidates to keep for one title (or one episode).

    candidates: list[Candidate], must be non-empty.
    target_languages: list[str] — only the *set* matters, order doesn't.
    target_qualities: list[str] — tiers in scope, most preferred first.
        Empty means every tier the title has. A tier in this list absent
        from the title's candidates is simply skipped; if the list is
        non-empty and *none* of its tiers are present, the title falls
        back to its single best available tier rather than being dropped.
    exclude_unmatched_quality: when target_qualities is non-empty and none
        of it is present, drop the title instead of falling back to its
        best tier.
    exclude_unmatched_language: see _select_within_pool / select_all_in_pool.
        Applied independently per tier — a title can end up with nothing
        kept if every tier it has hits this.
    keep_one_per_tier: True picks one winner per tier (the minimal
        combination covering target_languages); False keeps every
        candidate per tier that matches target_languages (or everyone,
        with no target_languages).
    """
    if not candidates:
        return []

    by_tier = {}
    for c in candidates:
        by_tier.setdefault(c.quality_label, []).append(c)

    pick = _select_within_pool if keep_one_per_tier else select_all_in_pool

    if target_qualities:
        tiers = [q for q in target_qualities if q in by_tier]
        if not tiers:
            if exclude_unmatched_quality:
                return []
            tiers = [_best_available_tier(by_tier)]
    else:
        tiers = list(by_tier.keys())

    winners = []
    for tier in tiers:
        winners.extend(pick(by_tier[tier], target_languages, exclude_unmatched_language))
    return winners
