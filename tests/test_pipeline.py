import importlib.util
import os
import shutil
import sys
import tempfile

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
            {"status": "ok", "message": "b1", "processed": 30, "errors": 2, "pruned": 5},
            {"status": "ok", "message": "b2", "processed": 10, "errors": 0, "pruned": 1},
            {"status": "ok", "message": "Nothing queued. Run Scan Movies first."},
        ]
        calls, result = run_pipeline(plugin, lambda _s: batches.pop(0))
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
            {"status": "ok", "message": "b1", "processed": 5, "errors": 5, "pruned": 0},
            {"status": "ok", "message": "Queue is paused — resume it to process."},
        ]
        _, result = run_pipeline(plugin, lambda _s: batches.pop(0))
        assert "Queue is paused" in result["message"]

    with_plugin(run)


def test_pipeline_recovers_titles_left_in_progress_by_a_restart():
    def run(plugin):
        plugin.store.enqueue("movie", 1)
        assert plugin.store.claim_batch("movie", 5) == [1]
        seen = {}

        def process(_s):
            seen["counts"] = plugin.store.queue_counts("movie")
            return {"status": "ok", "message": "Nothing queued."}

        _, result = run_pipeline(plugin, process)
        assert seen["counts"]["pending"] == 1 and seen["counts"]["in_progress"] == 0
        assert "Recovered 1 movies" in result["message"]

    with_plugin(run)


def test_pipeline_refuses_to_start_while_another_run_is_live():
    def run(plugin):
        plugin.store.try_acquire_lock("scan_and_process_movie")
        calls, result = run_pipeline(plugin, lambda _s: {"status": "ok", "message": "x"})
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
        idle = lambda _s: {"status": "ok", "message": "Nothing queued."}  # noqa: E731
        run_pipeline(plugin, idle, settings, scheduled=False, generate=generate)
        assert generated == []
        run_pipeline(plugin, idle, settings, scheduled=True, generate=generate)
        assert generated == [1]

    with_plugin(run)
