import importlib.util
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")


def load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "vod_manager_pkg", os.path.join(ROOT, "__init__.py"), submodule_search_locations=[ROOT]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules["vod_manager_pkg"] = package
    spec.loader.exec_module(package)
    return importlib.import_module("vod_manager_pkg.plugin")


plugin_module = load_plugin_module()


def with_plugin(fn):
    tmp = tempfile.mkdtemp()
    os.environ["VOD_MANAGER_DATA_DIR"] = tmp
    # Django is not installed here: the two pieces that need it are stubbed.
    original = plugin_module._release_db_connections
    plugin_module._release_db_connections = lambda: None
    try:
        plugin = plugin_module.Plugin()
        plugin._catalog_stats = lambda _settings: None
        fn(plugin)
    finally:
        plugin_module._release_db_connections = original
        os.environ.pop("VOD_MANAGER_DATA_DIR", None)
        shutil.rmtree(tmp)


def message(text="ok"):
    return {"status": "ok", "message": text}


def run_pipeline(plugin, process, settings=None, scheduled=False, generate=None):
    calls = []

    def scan(_settings):
        calls.append("scan")
        return message("scanned")

    return calls, plugin._run_pipeline(
        "movie", "movies", settings or {"dry_run": False}, scheduled,
        lambda _s: message("cleaned"), scan, process, generate or (lambda _s: message("generated")),
    )


def test_pipeline_processes_batches_until_the_queue_is_empty():
    def run(plugin):
        batches = [
            {"status": "ok", "message": "b1", "claimed": 30, "processed": 30, "errors": 2, "pruned": 5},
            {"status": "ok", "message": "b2", "claimed": 10, "processed": 10, "errors": 0, "pruned": 1},
            {"status": "ok", "message": "Nothing queued. Run Scan Movies first.", "queue_empty": True},
        ]
        calls, result = run_pipeline(plugin, lambda _s, _progress: batches.pop(0))
        assert batches == []
        assert calls == ["scan"]
        assert "Processed 40 movies (2 errors), pruned 6." in result["message"]
        assert "Nothing queued" not in result["message"]
        # The lock is released so the next click can start.
        assert plugin.store.lock_held_since("scan_and_process_movie") is None

    with_plugin(run)


def test_pipeline_reports_why_it_stopped_when_paused():
    def run(plugin):
        batches = [
            {"status": "ok", "message": "b1", "claimed": 5, "processed": 5, "errors": 5, "pruned": 0},
            {"status": "ok", "message": "Queue is paused — resume it to process."},
        ]
        _, result = run_pipeline(plugin, lambda _s, _progress: batches.pop(0))
        assert "Queue is paused" in result["message"]

    with_plugin(run)


def test_pipeline_recovers_titles_left_in_progress_by_a_restart():
    def run(plugin):
        plugin.store.enqueue("movie", 1)
        assert plugin.store.claim_batch("movie", 5) == [1]
        seen = {}

        def process(_s, _progress):
            seen["counts"] = plugin.store.queue_counts("movie")
            return {"status": "ok", "message": "Nothing queued."}

        _, result = run_pipeline(plugin, process)
        assert seen["counts"]["pending"] == 1 and seen["counts"]["in_progress"] == 0
        assert "Recovered 1 movies" in result["message"]

    with_plugin(run)


def test_pipeline_refuses_to_start_while_another_run_is_live():
    def run(plugin):
        plugin.store.try_acquire_lock("scan_and_process_movie")
        calls, result = run_pipeline(plugin, lambda _s, _progress: {"status": "ok", "message": "x"})
        assert result["status"] == "error" and "already running" in result["message"]
        assert calls == []

    with_plugin(run)


def test_pipeline_generates_only_when_scheduled():
    def run(plugin):
        generated = []

        def generate(_s):
            generated.append(1)
            return message("generated")

        settings = {"dry_run": False, "auto_generate_strm": True}
        idle = lambda _s, _progress: {"status": "ok", "message": "Nothing queued.", "queue_empty": True}  # noqa: E731
        run_pipeline(plugin, idle, settings, scheduled=False, generate=generate)
        assert generated == []
        run_pipeline(plugin, idle, settings, scheduled=True, generate=generate)
        assert generated == [1]

    with_plugin(run)


def test_pipeline_lets_a_batch_renew_the_lock_after_each_title():
    def run(plugin):
        lock = "scan_and_process_movie"
        seen = {}

        def process(_s, progress):
            first = plugin.store.lock_held_since(lock)
            time.sleep(0.02)
            progress()
            seen["renewed"] = plugin.store.lock_held_since(lock) > first
            return {"status": "ok", "message": "Nothing queued."}

        run_pipeline(plugin, process)
        assert seen["renewed"]

    with_plugin(run)


