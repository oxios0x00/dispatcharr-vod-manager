"""Technical stream probing via ffprobe.

Validated empirically against a real Dispatcharr instance (see NOTES.md
point 1): pointing ffprobe directly at the Dispatcharr-served stream URL
lets ffmpeg's own HTTP client issue the Range requests and seeks it needs
(including following Dispatcharr's redirect chain to the real provider,
and locating a trailing `moov` atom on non-faststart MP4s) — a few MB read
even on multi-GB files. We do NOT pre-download a fixed head chunk; that
approach was tested and fails on a meaningful fraction of real MP4s.
"""
import json
import subprocess

# Bump whenever probe_stream()'s output shape changes in a way that
# affects selection (new field, changed extraction logic) — a cached
# probe row saved under an older version is treated as stale and
# re-probed, rather than silently reused with a missing/wrong field. See
# NOTES.md point 12: discovered when video_bitrate was added and existing
# cached rows kept showing bitrate=None for files that genuinely had a
# usable BPS tag, because they were probed before that extraction existed.
PROBE_SCHEMA_VERSION = 3

FFPROBE_BIN = "ffprobe"
DEFAULT_TIMEOUT_SECONDS = 25
DEFAULT_PROBESIZE = 20_000_000       # bytes
DEFAULT_ANALYZEDURATION = 20_000_000  # microseconds

# Best-effort substrings for audio-description detection via a track's
# free-text title tag. This is a heuristic, not a guarantee — see
# NOTES.md point 6. The disposition.visual_impaired flag (checked
# separately) is the more trustworthy of the two signals when present.
_AD_TITLE_HINTS = (
    "audiodescription",
    "audio description",
    "audio-description",
    "descriptive audio",
    " ad)",
    "(ad)",
    " ad ",
    "narration",
)


def classify_quality(width, height):
    """Height alone misclassifies real UHD masters: a 3840x1920 cinemascope
    (2:1) encode is genuine 4K but has a height below the naive 2160p
    threshold. Confirmed on a real stream (Ted Lasso, Dispatcharr-test) —
    ffprobe reported width=3840/height=1920, Emby correctly displayed it
    as 4K HEVC, and this function returned '1080p'. Checking width OR
    height against each tier's threshold (whichever is taller) catches
    both standard and cinematic aspect ratios."""
    w = width or 0
    h = height or 0
    if not w and not h:
        return "unknown"
    if w >= 3200 or h >= 2000:
        return "2160p"
    if w >= 1600 or h >= 1000:
        return "1080p"
    if w >= 1000 or h >= 700:
        return "720p"
    if w >= 600 or h >= 480:
        return "480p"
    return "sd"


def classify_hdr(video_stream):
    transfer = (video_stream.get("color_transfer") or "").lower()
    codec_tag = (video_stream.get("codec_tag_string") or "").lower()
    side_data = video_stream.get("side_data_list") or []
    for entry in side_data:
        entry_type = str(entry.get("side_data_type", "")).lower()
        if "dovi" in entry_type or "dolby vision" in entry_type:
            return "dolby_vision"
    if codec_tag.startswith("dvh"):
        return "dolby_vision"
    if transfer == "smpte2084":
        return "hdr10"
    if transfer == "arib-std-b67":
        return "hlg"
    return "sdr"


def _extract_video_bitrate(video_stream):
    """ffprobe's standard `bit_rate` field is populated for many MP4s but
    is often empty for Matroska, where the real value only shows up in the
    mkvmerge-written `BPS` tag instead (verified empirically against real
    files of both formats — see NOTES.md point 12). Try both, in that
    order; return None if neither is present rather than guessing."""
    bit_rate = video_stream.get("bit_rate")
    if bit_rate:
        try:
            return int(bit_rate)
        except (TypeError, ValueError):
            pass
    bps_tag = (video_stream.get("tags") or {}).get("BPS")
    if bps_tag:
        try:
            return int(bps_tag)
        except (TypeError, ValueError):
            pass
    return None


def _looks_like_audio_description(stream):
    disposition = stream.get("disposition") or {}
    if disposition.get("visual_impaired"):
        return True
    title = str((stream.get("tags") or {}).get("title", "")).lower()
    return any(hint in title for hint in _AD_TITLE_HINTS)


def probe_stream(url, timeout_seconds=DEFAULT_TIMEOUT_SECONDS):
    """Probe a stream URL directly (no pre-download). Returns a plain dict,
    always with an 'ok' key; never raises for expected failure modes
    (timeout, unreachable, unparseable) — callers should check 'ok'."""
    cmd = [
        FFPROBE_BIN,
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-analyzeduration", str(DEFAULT_ANALYZEDURATION),
        "-probesize", str(DEFAULT_PROBESIZE),
        url,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout_seconds}s"}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe binary not found on PATH"}

    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "ffprobe failed").strip()[:2000]}

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"could not parse ffprobe output: {exc}"}

    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    subtitle_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    if not video_streams:
        return {"ok": False, "error": "no video stream found", "raw": data}

    video = video_streams[0]
    width = video.get("width")
    height = video.get("height")

    audio_languages = []
    ad_languages = []
    for a in audio_streams:
        lang = (a.get("tags") or {}).get("language", "und")
        audio_languages.append(lang)
        if _looks_like_audio_description(a):
            ad_languages.append(lang)

    subtitle_languages = [
        (s.get("tags") or {}).get("language", "und") for s in subtitle_streams
    ]

    duration = None
    fmt = data.get("format") or {}
    if fmt.get("duration"):
        try:
            duration = float(fmt["duration"])
        except (TypeError, ValueError):
            duration = None

    return {
        "ok": True,
        "probe_version": PROBE_SCHEMA_VERSION,
        "width": width,
        "height": height,
        "quality_label": classify_quality(width, height),
        "video_codec": video.get("codec_name"),
        "video_bitrate": _extract_video_bitrate(video),
        "hdr_type": classify_hdr(video),
        "audio_languages": audio_languages,
        "audio_description_languages": ad_languages,
        "subtitle_languages": subtitle_languages,
        "duration_secs": duration,
        "raw": data,
    }
