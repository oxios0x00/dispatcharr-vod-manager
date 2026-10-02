"""Reset Plugin State and Delete .strm Files, both of which must respect
Dry Run like every other destructive action (Clean Titles, Generate,
Prune Orphaned State already did before this file existed)."""
import os

from test_pipeline import with_plugin


def test_reset_plugin_state_dry_run_changes_nothing():
    def check(plugin):
        plugin.store.set_known_relation_ids("movie", 1, {10, 11})
        before = plugin.store.table_row_counts()
        assert sum(before.values()) > 0

        result = plugin._reset_plugin_state({"dry_run": True})

        assert plugin.store.table_row_counts() == before
        assert "Would reset" in result["message"]
        assert "known_relations" in result["message"]

    with_plugin(check)


def test_reset_plugin_state_for_real_clears_everything():
    def check(plugin):
        plugin.store.set_known_relation_ids("movie", 1, {10, 11})

        result = plugin._reset_plugin_state({"dry_run": False})

        assert sum(plugin.store.table_row_counts().values()) == 0
        assert "Plugin state reset" in result["message"]

    with_plugin(check)


def test_reset_plugin_state_reports_nothing_to_do_when_already_empty():
    def check(plugin):
        result = plugin._reset_plugin_state({"dry_run": False})
        assert result["message"] == "Nothing to reset — plugin state is already empty."

    with_plugin(check)


def _make_library(tmp_path_root):
    movies = os.path.join(tmp_path_root, "movies")
    series = os.path.join(tmp_path_root, "series")
    os.makedirs(movies)
    os.makedirs(series)
    with open(os.path.join(movies, "Some Movie.strm"), "w") as f:
        f.write("http://example/stream")
    with open(os.path.join(series, "Some Series.strm"), "w") as f:
        f.write("http://example/stream")
    return {
        "strm_library_path": tmp_path_root,
        "strm_movies_subfolder": "movies",
        "strm_series_subfolder": "series",
    }


def test_delete_strm_files_dry_run_leaves_files_and_manifest_alone(tmp_path):
    def check(plugin):
        settings = _make_library(str(tmp_path))
        plugin.store.save_strm_manifest("movie", ["movies/Some Movie.strm"])
        settings["dry_run"] = True

        result = plugin._delete_strm_files(settings)

        assert os.path.exists(os.path.join(str(tmp_path), "movies", "Some Movie.strm"))
        assert plugin.store.get_strm_manifest("movie") == {"movies/Some Movie.strm"}
        assert "Would clear" in result["message"]

    with_plugin(check)


def test_delete_strm_files_for_real_deletes_and_clears_manifest(tmp_path):
    def check(plugin):
        settings = _make_library(str(tmp_path))
        plugin.store.save_strm_manifest("movie", ["movies/Some Movie.strm"])
        settings["dry_run"] = False

        result = plugin._delete_strm_files(settings)

        assert not os.path.exists(os.path.join(str(tmp_path), "movies", "Some Movie.strm"))
        assert os.path.isdir(os.path.join(str(tmp_path), "movies"))  # the subfolder itself survives
        assert plugin.store.get_strm_manifest("movie") == set()
        assert "Cleared" in result["message"]

    with_plugin(check)


def test_scheduled_run_settings_fill_in_manifest_defaults_without_overriding_stored():
    from test_pipeline import plugin_module
    import importlib
    schedule = importlib.import_module("vod_manager_pkg.schedule")
    fields = [{"id": "a", "default": 1}, {"id": "b", "default": 2}, {"id": "no_default"}]
    merged = schedule.merge_with_defaults({"a": 9}, fields)
    assert merged == {"a": 9, "b": 2}
    assert schedule.merge_with_defaults(None, fields) == {"a": 1, "b": 2}


def test_scheduled_run_falls_back_to_the_snapshot_when_settings_cannot_be_read():
    from test_pipeline import with_plugin

    def check(plugin):
        # Django is not installed here, so the stored settings are unreachable.
        assert plugin._live_settings({"x": 1}) == {"x": 1}
        assert plugin._live_settings(None) == {}

    with_plugin(check)
