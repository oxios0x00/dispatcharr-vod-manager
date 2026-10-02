import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import measurements


def props(**probe):
    return {"probe": probe}


def test_state_tells_measured_failed_and_missing_apart():
    assert measurements.state(None) == measurements.MISSING
    assert measurements.state({}) == measurements.MISSING
    assert measurements.state(props(status="ok", tier="1080p")) == measurements.MEASURED
    assert measurements.state(props(status="inferred", tier="2160p")) == measurements.MEASURED
    assert measurements.state(props(status="error")) == measurements.FAILED
    assert measurements.state(props(status="unreachable")) == measurements.FAILED
    # A block that says ok but carries no tier is not usable yet.
    assert measurements.state(props(status="ok")) == measurements.MISSING
    assert measurements.state(props(status="pending")) == measurements.MISSING
    assert measurements.state({"probe": "not a dict"}) == measurements.MISSING


def test_tier_only_for_a_usable_block():
    assert measurements.tier(props(status="ok", tier="720p")) == "720p"
    assert measurements.tier(props(status="error", tier="720p")) is None


def test_languages_skip_audio_description_tracks():
    tracks = [
        {"language": "fre"},
        {"language": "eng", "audio_description": True},
        {"language": "ger"},
    ]
    assert measurements.languages(props(status="ok", tier="1080p", audio=tracks)) == ["fre", "ger"]


def test_languages_fall_back_to_the_flat_list():
    assert measurements.languages(props(status="ok", tier="1080p", audio_languages=["fre", "eng"])) == ["fre", "eng"]
    assert measurements.languages(None) == []


def test_bitrate_prefers_the_whole_file_then_the_video_stream():
    assert measurements.bitrate(props(bit_rate=20_000_000, video={"bit_rate": 19_000_000})) == 20_000_000
    assert measurements.bitrate(props(video={"bit_rate": 19_000_000})) == 19_000_000
    assert measurements.bitrate(props(status="ok")) is None


def test_series_ready_needs_a_complete_summary():
    assert measurements.series_ready(props(status="ok", episodes=8))
    assert not measurements.series_ready(props(status="pending"))
    assert not measurements.series_ready(None)


def test_series_ready_accepts_error_as_a_final_answer():
    # vod-probe writes "error" for a series version whose every episode
    # failed, instead of leaving it "pending" forever — a series like that
    # is done being decided, not still waiting.
    assert measurements.series_ready(props(status="error"))


def test_series_ready_accepts_partial_as_a_final_answer():
    # vod-probe writes "partial" when some seasons answered and one or more
    # are confirmed dead — also done deciding, even though it's a mix.
    assert measurements.series_ready(props(status="partial"))


def test_series_ready_when_vod_probe_gave_up_on_a_series_with_no_episode():
    # vod-probe stops reloading an empty series after 3 tries but leaves it
    # "pending"; waiting on it would never end.
    gave_up = props(status="pending", episodes=0, attempts=3)
    assert measurements.series_given_up_empty(gave_up)
    assert measurements.series_ready(gave_up)


def test_series_still_waits_while_vod_probe_may_try_again():
    assert not measurements.series_ready(props(status="pending", episodes=0, attempts=2))
    assert not measurements.series_ready(props(status="pending", episodes=0))
    # episodes were seen: it is sampling them, not empty
    assert not measurements.series_ready(props(status="pending", episodes=12, attempts=5))
    assert not measurements.series_given_up_empty(props(status="ok", episodes=0, attempts=9))
    assert not measurements.series_given_up_empty(None)
    assert not measurements.series_given_up_empty(props(status="pending", episodes=0, attempts="many"))
