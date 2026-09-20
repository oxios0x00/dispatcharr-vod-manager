import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from probe_summary import dumps_compact, summarize_probe
from store import Store

# Shaped like real ffprobe output: noisy tags and a disposition dict per track.
NOISE_TAGS = {
    "BPS": "10012704", "DURATION": "00:30:55.938000000", "NUMBER_OF_BYTES": "2322869759",
    "_STATISTICS_TAGS": "BPS DURATION NUMBER_OF_FRAMES NUMBER_OF_BYTES",
    "_STATISTICS_WRITING_APP": "mkvmerge v79.0 ('Funeral Pyres') 64-bit",
}
DISPOSITION = {k: 0 for k in (
    "dub", "forced", "lyrics", "comment", "karaoke", "captions", "metadata", "original",
    "dependent", "still_image", "attached_pic", "descriptions", "clean_effects",
    "visual_impaired", "hearing_impaired", "timed_thumbnails")}


def sample_probe():
    streams = [{
        "index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
        "width": 3840, "height": 1600, "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10",
        "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001",
        "color_transfer": "smpte2084", "color_primaries": "bt2020", "color_space": "bt2020nc",
        "tags": dict(NOISE_TAGS), "disposition": {**DISPOSITION, "default": 1},
        "side_data_list": [{"side_data_type": "DOVI configuration record", "dv_profile": 8,
                            "dv_level": 6, "rpu_present_flag": 1, "unrelated": "x"}],
    }]
    for i, (lang, ch) in enumerate([("eng", 6), ("fre", 2)], start=1):
        streams.append({
            "index": i, "codec_type": "audio", "codec_name": "eac3", "channels": ch,
            "channel_layout": "5.1(side)" if ch == 6 else "stereo", "sample_rate": "48000",
            "tags": {"language": lang, "title": "Dolby Atmos" if lang == "eng" else "", **NOISE_TAGS},
            "disposition": {**DISPOSITION, "default": 1 if lang == "eng" else 0},
        })
    for i in range(30):
        streams.append({
            "index": 3 + i, "codec_type": "subtitle", "codec_name": "subrip",
            "tags": {"language": "eng"}, "disposition": {**DISPOSITION, "forced": 1 if i == 0 else 0},
        })
    return {"streams": streams, "format": {"format_name": "matroska,webm", "duration": "1856.0",
                                            "bit_rate": "11573000", "size": "2322869759", "probe_score": 100}}


def test_summary_keeps_the_details_worth_having_later():
    s = summarize_probe(sample_probe())
    video = s["video"][0]
    assert video["codec_name"] == "hevc" and video["pix_fmt"] == "yuv420p10le"
    assert video["bits_per_raw_sample"] == "10" and video["r_frame_rate"] == "24000/1001"
    assert video["bps_tag"] == "10012704"
    assert video["side_data"] == [{"type": "DOVI configuration record", "dv_profile": 8,
                                   "dv_level": 6, "rpu_present_flag": 1}]
    assert s["audio"][0] == {"codec_name": "eac3", "channels": 6, "channel_layout": "5.1(side)",
                             "sample_rate": "48000", "language": "eng", "title": "Dolby Atmos",
                             "flags": ["default"]}
    assert "title" not in s["audio"][1]
    assert s["subtitles"] == {"codecs": {"subrip": 30}, "forced": 1}
    assert s["format"]["format_name"] == "matroska,webm" and "probe_score" not in s["format"]


def test_summary_is_a_small_fraction_of_the_raw_output():
    data = sample_probe()
    assert len(dumps_compact(summarize_probe(data))) < len(json.dumps(data)) / 4


def test_summary_of_nothing_is_empty_but_valid():
    s = summarize_probe(None)
    assert s == {"format": {}, "video": [], "audio": []}


def test_save_probe_stores_the_compact_summary_not_the_raw_output():
    tmp = tempfile.mkdtemp()
    try:
        store = Store(tmp)
        summary = summarize_probe(sample_probe())
        store.save_probe("movie", 1, {"ok": True, "quality_label": "2160p", "summary": summary})
        stored = store.get_probe("movie", 1)["raw_json"]
        assert json.loads(stored) == summary
    finally:
        shutil.rmtree(tmp)


def test_a_failed_probe_keeps_a_size_capped_raw_output_for_diagnosis():
    tmp = tempfile.mkdtemp()
    try:
        store = Store(tmp)
        store.save_probe("movie", 2, {"ok": False, "error": "no video stream found",
                                      "raw": {"streams": [{"codec_type": "audio", "pad": "x" * 50000}]}})
        stored = store.get_probe("movie", 2)["raw_json"]
        assert stored is not None and len(stored) <= 8000
    finally:
        shutil.rmtree(tmp)
