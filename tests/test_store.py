import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from store import Store


def with_store(fn):
    tmp = tempfile.mkdtemp()
    try:
        fn(Store(tmp))
    finally:
        shutil.rmtree(tmp)


def test_enqueue_then_claim_then_done():
    def run(s):
        s.enqueue("movie", 1)
        assert s.queue_counts("movie") == {"pending": 1, "in_progress": 0, "done": 0, "error": 0}
        claimed = s.claim_batch("movie", 10)
        assert claimed == [1]
        s.mark_done("movie", 1)
        assert s.queue_counts("movie")["done"] == 1

    with_store(run)


def test_enqueue_is_a_noop_once_a_row_already_exists():
    # Documents the real (surprising) contract: enqueue() is INSERT ...
    # ON CONFLICT DO NOTHING. A title already marked 'done' stays 'done'
    # if you call enqueue() on it again — this is exactly the bug that
    # made scan_movies silently fail to requeue changed titles (it must
    # call requeue(), not enqueue(), for titles it has seen before).
    def run(s):
        s.enqueue("movie", 1)
        s.claim_batch("movie", 10)
        s.mark_done("movie", 1)
        assert s.queue_counts("movie")["done"] == 1

        s.enqueue("movie", 1)  # re-enqueue an already-done title
        assert s.queue_counts("movie")["done"] == 1, "enqueue() must not resurrect a done row"
        assert s.queue_counts("movie")["pending"] == 0

    with_store(run)


def test_requeue_resets_an_already_done_row_to_pending():
    def run(s):
        s.enqueue("movie", 1)
        s.claim_batch("movie", 10)
        s.mark_error("movie", 1, "boom")
        assert s.queue_counts("movie")["error"] == 1

        s.requeue("movie", 1)
        counts = s.queue_counts("movie")
        assert counts["pending"] == 1
        assert counts["error"] == 0

    with_store(run)


def test_claim_batch_only_returns_pending_and_marks_in_progress():
    def run(s):
        for i in (1, 2, 3):
            s.enqueue("movie", i)
        claimed = s.claim_batch("movie", 2)
        assert sorted(claimed) == [1, 2]
        counts = s.queue_counts("movie")
        assert counts["in_progress"] == 2
        assert counts["pending"] == 1

    with_store(run)


def test_known_relations_roundtrip():
    def run(s):
        assert s.get_known_relation_ids("movie", 1) is None
        s.set_known_relation_ids("movie", 1, {10, 11, 12})
        assert s.get_known_relation_ids("movie", 1) == {10, 11, 12}

    with_store(run)


def test_migrated_old_rows_default_probe_version_to_zero():
    # An old row saved before probe_version existed must read back as 0
    # (older than any real PROBE_SCHEMA_VERSION), so the "stale cache,
    # re-probe" check in plugin.py correctly treats it as needing a
    # re-probe rather than silently reusing pre-migration data forever.
    # This also predates content_type (pre-series-support schema) — the
    # migration must tag it 'movie', the only type that ever existed then.
    import sqlite3

    tmp = tempfile.mkdtemp()
    try:
        old_db = os.path.join(tmp, "state.sqlite3")
        conn = sqlite3.connect(old_db)
        conn.execute(
            """CREATE TABLE relation_probes (
                relation_id INTEGER PRIMARY KEY, probed_at REAL, ok INTEGER, error TEXT,
                width INTEGER, height INTEGER, quality_label TEXT, video_codec TEXT,
                hdr_type TEXT, audio_languages TEXT, audio_description_languages TEXT,
                subtitle_languages TEXT, duration_secs REAL, raw_json TEXT)"""
        )
        conn.execute(
            "INSERT INTO relation_probes (relation_id, probed_at, ok, quality_label) VALUES (1, 0, 1, '1080p')"
        )
        conn.commit()
        conn.close()

        s = Store(tmp)
        row = s.get_probe("movie", 1)
        assert row["probe_version"] == 0
        assert row["video_bitrate"] is None
        assert row["content_type"] == "movie"
    finally:
        shutil.rmtree(tmp)


def test_relation_probes_do_not_collide_across_content_types():
    # M3UMovieRelation and M3UEpisodeRelation are separate Django tables
    # with independent autoincrement ids — a movie relation and an episode
    # relation can share the same numeric id. The cache must not conflate
    # them (the bug the content_type composite key exists to prevent).
    def run(s):
        s.save_probe("movie", 42, {"ok": True, "quality_label": "1080p"})
        s.save_probe("episode", 42, {"ok": True, "quality_label": "720p"})

        movie_row = s.get_probe("movie", 42)
        episode_row = s.get_probe("episode", 42)
        assert movie_row["quality_label"] == "1080p"
        assert episode_row["quality_label"] == "720p"

    with_store(run)


