"""The batch loops and the per-title selection need Django only to look up
relations; a minimal stand-in for apps.vod.models lets them run here with fake
titles whose custom_properties carry vod-probe's blocks."""
import contextlib
import sys
import types
from types import SimpleNamespace

from test_pipeline import plugin_module, with_plugin

SETTINGS = {
    "dry_run": False,
    "batch_size": 20,
    "series_batch_size": 20,
    "target_qualities": "2160p,1080p",
    "target_languages": "fre,eng",
}


class FakeQuerySet:
    def __init__(self, manager, rows):
        self.manager, self.rows = manager, rows

    def filter(self, **kwargs):
        if self.manager.fail_lookups:
            raise RuntimeError("connection lost")
        rows = self.rows
        for key, wanted in kwargs.items():
            if key == "m3u_account__is_active":
                continue
            if key.endswith("__in"):
                rows = [r for r in rows if self._value(r, key[:-4]) in wanted]
            else:
                rows = [r for r in rows if self._value(r, key) == wanted]
        return FakeQuerySet(self.manager, rows)

    @staticmethod
    def _value(row, path):
        value = row
        for part in path.split("__"):
            value = getattr(value, part)
        return value

    def __iter__(self):
        return iter(self.rows)

    def values_list(self, *_fields, **_kwargs):
        return [r.id for r in self.rows]

    def delete(self):
        for row in self.rows:
            self.manager.rows.remove(row)


class FakeManager(FakeQuerySet):
    def __init__(self, rows, fail_lookups=False):
        self.rows, self.fail_lookups = rows, fail_lookups
        self.manager = self


@contextlib.contextmanager
def fake_django(movie_relations=(), series_relations=(), episode_relations=(), fail_lookups=False):
    names = ("apps", "apps.vod", "apps.vod.models")
    saved = {name: sys.modules.get(name) for name in names}
    models = types.ModuleType("apps.vod.models")
    for cls_name, rows in (
        ("M3UMovieRelation", list(movie_relations)),
        ("M3USeriesRelation", list(series_relations)),
        ("M3UEpisodeRelation", list(episode_relations)),
    ):
        cls = type(cls_name, (), {})
        cls.objects = FakeManager(rows, fail_lookups)
        setattr(models, cls_name, cls)
    apps, vod = types.ModuleType("apps"), types.ModuleType("apps.vod")
    apps.vod, vod.models = vod, models
    sys.modules.update({"apps": apps, "apps.vod": vod, "apps.vod.models": models})
    try:
        yield models
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def measured(tier, languages=("fre",), bit_rate=8_000_000, status="ok"):
    return {"probe": {
        "status": status, "tier": tier, "bit_rate": bit_rate,
        "audio": [{"language": lang} for lang in languages],
    }}


def movie(relation_id, movie_id, properties):
    return SimpleNamespace(id=relation_id, movie_id=movie_id, custom_properties=properties)


def series_relation(relation_id, series_id, properties=None):
    return SimpleNamespace(id=relation_id, series_id=series_id, custom_properties=properties or {"probe": {"status": "ok"}})


def episode(relation_id, episode_id, series_id, properties):
    return SimpleNamespace(
        id=relation_id, episode_id=episode_id, episode=SimpleNamespace(series_id=series_id),
        custom_properties=properties,
    )


def enqueue_all(plugin, content_type, ids):
    for content_id in ids:
        plugin.store.enqueue(content_type, content_id)


def process_one(plugin, content_type, settings=None):
    settings = {**SETTINGS, **(settings or {})}
    return plugin._process_batch(content_type, settings)


# --- the batch loop ---------------------------------------------------------


def test_a_failed_movie_records_its_relations_so_scan_leaves_it_alone():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2])
        rows = [
            movie(10, 1, measured("1080p", status="error")), movie(11, 1, {"probe": {"status": "unreachable"}}),
            movie(20, 2, measured("1080p")),
        ]
        with fake_django(movie_relations=rows):
            result = process_one(plugin, "movie")
        assert result["processed"] == 2 and result["errors"] == 1
        counts = plugin.store.queue_counts("movie")
        assert counts["error"] == 1 and counts["done"] == 1
        assert plugin.store.get_known_relation_ids("movie", 1) == {10, 11}
        assert plugin.store.changed_content_ids("movie", {1: {10, 11}}) == []

    with_plugin(run)


def test_a_failed_series_records_its_relations_too():
    def run(plugin):
        enqueue_all(plugin, "series", [7])
        with fake_django(
            series_relations=[series_relation(70, 7), series_relation(71, 7)],
            episode_relations=[episode(700, 1, 7, {"probe": {"status": "error"}})],
        ):
            result = process_one(plugin, "series")
        assert result["errors"] == 1
        assert plugin.store.queue_counts("series")["error"] == 1
        assert plugin.store.get_known_relation_ids("series", 7) == {70, 71}

    with_plugin(run)


def test_a_failing_relation_lookup_does_not_abort_the_batch():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2])

        def boom(*_args):
            raise RuntimeError("boom")

        plugin._process_one_movie = boom
        with fake_django(fail_lookups=True):
            result = process_one(plugin, "movie")
        assert result["processed"] == 2 and result["errors"] == 2
        assert plugin.store.queue_counts("movie")["in_progress"] == 0

    with_plugin(run)