def test_pipeline_tells_how_many_titles_are_left_in_error():
    def run(plugin):
        plugin.store.enqueue("movie", 1)
        plugin.store.claim_batch("movie", 1)
        plugin.store.mark_error("movie", 1, "boom")
        idle = lambda _s, _progress: {"status": "ok", "message": "Nothing queued.", "queue_empty": True}  # noqa: E731
        _, result = run_pipeline(plugin, idle)
        assert "1 movies in error" in result["message"]
        assert "Retry Errored Titles" in result["message"]

    with_plugin(run)


def test_busy_message_says_started_unless_the_lock_is_renewed():
    def run(plugin):
        started = plugin._busy_lock_message("Movie .strm generation", time.time() - 30)
        assert "started 30s ago" in started["message"] and "after 60 minutes." in started["message"]
        renewed = plugin._busy_lock_message("Scan + Process", time.time() - 30, 900, renewed=True)
        assert "last activity 30s ago" in renewed["message"]
        assert "after 15 minutes without activity" in renewed["message"]

    with_plugin(run)


FULL_STRM_SETTINGS = {"strm_dispatcharr_url": "http://x:9191", "strm_library_path": "/data/strm"}


def test_generate_is_refused_at_click_time_without_its_settings():
    def run(plugin):
        result = plugin.run("generate_movie_strm", {}, {"settings": {}})
        assert result["status"] == "error" and "Set both" in result["message"]

    with_plugin(run)


def test_generate_is_refused_at_click_time_while_the_queue_is_not_drained():
    def run(plugin):
        plugin.store.enqueue("series", 1)
        result = plugin.run("generate_series_strm", {}, {"settings": FULL_STRM_SETTINGS})
        assert result["status"] == "error"
        assert "1 series pending" in result["message"] and "Series Queue Status" in result["message"]

    with_plugin(run)


def test_generate_click_is_refused_while_a_generation_is_running():
    def run(plugin):
        plugin.store.try_acquire_lock("generate_movie_strm")
        result = plugin.run("generate_movie_strm", {}, {"settings": FULL_STRM_SETTINGS})
        assert result["status"] == "error" and "already running" in result["message"]

    with_plugin(run)


def test_generate_in_the_background_runs_it_and_reports_the_outcome():
    def run(plugin):
        notified = {}
        plugin._generate_movie_strm = lambda _s: {"status": "ok", "message": "Created 3 files."}
        plugin._notify_run_finished = lambda unit, message, stopped, logger, title=None: notified.update(
            unit=unit, message=message, stopped=stopped, title=title
        )
        result = plugin.run("generate_movie_strm", {}, {"settings": FULL_STRM_SETTINGS, "background": True})
        assert result["message"] == "Created 3 files."
        assert notified["message"] == "Created 3 files." and not notified["stopped"]
        assert "generated" in notified["title"]

    with_plugin(run)


def test_pipeline_goes_on_after_a_batch_made_only_of_waiting_titles():
    def run(plugin):
        batches = [
            {"status": "ok", "message": "w", "claimed": 5, "processed": 0, "errors": 0, "pruned": 0, "waiting": 5},
            {"status": "ok", "message": "b", "claimed": 3, "processed": 3, "errors": 0, "pruned": 2, "waiting": 0},
            {"status": "ok", "message": "Nothing queued.", "queue_empty": True},
        ]
        _, result = run_pipeline(plugin, lambda _s, _progress: batches.pop(0))
        assert batches == []
        assert "Processed 3 movies (0 errors), pruned 2." in result["message"]

    with_plugin(run)


def test_pipeline_gives_waiting_titles_another_chance_and_reports_the_ones_still_waiting():
    def run(plugin):
        plugin.store.enqueue("movie", 1)
        plugin.store.claim_batch("movie", 1)
        plugin.store.mark_waiting("movie", 1)
        seen = {}

        def process(_s, _progress):
            if "counts" not in seen:
                seen["counts"] = plugin.store.queue_counts("movie")
                plugin.store.claim_batch("movie", 10)
                plugin.store.mark_waiting("movie", 1)
                return {"status": "ok", "message": "w", "claimed": 1, "processed": 0, "errors": 0,
                        "pruned": 0, "waiting": 1}
            return {"status": "ok", "message": "Nothing queued.", "queue_empty": True}

        _, result = run_pipeline(plugin, process)
        assert seen["counts"]["pending"] == 1 and seen["counts"]["waiting"] == 0
        assert "1 movies waiting for vod-probe" in result["message"]

    with_plugin(run)
