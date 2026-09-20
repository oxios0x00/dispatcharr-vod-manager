"""A compact summary of an ffprobe result, kept in place of the raw output.

ffprobe's full JSON runs to tens of kilobytes per stream, most of it noise
(the muxer's own statistics tags, a dozen disposition flags repeated on every
track). The plugin already extracts what it decides on into dedicated
columns; this keeps the extra details a future feature might want — bit
depth, frame rate, audio codec and channel layout, Dolby Vision profile,
subtitle formats — at roughly a kilobyte instead, so a new criterion can be
computed later without probing thousands of streams again.

Pure Python, no Django, so it is unit-tested outside Dispatcharr."""
import json

_VIDEO_KEYS = (
    "codec_name", "profile", "level", "width", "height", "pix_fmt",
    "bits_per_raw_sample", "r_frame_rate", "avg_frame_rate", "field_order",
    "color_range", "color_space", "color_transfer", "color_primaries",
    "display_aspect_ratio", "bit_rate",
)
_AUDIO_KEYS = ("codec_name", "profile", "channels", "channel_layout", "sample_rate", "bit_rate")
_FORMAT_KEYS = ("format_name", "duration", "bit_rate", "size")
# Only the flags that change how a track should be read are worth keeping.
_DISPOSITION_FLAGS = ("default", "forced", "hearing_impaired", "visual_impaired", "comment", "original")
_DOVI_KEYS = ("dv_profile", "dv_level", "rpu_present_flag", "bl_present_flag", "dv_bl_signal_compatibility_id")


def _pick(source, keys):
    return {k: source[k] for k in keys if source.get(k) not in (None, "", "N/A")}


def _flags(stream):
    disposition = stream.get("disposition") or {}
    return [name for name in _DISPOSITION_FLAGS if disposition.get(name)]


def _video_summary(stream):
    out = _pick(stream, _VIDEO_KEYS)
    bps = (stream.get("tags") or {}).get("BPS")
    if bps:
        out["bps_tag"] = bps
    side_data = []
    for entry in stream.get("side_data_list") or []:
        item = {"type": entry.get("side_data_type")}
        item.update(_pick(entry, _DOVI_KEYS))
        side_data.append(item)
    if side_data:
        out["side_data"] = side_data
    return out


def _audio_summary(stream):
    out = _pick(stream, _AUDIO_KEYS)
    tags = stream.get("tags") or {}
    if tags.get("language"):
        out["language"] = tags["language"]
    if tags.get("title"):
        out["title"] = tags["title"]
    flags = _flags(stream)
    if flags:
        out["flags"] = flags
    return out


def summarize_probe(data):
    """Compact dict built from a parsed `ffprobe -show_format -show_streams`
    JSON document."""
    data = data or {}
    streams = data.get("streams") or []
    subtitle_codecs = {}
    subtitle_flags = {}
    for stream in streams:
        if stream.get("codec_type") != "subtitle":
            continue
        codec = stream.get("codec_name") or "unknown"
        subtitle_codecs[codec] = subtitle_codecs.get(codec, 0) + 1
        for flag in _flags(stream):
            if flag in ("forced", "hearing_impaired"):
                subtitle_flags[flag] = subtitle_flags.get(flag, 0) + 1

    summary = {
        "format": _pick(data.get("format") or {}, _FORMAT_KEYS),
        "video": [_video_summary(s) for s in streams if s.get("codec_type") == "video"],
        "audio": [_audio_summary(s) for s in streams if s.get("codec_type") == "audio"],
    }
    if subtitle_codecs:
        summary["subtitles"] = {"codecs": subtitle_codecs, **subtitle_flags}
    return summary


def dumps_compact(summary):
    return json.dumps(summary, separators=(",", ":"), ensure_ascii=False)
