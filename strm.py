"""Generate .strm files pointing directly at a specific Dispatcharr VOD
relation, bypassing the native Xtream API entirely.

Why this module exists: Dispatcharr's own Xtream endpoints
(`xc_get_vod_streams`/`get_series`/`stream_xc_movie`/`stream_xc_episode`)
always collapse a title with several kept relations (one per quality
tier, by design of this plugin) down to a single one — whichever M3U
account has the highest priority — regardless of which category a
client browsed through to get there (verified with real ffprobe calls
against a live Dispatcharr instance). A pure Xtream client (TiviMate,
etc.) can never be steered around this; only a dedicated Dispatcharr
core change could (tracked upstream as GitHub issue #1443, declined PR
#1500, ongoing #1610).

Dispatcharr does expose a second, generic endpoint that *is*
relation-aware: `/proxy/vod/<movie|series|episode>/<uuid>?stream_id=<id>`
(`apps/proxy/vod_proxy/views.py`, `_parse_preferred_vod_params` +
`stream_xc_*`'s `preferred_stream_id` matching against
`relation.stream_id`).

`stream_id` here is the relation's own `stream_id` *field* (the
provider's raw id), not the Django row's primary key — confirmed against
Dispatcharr's own matching logic (`str(r.stream_id) ==
str(preferred_stream_id)`).
"""
import os
import re

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Best to worst, matching probe.py's classify_quality() tiers exactly.
_QUALITY_ORDER = ["2160p", "1080p", "720p", "480p", "sd", "unknown"]


def sanitize_filename(name):
    """Strip characters that are invalid in a filename on common
    filesystems (Windows-compatible, since library shares often end up
    served over SMB). Collapses the result's whitespace and never
    returns an empty string."""
    cleaned = _INVALID_CHARS.sub("", name or "").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Unknown"


def id_tag(tmdb_id, imdb_id):
    """The Jellyfin/Emby external-id filename tag for a title, e.g.
    ' [tmdbid-12345]' or ' [imdbid-tt1234567]' (leading space, ready to
    append to a title). Prefers tmdb_id; falls back to imdb_id; returns
    '' when neither is known, leaving a media server only the title text
    to identify the file by. Plex uses a different tag ({tmdb-12345}, no
    'id' suffix, curly braces) — this plugin targets Emby/Jellyfin only,
    matching the rest of the .strm generation feature."""
    if tmdb_id:
        return f" [tmdbid-{tmdb_id}]"
    if imdb_id:
        return f" [imdbid-{imdb_id}]"
    return ""


def build_proxy_url(base_url, content_type, uuid, stream_id):
    """`{base_url}/proxy/vod/<content_type>/<uuid>?stream_id=<stream_id>`
    — pinned to one specific relation, ignoring account priority."""
    base = f"{base_url.rstrip('/')}/proxy/vod/{content_type}/{uuid}"
    if not stream_id:
        return base
    return f"{base}?stream_id={stream_id}"


def plan_suffixes(quality_labels):
    """Given the probed quality label (or None) for each of a title's
    kept relations, in a stable order, decide the filename suffix for
    each: the real quality label when known (or 'unprobed' if this
    relation was never successfully probed), always shown — even for a
    lone relation, so the quality is visible at a glance without having
    to open the file. With two or more relations, the label is also
    prefixed with a rank number so alphabetical sort matches quality
    order instead of string order — helpful in any file listing, though
    Emby does not necessarily use it to pick its default version (see
    best_quality_first) — '1080p' sorts before '2160p' as plain text, which is
    backwards. The fallback for an unprobed relation among several
    becomes positional ('v2', 'v3', ...) instead of the ambiguous
    'unprobed' repeated on more than one file, and never collides with a
    real label."""
    if not quality_labels:
        return []

    if len(quality_labels) == 1:
        return [f" - {quality_labels[0] or 'unprobed'}"]

    names = []
    seen = set(label for label in quality_labels if label)
    fallback_n = 1
    for label in quality_labels:
        if label:
            names.append(label)
            continue
        fallback_n += 1
        while f"v{fallback_n}" in seen:
            fallback_n += 1
        names.append(f"v{fallback_n}")
        seen.add(names[-1])

    def rank(label):
        try:
            return _QUALITY_ORDER.index(label)
        except ValueError:
            return len(_QUALITY_ORDER)  # unknown/fallback: sorts after every real quality

    order = sorted(range(len(names)), key=lambda i: (rank(quality_labels[i]), i))
    position = {original_i: rank_n for rank_n, original_i in enumerate(order, start=1)}

    return [f" - {position[i]:02d} - {names[i]}" for i in range(len(names))]


def best_quality_first(relations, suffixes):
    """Pairs each relation with its filename suffix, ordered so the best
    quality is written first (rank 01 before 02, ...). Emby appears to keep
    whichever version of a title it meets first as the primary one, so the
    file creation order matters: writing in relation-id order made the
    older — usually lower-quality — relation the first file on disk."""
    return sorted(zip(relations, suffixes), key=lambda pair: pair[1])


def remove_stale_files(paths, stop_dir):
    """Deletes each path in `paths` (already-missing files are silently
    skipped) and then prunes any parent directory left empty by that
    deletion, walking upward but never at or above `stop_dir` — so a
    fully-removed title's whole folder disappears instead of leaving an
    empty husk behind, without ever touching anything above the
    configured library root. Returns the number of files actually
    deleted (not counting already-missing ones)."""
    stop_dir = os.path.normpath(stop_dir)
    removed = 0
    for path in paths:
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            continue

        parent = os.path.normpath(os.path.dirname(path))
        while parent != stop_dir and (parent + os.sep).startswith(stop_dir + os.sep):
            try:
                os.rmdir(parent)
            except OSError:
                break
            parent = os.path.dirname(parent)
    return removed


def predict_strm_write(path, content):
    """What write_strm_if_changed would do, without touching the file —
    the dry-run path for .strm generation. Reads the same way, just never
    opens the file for writing."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return "unchanged" if f.read() == content else "updated"
    return "created"


def write_strm_if_changed(path, content):
    """Writes `content` to `path` only if missing or different, so an
    unrelated regeneration run doesn't bump every file's mtime and force
    a full media-server rescan. Returns 'created', 'updated' or
    'unchanged'."""
    kind = predict_strm_write(path, content)
    if kind == "unchanged":
        return "unchanged"
    if kind == "updated":
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return "updated"

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return "created"


def write_strm(path, content, dry_run):
    """The only entry point a caller should use: decides itself whether to
    actually write or only predict, so every .strm write in this plugin
    goes through one place instead of each call site branching on
    dry_run itself."""
    return predict_strm_write(path, content) if dry_run else write_strm_if_changed(path, content)
