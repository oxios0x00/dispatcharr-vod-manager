"""The movie and series batch loops need Django only to look up relation ids;
a minimal stand-in for apps.vod.models lets them run here with fake titles."""
import contextlib
import sys
import time
import types

from test_pipeline import plugin_module, with_plugin

SETTINGS = {
    "dry_run": False,
    "batch_size": 20,
    "series_batch_size": 20,
    "max_concurrent_probes": 1,
    "max_probes_per_second": 0,
}


class FakeRelations:
    """Stands in for M3U*Relation.objects: filter(...) then values_list(...)."""

    def __init__(self, id_field, relations_by_title, fail_lookups=False):
        self.id_field = id_field
        self.relations_by_title = relations_by_title
        self.fail_lookups = fail_lookups
        self._title = None

    def filter(self, **kwargs):
        if self.fail_lookups:
            raise RuntimeError("connection lost")
        clone = FakeRelations(self.id_field, self.relations_by_title)
        clone._title = kwargs[self.id_field]
        return clone

    def values_list(self, *_fields, **_kwargs):
        return sorted(self.relations_by_title.get(self._title, ()))


@contextlib.contextmanager
def fake_django(relations_by_title, fail_lookups=False):
    saved = {name: sys.modules.get(name) for name in ("apps", "apps.vod", "apps.vod.models")}
    models = types.ModuleType("apps.vod.models")
    for cls_name, field in (("M3UMovieRelation", "movie_id"), ("M3USeriesRelation", "series_id")):
        cls = type(cls_name, (), {})
        cls.objects = FakeRelations(field, relations_by_title, fail_lookups)
        setattr(models, cls_name, cls)
    apps, vod = types.ModuleType("apps"), types.ModuleType("apps.vod")
    apps.vod, vod.models = vod, models
    sys.modules.update({"apps": apps, "apps.vod": vod, "apps.vod.models": models})
    try:
        yield
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def enqueue_all(plugin, content_type, ids):
    for content_id in ids:
        plugin.store.enqueue(content_type, content_id)


def test_a_failed_movie_records_its_relations_so_scan_leaves_it_alone():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2])

        def process_one(movie_id, *_args):
            if movie_id == 1:
                raise RuntimeError("no relation could be probed")
            return 0

        plugin._process_one_movie = process_one
        with fake_django({1: {10, 11}, 2: {20}}):
            result = plugin._process_batch(SETTINGS)
        assert result["processed"] == 2 and result["errors"] == 1
        counts = plugin.store.queue_counts("movie")
        assert counts["error"] == 1 and counts["done"] == 1
        assert plugin.store.get_known_relation_ids("movie", 1) == {10, 11}
        assert plugin.store.changed_content_ids("movie", {1: {10, 11}}) == []

    with_plugin(run)


def test_a_failed_series_records_its_relations_too():
    def run(plugin):
        enqueue_all(plugin, "series", [7])

        def process_one(series_id, *_args):
            raise RuntimeError("fetch failed")

        plugin._process_one_series = process_one
        with fake_django({7: {70, 71}}):
            plugin._process_series_batch(SETTINGS)
        assert plugin.store.queue_counts("series")["error"] == 1
        assert plugin.store.get_known_relation_ids("series", 7) == {70, 71}

    with_plugin(run)


def test_a_failing_relation_lookup_does_not_abort_the_batch():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2])

        def process_one(movie_id, *_args):
            raise RuntimeError("boom")

        plugin._process_one_movie = process_one
        with fake_django({}, fail_lookups=True):
            result = plugin._process_batch(SETTINGS)
        assert result["processed"] == 2 and result["errors"] == 2
        assert plugin.store.queue_counts("movie")["in_progress"] == 0

    with_plugin(run)


def test_the_breaker_sends_unstarted_titles_back_to_the_queue():
    def run(plugin):
        enqueue_all(plugin, "movie", range(1, 21))

        def process_one(movie_id, *_args):
            time.sleep(0.05)
            raise RuntimeError("provider down")

        plugin._process_one_movie = process_one
        with fake_django({}):
            result = plugin._process_batch(SETTINGS)
        counts = plugin.store.queue_counts("movie")
        assert plugin.store.is_paused("movie")
        assert counts["in_progress"] == 0
        # Whatever a thread had started finished as an error; the rest is back in the queue.
        assert counts["error"] == result["processed"] and counts["pending"] > 0
        assert counts["error"] + counts["pending"] == 20
        assert counts["error"] < 20

    with_plugin(run)


def test_the_series_breaker_releases_unstarted_titles_as_well():
    def run(plugin):
        enqueue_all(plugin, "series", range(1, 21))

        def process_one(series_id, *_args):
            time.sleep(0.05)
            raise RuntimeError("provider down")

        plugin._process_one_series = process_one
        with fake_django({}):
            plugin._process_series_batch(SETTINGS)
        counts = plugin.store.queue_counts("series")
        assert plugin.store.is_paused("series")
        assert counts["in_progress"] == 0 and counts["pending"] > 0
        assert counts["error"] + counts["pending"] == 20

    with_plugin(run)


def test_each_finished_title_reports_progress():
    def run(plugin):
        enqueue_all(plugin, "movie", [1, 2, 3])
        plugin._process_one_movie = lambda movie_id, *_args: 0
        ticks = []
        with fake_django({}):
            plugin._process_batch(SETTINGS, on_progress=lambda: ticks.append(1))
        assert len(ticks) == 3

    with_plugin(run)
