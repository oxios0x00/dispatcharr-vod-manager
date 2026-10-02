"""Reading vod-probe's results from a relation's custom_properties.

vod-probe measures every VOD relation once and writes a `probe` block (and a
`quality`) into the relation; this plugin no longer runs ffprobe itself. Pure
Python, no Django, so it is unit-tested outside Dispatcharr."""

MEASURED = "measured"
FAILED = "failed"
MISSING = "missing"

_USABLE_STATUSES = ("ok", "inferred")
_FAILED_STATUSES = ("error", "unreachable")


def block(custom_properties):
    found = (custom_properties or {}).get("probe")
    return found if isinstance(found, dict) else None


def state(custom_properties):
    """MEASURED when the relation has a usable result (a real one or one copied
    from a sibling episode), FAILED when vod-probe tried and could not measure
    it, MISSING when vod-probe has not looked at it yet."""
    found = block(custom_properties)
    if found is None:
        return MISSING
    if found.get("status") in _USABLE_STATUSES and found.get("tier"):
        return MEASURED
    if found.get("status") in _FAILED_STATUSES:
        return FAILED
    return MISSING


def tier(custom_properties):
    found = block(custom_properties)
    return found.get("tier") if found and state(custom_properties) == MEASURED else None


def usable_tier(status, tier):
    """The tier of a block given only its status and tier, for callers that
    read those two keys straight from the database."""
    return tier if status in _USABLE_STATUSES and tier else None


def languages(custom_properties):
    """Audio languages a title can be matched on. A track that is an audio
    description is not counted, as the version it belongs to would otherwise
    look like it carries that language for real."""
    found = block(custom_properties) or {}
    tracks = found.get("audio")
    if isinstance(tracks, list):
        return [t.get("language", "und") for t in tracks if not t.get("audio_description")]
    return list(found.get("audio_languages") or [])


def bitrate(custom_properties):
    """The overall bitrate of the file when known: the video stream often
    carries none of its own, and one measure has to be used for every version
    so that the tie-break compares like with like."""
    found = block(custom_properties) or {}
    return found.get("bit_rate") or (found.get("video") or {}).get("bit_rate")


_SERIES_DONE_STATUSES = ("ok", "error", "partial")

# vod-probe stops asking the provider again for a series version that still has
# no episode after this many attempts (MAX_EMPTY_RELOADS in its contract.py),
# but leaves its summary "pending". Nothing will ever change it, so waiting for
# a better answer would be forever.
EMPTY_SERIES_GIVE_UP_ATTEMPTS = 3


def series_given_up_empty(custom_properties):
    """Whether vod-probe has given up on a series version the provider keeps
    answering with no episode: still "pending", no episode seen, and at least
    EMPTY_SERIES_GIVE_UP_ATTEMPTS tries. There is nothing to choose between
    on such a version, and nothing left to wait for."""
    found = block(custom_properties)
    if not found or found.get("status") != "pending":
        return False
    try:
        return (found.get("episodes") or 0) == 0 and int(found.get("attempts") or 0) >= EMPTY_SERIES_GIVE_UP_ATTEMPTS
    except (TypeError, ValueError):
        return False


def series_ready(custom_properties):
    """Whether vod-probe is done deciding about this series version: "ok" (a
    real result), "error" (tried, every episode failed) and "partial" (some
    seasons answered, one or more are confirmed dead) all count as done;
    only "pending" (not looked at yet, or a season still being sampled)
    means keep waiting — except a "pending" series version vod-probe has given
    up on for lack of any episode (see series_given_up_empty). The per-episode selection copes with a mix of
    measured and failed episodes on its own once a series is ready: an
    "error" series ends up with zero usable episodes (surfaced as a
    title-level error), a "partial" one keeps whatever episodes did
    measure."""
    found = block(custom_properties)
    return (bool(found) and found.get("status") in _SERIES_DONE_STATUSES) or series_given_up_empty(custom_properties)
