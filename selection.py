"""Automatic quality + language selection.

Semantics (revised after real-catalogue testing — see NOTES.md point 12):
  - target_qualities is a list of quality TIERS to each keep a winner
    from, in priority order — not a single-winner tie-break. Setting
    "2160p,1080p" means "keep one 2160p version AND one 1080p version",
    each independently language-selected, if that title actually has
    candidates in both tiers.
  - A title is never eliminated for lacking a requested tier: if none of
    the requested tiers have any candidate, fall back to the best
    available tier for that title instead of returning nothing.
  - Within a single tier, ties are broken by real probed video bitrate
    (higher wins) rather than by a quality label — quality no longer
    varies inside one tier by definition.
  - Language coverage still uses the "minimal combination of versions
    covering target_languages" rule from the original spec, now applied
    independently per tier rather than globally.
"""
from itertools import combinations

# Only used to pick a fallback tier when none of the user's requested
# qualities have any candidate for a title — never used to filter/exclude.
_QUALITY_LADDER = ["2160p", "1080p", "720p", "480p", "sd", "unknown"]


class Candidate:
    """A single M3UMovieRelation (or episode relation) plus its probe
    result, in the shape selection() needs. Callers build these from
    Store.get_probe() rows."""

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
    single pool of same-tier candidates, tie-broken by bitrate. This is
    the original spec algorithm minus the quality tie-break, which is now
    handled by partitioning into tiers before calling this.

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


def _best_available_tier(by_tier):
    for tier in _QUALITY_LADDER:
        if tier in by_tier:
            return tier
    return next(iter(by_tier))


def select_winners(
    candidates, target_languages, target_qualities, exclude_unmatched_language=False,
    exclude_unmatched_quality=False,
):
    """Return the list of winning Candidates for one title.

    candidates: list[Candidate], must be non-empty.
    target_languages: list[str] — only the *set* matters, order doesn't.
    target_qualities: list[str] — tiers to each keep a winner from, most
        preferred first. A tier absent from this title's candidates is
        simply skipped; if *none* of them are present, the title falls
        back to its single best available tier rather than being dropped.
    exclude_unmatched_quality: when none of target_qualities is present, the
        title yields no winner instead of falling back to its best tier — an
        explicit opt-in to dropping titles with nothing in the wanted tiers.
    exclude_unmatched_language: see _select_within_pool. Applied
        independently per tier (and to the fallback-tier pick when no
        requested tier is present) — a title can end up with an empty
        result if every tier it has hits this, which is the point: an
        explicit opt-in to excluding language-mismatched content instead
        of always keeping something regardless of language.
    """
    if not candidates:
        return []

    by_tier = {}
    for c in candidates:
        by_tier.setdefault(c.quality_label, []).append(c)

    requested_tiers = [q for q in (target_qualities or []) if q in by_tier]

    if not requested_tiers:
        if exclude_unmatched_quality and target_qualities:
            return []
        fallback_tier = _best_available_tier(by_tier)
        return _select_within_pool(by_tier[fallback_tier], target_languages, exclude_unmatched_language)

    winners = []
    for tier in requested_tiers:
        winners.extend(_select_within_pool(by_tier[tier], target_languages, exclude_unmatched_language))
    return winners
