import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from nfo import build_nfo_xml, nfo_path


def _measured(**overrides):
    probe = {
        "schema_version": 3,
        "status": "ok",
        "tier": "2160p",
        "hdr": "dolbyvision",
        "video": {"codec": "hevc", "profile": "Main 10", "bit_depth": 10, "bit_rate": 15000000, "frame_rate": 23.976},
        "audio_languages": ["fre", "eng"],
        "audio": [
            {"codec": "eac3", "channels": 6, "language": "fre"},
            {"codec": "aac", "channels": 2, "language": "eng"},
        ],
        "subtitle_languages": ["fre", "eng"],
        "subtitle": [
            {"codec": "subrip", "language": "fre"},
            {"codec": "subrip", "language": "eng", "forced": True, "hearing_impaired": True},
        ],
        "duration_secs": 7200,
        "container": "matroska,webm",
        "bit_rate": 18000000,
    }
    probe.update(overrides.pop("probe_overrides", {}))
    return {"resolution": "3840x2160", "probe": probe, **overrides}


def test_nfo_path_appends_dot_nfo_to_the_whole_strm_name():
    # Never a plain extension swap (Path.ChangeExtension-style), so it can
    # never collide with Jellyfin/Emby's own native NFO saver, which computes
    # exactly that swap for every version of a title but the first (confirmed
    # against Jellyfin's MovieNfoSaver.GetMovieSavePaths).
    assert nfo_path("/lib/movies/Napoleon/Napoleon - 01 - 2160p.strm") == \
        "/lib/movies/Napoleon/Napoleon - 01 - 2160p.strm.nfo"


def test_build_nfo_xml_root_tag_and_ids():
    xml = build_nfo_xml("movie", "631842", "tt1234567", _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.tag == "movie"
    assert root.find("tmdbid").text == "631842"
    assert root.find("imdbid").text == "tt1234567"


def test_build_nfo_xml_uses_episodedetails_root_for_episodes():
    xml = build_nfo_xml("episodedetails", "631842", None, _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.tag == "episodedetails"


def test_build_nfo_xml_omits_id_elements_when_unknown():
    xml = build_nfo_xml("movie", None, None, _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("tmdbid") is None
    assert root.find("imdbid") is None


def test_build_nfo_xml_streamdetails_video_fields():
    xml = build_nfo_xml("movie", "1", None, _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    video = root.find("fileinfo/streamdetails/video")
    assert video.find("codec").text == "hevc"
    assert video.find("width").text == "3840"
    assert video.find("height").text == "2160"
    assert video.find("bitrate").text == "15000000"
    assert video.find("framerate").text == "23.976"
    assert video.find("durationinseconds").text == "7200"
    assert video.find("hdrtype").text == "dolbyvision"


def test_build_nfo_xml_one_audio_element_per_track_in_order():
    xml = build_nfo_xml("movie", "1", None, _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    tracks = root.findall("fileinfo/streamdetails/audio")
    assert [t.find("language").text for t in tracks] == ["fre", "eng"]
    assert tracks[0].find("codec").text == "eac3"
    assert tracks[0].find("channels").text == "6"


def test_build_nfo_xml_one_subtitle_element_per_track_in_order():
    xml = build_nfo_xml("movie", "1", None, _measured())
    root = ET.fromstring(xml.split("?>", 1)[1])
    tracks = root.findall("fileinfo/streamdetails/subtitle")
    assert [t.find("language").text for t in tracks] == ["fre", "eng"]
    assert tracks[0].find("codec").text == "subrip"
    assert tracks[0].find("forced") is None  # not flagged on this track, no empty tag written
    assert tracks[0].find("hearingimpaired") is None
    assert tracks[1].find("forced").text == "true"
    assert tracks[1].find("hearingimpaired").text == "true"


def test_build_nfo_xml_has_no_subtitle_elements_for_an_older_probe_block_without_them():
    # schema_version 5 and earlier never wrote `subtitle` at all — graceful
    # degradation, not a crash, for a relation vod-probe hasn't re-measured yet.
    cp = _measured()
    del cp["probe"]["subtitle"]
    xml = build_nfo_xml("movie", "1", None, cp)
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.findall("fileinfo/streamdetails/subtitle") == []


def test_build_nfo_xml_omits_duration_for_an_inferred_episode():
    # vod-probe deliberately strips duration_secs from an inferred block
    # (episode-specific data copied from a sibling) — never fabricated here.
    cp = _measured(probe_overrides={"status": "inferred"})
    del cp["probe"]["duration_secs"]
    xml = build_nfo_xml("episodedetails", "1", None, cp)
    root = ET.fromstring(xml.split("?>", 1)[1])
    assert root.find("fileinfo/streamdetails/video/durationinseconds") is None


def test_build_nfo_xml_returns_none_when_never_measured():
    assert build_nfo_xml("movie", "1", None, {}) is None
    assert build_nfo_xml("movie", "1", None, None) is None


def test_build_nfo_xml_returns_none_for_a_failed_probe():
    # A relation can still get a .strm (the 'unprobed' filename fallback)
    # despite a failed probe — never fabricate streamdetails for it.
    cp = {"probe": {"schema_version": 3, "status": "error", "error": "boom"}}
    assert build_nfo_xml("movie", "1", None, cp) is None


def test_build_nfo_xml_returns_none_while_still_pending():
    cp = {"probe": {"schema_version": 3, "status": "ok"}}  # no tier yet
    assert build_nfo_xml("movie", "1", None, cp) is None


if __name__ == "__main__":
    tests = [obj for name, obj in list(globals().items()) if name.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"OK   {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
