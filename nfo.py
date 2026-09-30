"""Kodi-style .nfo sidecar files, written next to each .strm this plugin
generates, carrying vod-probe's already-measured streamdetails so a future,
separate Emby/Jellyfin plugin can populate MediaInfo without ever probing the
stream itself — the whole reason this file exists is to avoid a second live
probe re-triggering Dispatcharr's proxy session-collision bug (see strm.py's
own module docstring and docs/troubleshooting.md).

Naming: `<strm-path>.nfo`, the full .strm filename with `.nfo` appended, never
a plain extension swap (`<basename>.nfo`). Confirmed against Jellyfin's own
source (MediaBrowser.XbmcMetadata.Savers.MovieNfoSaver.GetMovieSavePaths,
github.com/jellyfin/jellyfin) that its native "save local metadata" NFO saver
computes `Path.ChangeExtension(item.Path, ".nfo")` for every version of a
title but the first — landing exactly on `<basename>.nfo` — so appending
`.nfo` to the whole `.strm` name instead avoids that path outright, with no
dependency on a library setting the server saver is disabled. Emby's own
saver is closed-source and unconfirmed, but shares the same MediaBrowser
lineage as Jellyfin's, so the same precaution is taken for it too.

Pure Python, no Django, so it is unit-tested outside Dispatcharr."""
import xml.etree.ElementTree as ET

try:
    from . import measurements
except ImportError:  # imported as a top-level module by the unit tests
    import measurements


def nfo_path(strm_path):
    """The .nfo sidecar path for a given .strm path."""
    return strm_path + ".nfo"


def _text(parent, tag, value):
    if value in (None, ""):
        return
    ET.SubElement(parent, tag).text = str(value)


def _resolution(custom_properties):
    """(width, height) parsed from the top-level `resolution` string
    contract.py's quality_fields() writes (e.g. "3840x2160"), a sibling of
    `probe`, not part of it. (None, None) when absent or unparsable."""
    raw = (custom_properties or {}).get("resolution") or ""
    width, _, height = raw.partition("x")
    try:
        return int(width), int(height)
    except ValueError:
        return None, None


def build_nfo_xml(root_tag, tmdb_id, imdb_id, custom_properties):
    """A `<movie>` or `<episodedetails>` document: the TMDB/IMDB id (Kodi's
    own tag names, confirmed against Jellyfin's BaseNfoSaver — `tmdbid` and
    `imdbid` for both roots, `imdb_id` only applies to a `<tvshow>` root this
    plugin never writes) plus `<fileinfo><streamdetails>` built from
    vod-probe's `probe` block. Nothing else — no title/plot/cast, Emby/
    Jellyfin fills those in on its own from the id. Returns None when the
    relation isn't currently MEASURED (missing, still pending, or a failed
    probe's error block) — never fabricates streamdetails from a partial or
    error result, even though such a relation can still get a `.strm` (the
    'unprobed' filename fallback, see strm.py's plan_suffixes)."""
    if measurements.state(custom_properties) != measurements.MEASURED:
        return None
    probe = measurements.block(custom_properties)

    root = ET.Element(root_tag)
    _text(root, "tmdbid", tmdb_id)
    _text(root, "imdbid", imdb_id)

    fileinfo = ET.SubElement(root, "fileinfo")
    streamdetails = ET.SubElement(fileinfo, "streamdetails")

    video = ET.SubElement(streamdetails, "video")
    video_block = probe.get("video") or {}
    width, height = _resolution(custom_properties)
    _text(video, "codec", video_block.get("codec"))
    _text(video, "width", width)
    _text(video, "height", height)
    _text(video, "bitrate", video_block.get("bit_rate"))
    _text(video, "framerate", video_block.get("frame_rate"))
    _text(video, "durationinseconds", probe.get("duration_secs"))
    _text(video, "hdrtype", probe.get("hdr"))

    for track in probe.get("audio") or []:
        audio = ET.SubElement(streamdetails, "audio")
        _text(audio, "codec", track.get("codec"))
        _text(audio, "channels", track.get("channels"))
        _text(audio, "language", track.get("language"))

    for track in probe.get("subtitle") or []:
        subtitle = ET.SubElement(streamdetails, "subtitle")
        _text(subtitle, "codec", track.get("codec"))
        _text(subtitle, "language", track.get("language"))
        _text(subtitle, "forced", "true" if track.get("forced") else None)
        _text(subtitle, "hearingimpaired", "true" if track.get("hearing_impaired") else None)

    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n" + ET.tostring(root, encoding="unicode")