def test_a_title_vod_probe_has_not_measured_waits_and_is_not_counted_as_processed():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2])
        rows = [movie(10, 1, {}), movie(20, 2, measured("1080p"))]
        with fake_django(movie_relations=rows):
            result = process_one(plugin, "movie")
        assert result["claimed"] == 2 and result["processed"] == 1 and result["waiting"] == 1
        counts = plugin.store.queue_counts("movie")
        assert counts["waiting"] == 1 and counts["done"] == 1 and counts["pending"] == 0
        assert "waiting for vod-probe" in result["message"]

    with_plugin(run)


def test_each_finished_title_reports_progress():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2, 3])
        rows = [movie(i * 10, i, measured("1080p")) for i in (1, 2, 3)]
        ticks = []
        with fake_django(movie_relations=rows):
            plugin._process_batch("movie", SETTINGS, on_progress=lambda: ticks.append(1))
        assert len(ticks) == 3

    with_plugin(run)


# --- selecting movies from vod-probe's measurements -------------------------


def test_the_best_measured_version_is_kept_and_the_others_pruned():
    def run(plugin):
        enqueue_all(plugin, "movie", [1])
        rows = [
            movie(10, 1, measured("2160p", ("fre", "eng"))),
            movie(11, 1, measured("1080p", ("fre",))),
            movie(12, 1, measured("720p", ("fre",))),
            movie(13, 1, {"probe": {"status": "error"}}),
        ]
        with fake_django(movie_relations=rows) as models:
            result = process_one(plugin, "movie")
            remaining = sorted(r.id for r in models.M3UMovieRelation.objects.rows)
        # One winner per requested tier (2160p, 1080p); the 720p and the failed one go.
        assert remaining == [10, 11]
        assert result["pruned"] == 2

    with_plugin(run)


def test_dry_run_prunes_nothing():
    def run(plugin):
        enqueue_all(plugin, "movie", [1])
        rows = [movie(10, 1, measured("2160p")), movie(11, 1, measured("720p"))]
        with fake_django(movie_relations=rows) as models:
            result = process_one(plugin, "movie", {"dry_run": True})
            assert len(models.M3UMovieRelation.objects.rows) == 2
        assert result["pruned"] == 1

    with_plugin(run)


def test_audio_description_tracks_do_not_count_as_a_language():
    def run(plugin):
        enqueue_all(plugin, "movie", [1])
        with_ad = {"probe": {"status": "ok", "tier": "1080p", "bit_rate": 9_000_000,
                             "audio": [{"language": "fre", "audio_description": True}, {"language": "ger"}]}}
        real_fr = measured("1080p", ("fre",), bit_rate=1_000_000)
        rows = [movie(10, 1, with_ad), movie(11, 1, real_fr)]
        with fake_django(movie_relations=rows) as models:
            process_one(plugin, "movie", {"target_qualities": "1080p", "target_languages": "fre"})
            remaining = sorted(r.id for r in models.M3UMovieRelation.objects.rows)
        # Only the second version really carries French, whatever the bitrate says.
        assert remaining == [11]

    with_plugin(run)


def test_a_movie_with_no_target_quality_is_dropped_only_when_asked():
    def run(plugin):
        rows = lambda: [movie(10, 1, measured("720p"))]  # noqa: E731
        enqueue_all(plugin, "movie", [1])
        with fake_django(movie_relations=rows()) as models:
            process_one(plugin, "movie")
            assert len(models.M3UMovieRelation.objects.rows) == 1
        plugin.store.requeue("movie", 1)
        with fake_django(movie_relations=rows()) as models:
            process_one(plugin, "movie", {"exclude_unmatched_quality": True})
            assert models.M3UMovieRelation.objects.rows == []

    with_plugin(run)


# --- selecting series --------------------------------------------------------


def test_a_series_is_decided_per_episode_from_its_episode_measurements():
    def run(plugin):
        enqueue_all(plugin, "series", [7])
        episodes = [
            episode(700, 1, 7, measured("2160p", ("fre",))), episode(701, 1, 7, measured("1080p", ("fre",))),
            episode(702, 1, 7, measured("720p", ("fre",))),
            episode(710, 2, 7, measured("1080p", ("fre",), status="inferred")),
        ]
        with fake_django(series_relations=[series_relation(70, 7)], episode_relations=episodes) as models:
            result = process_one(plugin, "series")
            remaining = sorted(r.id for r in models.M3UEpisodeRelation.objects.rows)
        assert remaining == [700, 701, 710] and result["pruned"] == 1
        assert plugin.store.get_known_relation_ids("series", 7) == {70}

    with_plugin(run)


def test_a_series_waits_until_vod_probe_has_finished_it_and_prunes_nothing_meanwhile():
    def run(plugin):
        enqueue_all(plugin, "series", [7, 8])
        series = [series_relation(70, 7, {"probe": {"status": "pending"}}), series_relation(80, 8)]
        episodes = [
            episode(700, 1, 7, measured("2160p")), episode(701, 1, 7, measured("720p")),
            episode(800, 1, 8, measured("2160p")), episode(801, 1, 8, {}),  # not measured yet
        ]
        with fake_django(series_relations=series, episode_relations=episodes) as models:
            result = process_one(plugin, "series")
            assert len(models.M3UEpisodeRelation.objects.rows) == 4
        assert result["waiting"] == 2 and result["processed"] == 0
        assert plugin.store.queue_counts("series")["waiting"] == 2

    with_plugin(run)