def test_save_probe_records_sampled_from_relation_id():
    # Episode sampling copies one real probe's result onto sibling episode
    # relations in the same season/source — this field keeps that
    # extrapolation auditable rather than indistinguishable from a real probe.
    def run(s):
        s.save_probe("episode", 100, {"ok": True, "quality_label": "1080p"})
        s.save_probe(
            "episode", 101, {"ok": True, "quality_label": "1080p"},
            sampled_from_relation_id=100,
        )

        assert s.get_probe("episode", 100)["sampled_from_relation_id"] is None
        assert s.get_probe("episode", 101)["sampled_from_relation_id"] == 100

    with_store(run)


def test_get_quality_labels_bulk_lookup():
    def run(s):
        s.save_probe("episode", 1, {"ok": True, "quality_label": "2160p"})
        s.save_probe("episode", 2, {"ok": True, "quality_label": "1080p"})
        s.save_probe("episode", 3, {"ok": False, "quality_label": "720p"})  # not ok, excluded

        labels = s.get_quality_labels("episode", [1, 2, 3, 4])
        assert labels == {1: "2160p", 2: "1080p"}
        assert s.get_quality_labels("episode", []) == {}

    with_store(run)


def test_catalog_stats_snapshot_roundtrip():
    def run(s):
        assert s.get_latest_catalog_stats() == (None, [])
        taken_at = s.save_catalog_stats_snapshot([
            {"content_type": "movie", "quality_label": "2160p", "title_count": 5, "relation_count": 6},
            {"content_type": "series", "quality_label": "1080p", "title_count": 10, "relation_count": 200},
        ])
        latest_at, rows = s.get_latest_catalog_stats()
        assert latest_at == taken_at
        assert {r["content_type"] for r in rows} == {"movie", "series"}
        movie_row = next(r for r in rows if r["content_type"] == "movie")
        assert movie_row["title_count"] == 5 and movie_row["relation_count"] == 6

    with_store(run)


def test_pause_flag_persists():
    def run(s):
        assert s.is_paused("movie") is False
        s.set_paused("movie", True)
        assert s.is_paused("movie") is True

    with_store(run)


def test_pause_flag_is_independent_per_content_type():
    # A movie circuit-breaker trip (or a manual pause) must not also pause
    # series processing, and vice versa.
    def run(s):
        s.set_paused("movie", True)
        assert s.is_paused("movie") is True
        assert s.is_paused("series") is False

    with_store(run)


def test_strm_manifest_starts_empty():
    def run(s):
        assert s.get_strm_manifest("movie") == set()

    with_store(run)


def test_strm_manifest_roundtrip():
    def run(s):
        s.save_strm_manifest("movie", ["/a.strm", "/b.strm"])
        assert s.get_strm_manifest("movie") == {"/a.strm", "/b.strm"}

    with_store(run)


def test_strm_manifest_save_replaces_previous_set():
    # A relation that's no longer generated must disappear from the
    # manifest on the next save, not linger forever.
    def run(s):
        s.save_strm_manifest("movie", ["/a.strm", "/b.strm"])
        s.save_strm_manifest("movie", ["/b.strm", "/c.strm"])
        assert s.get_strm_manifest("movie") == {"/b.strm", "/c.strm"}

    with_store(run)


def test_strm_manifest_is_independent_per_content_type():
    def run(s):
        s.save_strm_manifest("movie", ["/movies/a.strm"])
        s.save_strm_manifest("episode", ["/series/a.strm"])
        assert s.get_strm_manifest("movie") == {"/movies/a.strm"}
        assert s.get_strm_manifest("episode") == {"/series/a.strm"}

    with_store(run)


def test_delete_probe_removes_only_the_targeted_relation():
    def run(s):
        s.save_probe("movie", 10, {"ok": True, "quality_label": "1080p"})
        s.save_probe("movie", 11, {"ok": True, "quality_label": "2160p"})

        s.delete_probe("movie", 10)

        assert s.get_probe("movie", 10) is None
        assert s.get_probe("movie", 11) is not None

    with_store(run)


def test_delete_probe_is_a_noop_when_nothing_cached():
    def run(s):
        s.delete_probe("movie", 999)  # must not raise
        assert s.get_probe("movie", 999) is None

    with_store(run)


def test_reset_all_clears_every_table():
    def run(s):
        s.enqueue("movie", 1)
        s.set_known_relation_ids("movie", 1, {10, 11})
        s.save_probe("movie", 10, {"ok": True, "quality_label": "1080p"})
        s.set_paused("movie", True)
        s.start_run("movie", dry_run=True)
        s.save_catalog_stats_snapshot([
            {"content_type": "movie", "quality_label": "1080p", "title_count": 1, "relation_count": 1},
        ])
        s.save_strm_manifest("movie", ["/a.strm"])

        s.reset_all()

        assert s.queue_counts("movie") == {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
        assert s.get_known_relation_ids("movie", 1) is None
        assert s.get_probe("movie", 10) is None
        assert s.is_paused("movie") is False
        assert s.get_latest_catalog_stats() == (None, [])
        assert s.get_strm_manifest("movie") == set()

    with_store(run)


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
