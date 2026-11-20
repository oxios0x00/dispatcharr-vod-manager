"""Reading vod-probe's results from a relation's custom_properties.

vod-probe measures every VOD relation once and writes a `probe` block (and a
`quality`) into the relation; this plugin no longer runs ffprobe itself. Pure
Python, no Django, so it is unit-tested outside Dispatcharr."""

from datetime import datetime, timedelta, timezone

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

# vod-probe stops asking the provider again for a series version that still
# can't be sampled after this many attempts (MAX_EMPTY_RELOADS in its
# contract.py), but leaves its summary "pending". Nothing will ever change it,
# so waiting for a better answer would be forever. That covers a version the
# provider answers with no episode, and one whose episodes are listed but whose
# sample episode keeps failing (HTTP 400).
SERIES_GIVE_UP_ATTEMPTS = 3


def series_given_up(custom_properties):
    """Whether vod-probe has given up on a series version: still "pending"
    after at least SERIES_GIVE_UP_ATTEMPTS tries. There is nothing to choose
    between on such a version, and nothing left to wait for."""
    found = block(custom_properties)
    if not found or found.get("status") != "pending":
        return False
    try:
        return int(found.get("attempts") or 0) >= SERIES_GIVE_UP_ATTEMPTS
    except (TypeError, ValueError):
        return False


def series_ready(custom_properties):
    """Whether vod-probe is done deciding about this series version: "ok" (a
    real result), "error" (tried, every episode failed) and "partial" (some
    seasons answered, one or more are confirmed dead) all count as done;
    only "pending" (not looked at yet, or a season still being sampled)
    means keep waiting — except a "pending" series version vod-probe has given
    up on (see series_given_up). The per-episode selection copes with a mix of
    measured and failed episodes on its own once a series is ready: an
    "error" series ends up with zero usable episodes (surfaced as a
    title-level error), a "partial" one keeps whatever episodes did
    measure."""
    found = block(custom_properties)
    return (bool(found) and found.get("status") in _SERIES_DONE_STATUSES) or series_given_up(custom_properties)


# vod-probe takes a series reload for its own when last_episode_refresh is
# within this much of its summary's probed_at (RELOAD_TOLERANCE in its
# contract.py): the summary's time is truncated to the second.
_RELOAD_TOLERANCE = timedelta(seconds=2)


def _parse_iso(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def series_settled(custom_properties, last_episode_refresh=None):
    """Whether vod-probe will never come back to the episodes of this series
    version on its own: its summary is "error" or "partial" (every sample, or
    every sample of some seasons, failed), or it gave up on it (see
    series_given_up). vod-probe only samples a few episodes per series and
    copies a result to the others when one answers, so the episodes of a dead
    season stay without any result for good; waiting for them would never end.

    Not settled while vod-probe still has a reason to visit it again, the same
    ones its series_work() checks: Retry Errors or Reload Incomplete Series
    flagged it, the provider's last_modified moved, or Dispatcharr reloaded
    the episodes after the summary (which erases their results, so a missing
    result then means "not redone yet", not "never")."""
    found = block(custom_properties)
    if not found or found.get("retry") or found.get("reload"):
        return False
    if found.get("status") not in ("error", "partial") and not series_given_up(custom_properties):
        return False
    current = ((custom_properties or {}).get("basic_data") or {}).get("last_modified")
    if current is not None and str(current) != str(found.get("last_modified")):
        return False
    if last_episode_refresh is not None:
        summarised_at = _parse_iso(found.get("probed_at"))
        if summarised_at is None:
            return False
        if last_episode_refresh.tzinfo is None:
            last_episode_refresh = last_episode_refresh.replace(tzinfo=timezone.utc)
        if last_episode_refresh > summarised_at + _RELOAD_TOLERANCE:
            return False
    return True


def settled_state(custom_properties, version_settled):
    """state(), except that an episode still MISSING is FAILED when its series
    version is settled (see series_settled): nothing will ever measure it."""
    found = state(custom_properties)
    return FAILED if found == MISSING and version_settled else found
