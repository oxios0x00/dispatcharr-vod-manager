"""Strip provider junk tags from the start of movie titles.

Purely cosmetic — operates on the title string only, never on the
quality/language selection logic (see NOTES.md: the spec explicitly
forbids guessing quality/language from names; this is a separate,
additive concern about display names).
"""

MAX_PASSES = 5


def parse_tag_list(text):
    """One literal prefix per line, as the user typed it (including any
    trailing separator like ' -' or ' .') — blank lines ignored."""
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def strip_title_tags(title, tags):
    """Repeatedly strip the longest matching configured prefix from the
    start of `title` (case-insensitive), up to MAX_PASSES times so
    stacked tags ("NF - 4K-FR - Title") get fully cleaned. Returns the
    original title unchanged if no tag matches."""
    if not title or not tags:
        return title

    ordered = sorted(tags, key=len, reverse=True)
    working = title
    changed = False

    for _ in range(MAX_PASSES):
        candidate = working.lstrip()
        matched_len = 0
        for tag in ordered:
            if candidate[: len(tag)].casefold() == tag.casefold():
                matched_len = len(tag)
                break
        if not matched_len:
            break
        working = candidate[matched_len:]
        changed = True

    if not changed:
        return title

    cleaned = working.lstrip()
    return cleaned if cleaned else title  # never return an empty title
