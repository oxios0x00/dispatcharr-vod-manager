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
    assert measurements.series_given_up(gave_up)
    assert measurements.series_ready(gave_up)


def test_series_ready_when_vod_probe_gave_up_on_a_series_whose_sample_episode_keeps_failing():
    # 30 episodes listed, but the provider answers 400 on the sample one:
    # vod-probe tried 5 times and left it "pending" for good.
    stuck = props(status="pending", episodes=30, attempts=5)
    assert measurements.series_given_up(stuck)
    assert measurements.series_ready(stuck)


def test_series_still_waits_while_vod_probe_may_try_again():
    assert not measurements.series_ready(props(status="pending", episodes=0, attempts=2))
    assert not measurements.series_ready(props(status="pending", episodes=0))
    assert not measurements.series_ready(props(status="pending", episodes=12, attempts=2))
    assert not measurements.series_given_up(props(status="ok", episodes=0, attempts=9))
    assert not measurements.series_given_up(None)
    assert not measurements.series_given_up(props(status="pending", episodes=0, attempts="many"))


def test_series_settled_for_error_partial_and_given_up_versions():
    # vod-probe only samples a few episodes: a dead season's episodes never get
    # a result, so a version whose summary says so must not be waited on.
    assert measurements.series_settled(props(status="error", probed_at="2026-11-01T10:00:00Z"))
    assert measurements.series_settled(props(status="partial", probed_at="2026-11-01T10:00:00Z"))
    assert measurements.series_settled(props(status="pending", episodes=30, attempts=3))
    assert not measurements.series_settled(props(status="ok", probed_at="2026-11-01T10:00:00Z"))
    assert not measurements.series_settled(props(status="pending", episodes=30, attempts=1))
    assert not measurements.series_settled(None)


def test_series_not_settled_while_vod_probe_will_visit_it_again():
    from datetime import datetime, timezone

    assert not measurements.series_settled(props(status="error", retry=True))
    assert not measurements.series_settled(props(status="partial", reload=True))
    moved = {"probe": {"status": "error", "last_modified": "100"}, "basic_data": {"last_modified": 200}}
    assert not measurements.series_settled(moved)
    same = {"probe": {"status": "error", "last_modified": "100"}, "basic_data": {"last_modified": 100}}
    assert measurements.series_settled(same)

    summary = props(status="partial", probed_at="2026-11-01T10:00:00Z")
    # Dispatcharr reloaded the episodes after the summary: their results were
    # erased and vod-probe redoes them, so missing ones are worth waiting for.
    reloaded = datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    assert not measurements.series_settled(summary, reloaded)
    # vod-probe's own reload, written just before its summary.
    own = datetime(2026, 11, 1, 10, 0, 1, tzinfo=timezone.utc)
    assert measurements.series_settled(summary, own)
    # A naive datetime is read as UTC.
    assert measurements.series_settled(summary, datetime(2026, 11, 1, 9, 0))
    assert not measurements.series_settled(props(status="error"), own)


def test_settled_state_only_turns_missing_into_failed():
    missing, measured, failed = None, props(status="ok", tier="1080p"), props(status="error")
    assert measurements.settled_state(missing, True) == measurements.FAILED
    assert measurements.settled_state(missing, False) == measurements.MISSING
    assert measurements.settled_state(measured, True) == measurements.MEASURED
    assert measurements.settled_state(failed, False) == measurements.FAILED
