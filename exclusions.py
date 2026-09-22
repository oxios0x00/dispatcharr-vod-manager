"""Parsing for the tmdbid exclusion settings fields (movies and series)."""


def parse_excluded_ids(text):
    """Return the set of TMDB id strings found in `text`, one entry per
    non-blank line. A line's first whitespace-separated token is the id;
    anything after it is a free comment for the user's own reference (e.g.
    '1396 wrong match for X') and is ignored. A line whose first token isn't
    all digits is skipped rather than risk matching the wrong thing."""
    ids = set()
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        token = line.split(None, 1)[0]
        if token.isdigit():
            ids.add(token)
    return ids
