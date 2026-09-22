"""VOD Manager — automatic quality/language curation for Dispatcharr's VOD
catalogue.

Reads the quality, languages and bitrate that the vod-probe plugin measured
for each M3UMovieRelation/M3UEpisodeRelation, picks the winning relation(s)
per the target_qualities/target_languages algorithm, and prunes the losing
relations from Dispatcharr's own database. Optionally also generates .strm files pinned directly to each
kept relation (see strm.py), for media servers that can't get real
multi-version playback through Dispatcharr's native Xtream API alone.

Every design decision referenced in comments below was validated against
a real Dispatcharr instance and the real Dispatcharr source.
"""
import json
import os
import time

CONTENT_TYPE_MOVIE = "movie"
CONTENT_TYPE_SERIES = "series"
CONTENT_TYPE_EPISODE = "episode"

# Sibling submodules (.measurements/.selection/.store) are imported lazily inside
# methods below, not at module top level — matches the defensive pattern
# used by other Dispatcharr plugins in this ecosystem (see e.g.
# iptv_checker/plugin.py's `from . import notify_report, reports` done
# inside function bodies) to avoid stale references across a plugin
# reload cycle.


def _release_db_connections():
    """Hands this thread's database connection back to Dispatcharr's pool. A
    Scan + Process runs for a long time in one Celery thread; closing its
    connection between batches keeps it from pinning one meanwhile."""
    from django.db import connections

    connections.close_all()


def _parse_csv_list(value):
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


class _WaitingForMeasurements(Exception):
    """A title has a relation vod-probe has not measured yet."""


class Plugin:
    name = "VOD Manager"
    version = "2.4.2"
    description = (
        "Curates Dispatcharr's VOD catalogue from vod-probe's measurements: keeps the versions matching "
        "your quality/language settings and prunes the rest. Optional .strm generation for Emby/Jellyfin. "
        "Needs vod-probe. Dry-run by default."
    )
    author = "oxios0x00"
    help_url = ""

    # Names for the django-celery-beat PeriodicTask and the Celery task it
    # points at — same pattern as the real, published vod2mlib plugin. No
    # formal plugin scheduling API exists in Dispatcharr; a plugin
    # registers its own PeriodicTask directly.
    SCHEDULE_TASK_NAME = "vod_manager.auto_run"
    SCHEDULED_TASK_CELERY_NAME = "vod_manager.scheduled_run"

    fields = [
        {
            "id": "dry_run",
            "label": "Dry run (log decisions, don't delete anything)",
            "type": "boolean",
            "default": True,
            "help_text": (
                "ON: decides winners but never deletes anything. "
                "Turn OFF once a test batch looks right — pruning is a real "
                "delete, only recoverable via Dispatcharr's own next refresh."
            ),
        },
        {
            "id": "target_qualities",
            "label": "Qualities to keep (one winner per tier)",
            "type": "string",
            "default": "2160p,1080p",
            "help_text": "Comma-separated, best first. Empty = every tier. Falls back to the best available tier if none listed exist. Details: docs/settings.md.",
        },
        {
            "id": "exclude_unmatched_quality",
            "label": "Exclude titles with none of the target qualities",
            "type": "boolean",
            "default": False,
            "help_text": "OFF: keeps the best available tier instead. ON: drops the title entirely.",
        },
        {
            "id": "keep_one_version_per_tier",
            "label": "Keep one version per quality tier",
            "type": "boolean",
            "default": False,
            "help_text": "OFF (default): keep every matching version per tier. ON: one winner per tier (old, more aggressive behaviour). Details: docs/settings.md.",
        },
        {
            "id": "target_languages",
            "label": "Target languages (ISO 639-2, e.g. fre,eng)",
            "type": "string",
            "default": "fre,eng",
            "help_text": "Comma-separated. Empty = language is ignored.",
        },
        {
            "id": "exclude_unmatched_language",
            "label": "Exclude relations matching none of the target languages",
            "type": "boolean",
            "default": False,
            "help_text": "OFF: keeps the best-bitrate relation anyway. ON: drops that tier entirely.",
        },
        {
            "id": "_section_exclusions",
            "label": "[EXCLUSIONS]",
            "type": "info",
            "description": "Permanently exclude specific titles by TMDB id, regardless of the settings above. Details: docs/settings.md.",
        },
        {
            "id": "excluded_movie_tmdbids",
            "label": "Movies to exclude (one TMDB id per line)",
            "type": "text",
            "default": "",
            "help_text": "One TMDB id per line, optional free comment after it (e.g. '603 wrong match').",
        },
        {
            "id": "excluded_series_tmdbids",
            "label": "Series to exclude (one TMDB id per line)",
            "type": "text",
            "default": "",
            "help_text": "Same format, matched against the series' own TMDB id.",
        },
        {
            "id": "batch_size",
            "label": "Batch size (movies per batch)",
            "type": "number",
            "default": 25,
            "min": 1,
            "help_text": "Start small (5-25) to validate before scaling up.",
        },
        {
            "id": "_section_series",
            "label": "[SERIES]",
            "type": "info",
            "description": "Same select/prune pipeline as Films, per episode, using the settings above. Details: docs/concepts.md.",
        },
        {
            "id": "series_batch_size",
            "label": "Batch size (series per batch)",
            "type": "number",
            "default": 5,
            "min": 1,
            "help_text": "Smaller than the Films batch size — one series can fan out into dozens of episodes.",
        },
        {
            "id": "_section_title_cleanup",
            "label": "[TITLE CLEANUP]",
            "type": "info",
            "description": "Cosmetic only — strips junk prefixes from titles (e.g. 'NF - The Matrix' → 'The Matrix'). Never affects quality/language selection.",
        },
        {
            "id": "title_cleanup_tags",
            "label": "Tags to strip (one per line)",
            "type": "text",
            "default": (
                "NF -\nTOP -\n4K-FR -\nAMZ -\nFR -\nFR .\nD+ -\nD+  -\nUNV -\n4K-NF -\n"
                "PRMT -\n4K-AMZ -\n4K-D+ -\n4K-FR-HDR -\n4K-FR-\nA+ -\nA+\n"
                "4K-A+ -\n4K-MRVL -\nDWA -\n007 -\nK-FR -\nAR-SUBS -\n4M-AMZ -\n4K-"
            ),
            "help_text": "Case-insensitive prefixes, stripped from the start of the title (stacked tags are stripped repeatedly).",
        },
        {
            "id": "auto_clean_titles",
            "label": "Run title cleanup automatically with Scan + Process",
            "type": "boolean",
            "default": False,
            "help_text": "OFF (default): only runs from the Clean Titles buttons or its own scheduled action.",
        },
        {
            "id": "_section_strm",
            "label": "[.STRM OUTPUT]",
            "type": "info",
            "description": "Optional: writes one .strm per kept relation, for Emby/Jellyfin multi-version playback. Run order: Scan + Process until Queue Status is empty, then Generate. Details: docs/strm-and-emby.md.",
        },
        {
            "id": "strm_dispatcharr_url",
            "label": "Dispatcharr base URL (baked into every .strm)",
            "type": "string",
            "default": "",
            "placeholder": "http://192.168.1.94:9292",
            "help_text": "Must be reachable from your media server, not just from Dispatcharr — a LAN IP/hostname, not 'localhost'.",
        },
        {
            "id": "strm_library_path",
            "label": "Library root path (inside this container)",
            "type": "string",
            "default": "/data/strm",
            "help_text": "Mount this same path into your media server so it can see the generated files.",
        },
        {
            "id": "strm_movies_subfolder",
            "label": "Movies subfolder name",
            "type": "string",
            "default": "movies",
        },
        {
            "id": "strm_series_subfolder",
            "label": "Series subfolder name",
            "type": "string",
            "default": "series",
        },
        {
            "id": "auto_generate_strm",
            "label": "Run .strm generation automatically with Scan + Process",
            "type": "boolean",
            "default": False,
            "help_text": "OFF (default): only runs from the Generate buttons or its own scheduled action.",
        },
        {
            "id": "strm_include_id_tag",
            "label": "Include [tmdbid-####] / [imdbid-ttXXXXXXX] in the movie/series folder name",
            "type": "boolean",
            "default": True,
            "help_text": "ON (default): tags the folder and file names with the title's TMDB/IMDB id, for Emby/Jellyfin identification and Jellyfin's multi-version grouping. Flipping this renames the whole tagged tree on the next Generate. Details: docs/strm-and-emby.md.",
        },
        {
            "id": "strm_require_id",
            "label": "Skip titles with no TMDB/IMDB id",
            "type": "boolean",
            "default": True,
            "help_text": "ON (default): no .strm for a title with neither id (some providers never expose one). OFF: it still gets one, named from the raw title text.",
        },
        {
            "id": "_section_schedule",
            "label": "[SCHEDULE]",
            "type": "info",
            "description": "Runs one action on its own cron. Fill in the fields below, click Apply, then restart Dispatcharr once. Leave Schedule empty until you trust the picks. Details: docs/scheduling.md.",
        },
        {
            "id": "schedule_cron",
            "label": "Schedule (5-field cron)",
            "type": "string",
            "default": "",
            "help_text": "'minute hour day-of-month month day-of-week'. Empty falls back to every 6 hours when Apply is clicked. Best set a few minutes after Dispatcharr's VOD refresh and vod-probe.",
        },
        {
            "id": "schedule_timezone",
            "label": "Schedule timezone",
            "type": "string",
            "default": "",
            "placeholder": "Europe/Paris",
            "help_text": "IANA timezone name. Leave empty to use UTC.",
        },
        {
            "id": "schedule_target",
            "label": "Scheduled action",
            "type": "select",
            "default": "scan_and_process",
            "options": [
                {"value": "scan_and_process", "label": "Scan + Process Movies (recommended)"},
                {"value": "scan_movies", "label": "Scan Movies only"},
                {"value": "clean_movie_titles", "label": "Clean Movie Titles only"},
                {"value": "clean_series_titles", "label": "Clean Series Titles only"},
                {"value": "scan_and_process_series", "label": "Scan + Process Series (recommended)"},
                {"value": "scan_series", "label": "Scan Series only"},
                {"value": "generate_movie_strm", "label": "Generate Movie .strm Files only"},
                {"value": "generate_series_strm", "label": "Generate Series .strm Files only"},
            ],
            "help_text": "Which action the scheduler runs on each tick.",
        },
    ]

    actions = [
        {
            "id": "scan_and_process",
            "label": "[MOVIES] Scan + Process",
            "description": "Background run: scans, then selects and prunes until the queue is empty. Follow with Queue Status, end with Stop.",
            "button_label": "Run",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "scan_movies",
            "label": "[MOVIES] Scan",
            "description": "Enqueue movies whose M3U relations changed since last pass.",
            "button_label": "Scan",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "queue_status",
            "label": "[MOVIES] Queue Status",
            "description": "Whether a run is in progress, pending/in-progress/done/error counts and the last batch.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "stop_queue",
            "label": "[MOVIES] Stop",
            "description": "Stops a running movie Scan + Process after its current batch. Nothing is left blocked: run Scan + Process again to carry on.",
            "button_label": "Stop",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "clean_movie_titles",
            "label": "[MOVIES] Clean Titles",
            "description": "Strip configured junk prefixes from movie titles (uses Dry Run).",
            "button_label": "Clean Titles",
            "button_variant": "outline",
            "button_color": "grape",
        },
        {
            "id": "generate_movie_strm",
            "label": "[MOVIES] Generate .strm Files",
            "description": "Write one .strm per kept movie relation. Refuses to run until Scan + Process is done. Runs in the background; a notification appears when finished.",
            "button_label": "Generate",
            "button_variant": "outline",
            "button_color": "cyan",
        },
        {
            "id": "scan_and_process_series",
            "label": "[SERIES] Scan + Process",
            "description": "Background run: scans, then selects and prunes until the queue is empty. Follow with Queue Status, end with Stop.",
            "button_label": "Run",
            "button_variant": "filled",
            "button_color": "green",
        },
        {
            "id": "scan_series",
            "label": "[SERIES] Scan",
            "description": "Enqueue series whose M3U relations changed since last pass.",
            "button_label": "Scan",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "series_queue_status",
            "label": "[SERIES] Queue Status",
            "description": "Whether a run is in progress, pending/in-progress/done/error counts and the last batch, for series.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "stop_series_queue",
            "label": "[SERIES] Stop",
            "description": "Stops a running series Scan + Process after its current batch. Nothing is left blocked: run Scan + Process again to carry on.",
            "button_label": "Stop",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "clean_series_titles",
            "label": "[SERIES] Clean Titles",
            "description": "Strip configured junk prefixes from series titles (uses Dry Run).",
            "button_label": "Clean Titles",
            "button_variant": "outline",
            "button_color": "grape",
        },
        {
            "id": "generate_series_strm",
            "label": "[SERIES] Generate .strm Files",
            "description": "Write one .strm per kept episode relation. Refuses to run until Scan + Process is done. Runs in the background; a notification appears when finished.",
            "button_label": "Generate",
            "button_variant": "outline",
            "button_color": "cyan",
        },
        {
            "id": "catalog_stats",
            "label": "[MAINTENANCE] Catalog Stats",
            "description": "Snapshot kept movies/series by quality tier. Also runs automatically after Scan + Process.",
            "button_label": "Stats",
            "button_variant": "outline",
            "button_color": "grape",
        },
        {
            "id": "delete_strm_files",
            "label": "[MAINTENANCE] Delete .strm Files",
            "description": "Deletes every generated .strm (movies and series) and clears the manifest. Real files, real delete.",
            "button_label": "Delete",
            "button_variant": "outline",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Delete every .strm file?",
                "message": "Removes all files under the configured Movies/Series .strm subfolders. Your media server will lose them until the next Generate. This does not touch Dispatcharr's own catalogue.",
            },
        },
        {
            "id": "prune_orphaned_state",
            "label": "[MAINTENANCE] Prune Orphaned State",
            "description": "Deletes this plugin's own queue rows for titles Dispatcharr has since deleted. Uses Dry Run. Never touches Dispatcharr's data or .strm files.",
            "button_label": "Prune",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "retry_errored_titles",
            "label": "[MAINTENANCE] Retry Errored Titles",
            "description": "Re-queues every title in 'error' status, movies and series. Follow with Scan + Process.",
            "button_label": "Retry",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "reset_plugin_state",
            "label": "[MAINTENANCE] Reset Plugin State",
            "description": "Wipes the queues, known relations, run history and catalog stats — starts fresh on the next Scan/Process.",
            "button_label": "Reset",
            "button_variant": "outline",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Reset all plugin state?",
                "message": "Clears every queue this plugin has recorded — the next Scan + Process will decide every title again. Dispatcharr's own movies/series/relations are untouched.",
            },
        },
        {
            "id": "apply_schedule",
            "label": "[SCHEDULE] Apply",
            "description": "Register or update the periodic task from the [SCHEDULE] settings.",
            "button_label": "Apply",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "remove_schedule",
            "label": "[SCHEDULE] Remove",
            "description": "Unregister the periodic task.",
            "button_label": "Remove",
            "button_variant": "outline",
            "button_color": "orange",
            "confirm": {
                "required": True,
                "title": "Remove the schedule?",
                "message": "Unregisters the periodic task. You can re-create it any time with Apply Schedule.",
            },
        },
        {
            "id": "schedule_status",
            "label": "[SCHEDULE] Status",
            "description": "Whether a schedule is registered and when it last ran.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
    ]

    def __init__(self):
        from .store import Store

        data_dir = os.environ.get(
            "VOD_MANAGER_DATA_DIR",
            os.path.join(os.path.dirname(__file__), "data"),
        )
        self.store = Store(data_dir)

    # --- dispatch ---------------------------------------------------------

    def run(self, action_id, params, context):
        settings = context.get("settings", {})
        scheduled = bool(context.get("scheduled"))
        if action_id in self._BACKGROUND_ACTIONS and not context.get("background"):
            return self._start_background(action_id, settings, scheduled)
        if action_id in self._GENERATE_ACTIONS:
            if not context.get("background"):
                return self._start_generate(action_id, settings)
            return self._run_generate_in_background(action_id, settings)
        if action_id == "clean_movie_titles":
            return self._clean_movie_titles(settings)
        if action_id == "clean_series_titles":
            return self._clean_series_titles(settings)
        if action_id == "scan_movies":
            return self._scan_movies(settings)
        if action_id == "scan_and_process":
            return self._scan_and_process(settings)
        if action_id == "queue_status":
            return self._queue_status(CONTENT_TYPE_MOVIE)
        if action_id == "stop_queue":
            return self._request_stop(CONTENT_TYPE_MOVIE)
        if action_id == "scan_series":
            return self._scan_series(settings)
        if action_id == "scan_and_process_series":
            return self._scan_and_process_series(settings)
        if action_id == "series_queue_status":
            return self._queue_status(CONTENT_TYPE_SERIES)
        if action_id == "catalog_stats":
            return self._catalog_stats(settings)
        if action_id == "delete_strm_files":
            return self._delete_strm_files(settings)
        if action_id == "reset_plugin_state":
            return self._reset_plugin_state(settings)
        if action_id == "prune_orphaned_state":
            return self._prune_orphaned_state(settings)
        if action_id == "retry_errored_titles":
            return self._retry_errored_titles()
        if action_id == "stop_series_queue":
            return self._request_stop(CONTENT_TYPE_SERIES)
        if action_id == "apply_schedule":
            return self._apply_schedule(settings)
        if action_id == "remove_schedule":
            return self._remove_schedule()
        if action_id == "schedule_status":
            return self._schedule_status()
        return {"status": "error", "message": f"Unknown action '{action_id}'"}

    def _request_stop(self, content_type):
        if not self.store.lock_held_since(
            self._pipeline_lock(content_type), self._PIPELINE_LOCK_STALE_SECONDS
        ):
            return {"status": "ok", "message": "Nothing is running."}
        self.store.request_stop(content_type)
        return {
            "status": "ok",
            "message": "Stop requested: the run ends after its current batch. Run Scan + Process again to carry on.",
        }

    def _busy_lock_message(self, human_name, held_since, stale_after=3600, renewed=False):
        """A re-click or an automatic retry found the same action still
        running — without this, both would run to completion in parallel and
        pin a worker each for the whole batch, which is what made the whole
        UI look frozen during the 2026-09-18 production incident. held_since
        comes from Store.try_acquire_lock or lock_held_since; a lock left by
        a run that died clears itself after stale_after seconds."""
        elapsed = int(time.time() - held_since) if held_since else 0
        minutes = max(1, stale_after // 60)
        # Only a run that renews its lock has a "last activity"; for the others
        # the lock's time is when the action started.
        since = "last activity" if renewed else "started"
        clears = f"{minutes} minutes without activity" if renewed else f"{minutes} minutes"
        return {
            "status": "error",
            "message": (
                f"{human_name} is already running ({since} {elapsed}s ago) — wait for it to "
                "finish, or check Queue Status for progress, before starting another. If "
                f"Dispatcharr restarted while one was running, this clears itself automatically "
                f"after {clears}."
            ),
        }

    # --- background runs -----------------------------------------------------
    #
    # Scan + Process runs in a Celery worker, not in the request that clicked
    # it: a whole catalogue takes longer than the browser (about a minute
    # here) or nginx (300 s) will wait, so a synchronous click ended
    # in a 504 while the work carried on unseen. The click now only queues the
    # task; Queue Status shows how far it got and Stop ends it after the
    # current batch.

    _BACKGROUND_ACTIONS = {
        "scan_and_process": CONTENT_TYPE_MOVIE,
        "scan_and_process_series": CONTENT_TYPE_SERIES,
    }
    # A run renews its lock between batches; a lock that has been silent this
    # long belongs to a run that died with its worker.
    _PIPELINE_LOCK_STALE_SECONDS = 900

    @staticmethod
    def _pipeline_lock(content_type):
        return f"scan_and_process_{content_type}"

    def _enqueue_background(self, action, settings, scheduled):
        """Queue `action` on the Celery worker. Returns (task_id, None), or
        (None, error_result) when it cannot be queued."""
        task_fn = globals().get("_vod_manager_scheduled_run")
        if task_fn is None:
            return None, {
                "status": "error",
                "message": "Background task failed to register at plugin load — check server logs.",
            }
        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}
        try:
            async_result = task_fn.apply_async(
                kwargs={"action": action, "settings": snapshot, "scheduled": scheduled}, queue="dvr"
            )
        except Exception as e:
            return None, {"status": "error", "message": f"Failed to queue the background run: {e}"}
        return async_result.id, None

    _GENERATE_ACTIONS = {
        "generate_movie_strm": (CONTENT_TYPE_MOVIE, "Movie .strm generation", "movie .strm files"),
        "generate_series_strm": (CONTENT_TYPE_SERIES, "Series .strm generation", "series .strm files"),
    }

    def _generate_blocker(self, content_type, settings):
        """Why a .strm generation cannot start now, or None. Checked when the
        button is clicked, so the refusal shows at once instead of in a
        background task nobody is watching."""
        if not (settings.get("strm_dispatcharr_url") or "").strip() or not (
            settings.get("strm_library_path") or ""
        ).strip():
            return {
                "status": "error",
                "message": "Set both 'Dispatcharr base URL' and 'Library root path' in [.STRM OUTPUT] first.",
            }
        queue = self.store.queue_counts(content_type)
        if queue["pending"] or queue["in_progress"] or queue["waiting"]:
            queue_name = "Queue Status" if content_type == CONTENT_TYPE_MOVIE else "Series Queue Status"
            return {
                "status": "error",
                "message": (
                    f"{queue['pending']} {'movie(s)' if content_type == CONTENT_TYPE_MOVIE else 'series'} pending, "
                    f"{queue['in_progress']} in progress, {queue['waiting']} waiting for vod-probe — "
                    f"finish Scan + Process first ({queue_name} should read 0 pending, 0 in "
                    "progress and 0 waiting). Generating now would give still-unmeasured titles a '- unprobed' "
                    "filename and write a file for a relation that's about to be pruned, only for "
                    "it to disappear on the next Generate run."
                ),
            }
        return None

    def _start_generate(self, action_id, settings):
        content_type, human_name, _ = self._GENERATE_ACTIONS[action_id]
        blocker = self._generate_blocker(content_type, settings)
        if blocker:
            return blocker
        held_since = self.store.lock_held_since(action_id)
        if held_since:
            return self._busy_lock_message(human_name, held_since)
        _, error = self._enqueue_background(action_id, settings, scheduled=False)
        if error:
            return error
        return {
            "status": "ok",
            "message": (
                f"{human_name} started in the background; a notification appears when it is done."
            ),
        }

    def _run_generate_in_background(self, action_id, settings):
        """Run a generation inside the Celery task and post its outcome."""
        import logging

        logger = logging.getLogger("vod_manager.generate")
        run = self._generate_movie_strm if action_id == "generate_movie_strm" else self._generate_series_strm
        result = run(settings)
        _, _, label = self._GENERATE_ACTIONS[action_id]
        logger.info("%s finished: %s", label, result.get("message"))
        self._notify_run_finished(
            label.replace(" ", "-"), result.get("message", ""), stopped=result.get("status") == "error",
            logger=logger, title=f"VOD Manager: {label} " + ("failed" if result.get("status") == "error" else "generated"),
        )
        return result

    def _start_background(self, action_id, settings, scheduled):
        content_type = self._BACKGROUND_ACTIONS[action_id]
        held_since = self.store.lock_held_since(
            self._pipeline_lock(content_type), self._PIPELINE_LOCK_STALE_SECONDS
        )
        if held_since:
            return self._busy_lock_message(
                "Scan + Process", held_since, self._PIPELINE_LOCK_STALE_SECONDS, renewed=True
            )
        _, error = self._enqueue_background(action_id, settings, scheduled)
        if error:
            return error
        return {
            "status": "ok",
            "message": (
                "Scan + Process started in the background. It runs until the queue is empty. "
                "Click Queue Status to follow it, Stop to end it after the current batch."
            ),
        }

    def _run_pipeline(self, content_type, unit, settings, clean, scan, process, generate):
        """Scan, then process batches until the queue is empty, a stop is
        requested or a batch cannot start."""
        import logging

        logger = logging.getLogger("vod_manager.pipeline")
        lock = self._pipeline_lock(content_type)
        acquired, held_since = self.store.try_acquire_lock(
            lock, stale_after=self._PIPELINE_LOCK_STALE_SECONDS
        )
        if not acquired:
            return self._busy_lock_message(
                "Scan + Process", held_since, self._PIPELINE_LOCK_STALE_SECONDS, renewed=True
            )
        try:
            parts = []
            # Holding the lock means no other run is working this queue, so
            # anything still in progress was cut short by a restart.
            recovered = self.store.requeue_in_progress(content_type)
            if recovered:
                parts.append(f"Recovered {recovered} {unit} left in progress by an interrupted run.")
            # Titles that were waiting for vod-probe get another chance now.
            self.store.requeue_waiting(content_type)
            # A stop asked for while nothing ran must not cancel this run.
            self.store.clear_stop(content_type)
            if settings.get("auto_clean_titles"):
                parts.append(clean(settings).get("message", ""))
            parts.append(scan(settings).get("message", ""))

            totals = {"processed": 0, "errors": 0, "pruned": 0}
            stop_message = ""
            queue_empty = False
            stopped = False
            while True:
                if self.store.stop_requested(content_type):
                    self.store.clear_stop(content_type)
                    stopped = True
                    break
                self.store.renew_lock(lock)
                _release_db_connections()
                # A batch can outlast the lock's staleness window, so every
                # finished title renews it too.
                result = process(settings, lambda: self.store.renew_lock(lock))
                if not result.get("claimed"):
                    stop_message = result.get("message", "")
                    queue_empty = bool(result.get("queue_empty"))
                    break
                for key in totals:
                    totals[key] += result[key]

            dry_run = bool(settings.get("dry_run", True))
            parts.append(
                f"Processed {totals['processed']} {unit} ({totals['errors']} errors), "
                f"{'would prune' if dry_run else 'pruned'} {totals['pruned']}."
            )
            if stopped:
                parts.append("Stopped on request; run Scan + Process again to carry on.")
            elif stop_message and not queue_empty:
                parts.append(stop_message)
            counts = self.store.queue_counts(content_type)
            if counts["waiting"]:
                parts.append(
                    f"{counts['waiting']} {unit} waiting for vod-probe to measure them: check that "
                    "vod-probe is installed and has run, then run Scan + Process again."
                )
            errored = counts["error"]
            if errored:
                parts.append(
                    f"{errored} {unit} in error, left alone until their relations change — "
                    "[MAINTENANCE] Retry Errored Titles puts them back in the queue."
                )
            if settings.get("auto_generate_strm"):
                parts.append(generate(settings).get("message", ""))
            self._catalog_stats(settings)
            message = " | ".join(part for part in parts if part)
            logger.info("Scan + Process (%s) finished: %s", content_type, message)
            self._notify_run_finished(
                unit, message, stopped=stopped, logger=logger
            )
            return {"status": "ok", "message": message}
        finally:
            self.store.release_lock(lock)

    _NOTIFICATION_KEY_PREFIX = "vod-manager-run-"

    def _notify_run_finished(self, unit, message, stopped, logger, title=None):
        """Show the outcome in Dispatcharr's notification centre (a toast for
        whoever is connected, then an entry in the bell). Only the latest per
        content type is kept: a dismissed notification stays dismissed per user, so reusing
        one key would hide every later run from anyone who had closed it."""
        try:
            from core.models import SystemNotification
            from core.utils import send_websocket_notification

            SystemNotification.objects.filter(
                notification_key__startswith=f"{self._NOTIFICATION_KEY_PREFIX}{unit}-"
            ).delete()
            kind = SystemNotification.NotificationType
            notification = SystemNotification.objects.create(
                notification_key=f"{self._NOTIFICATION_KEY_PREFIX}{unit}-{int(time.time())}",
                notification_type=kind.WARNING if stopped else kind.INFO,
                priority=SystemNotification.Priority.HIGH,
                title=title or f"VOD Manager: {unit} {'stopped' if stopped else 'done'}",
                message=message,
                is_active=True,
                admin_only=True,
            )
            send_websocket_notification(notification)
        except Exception as exc:  # noqa: BLE001 - a notification must never fail the run
            logger.warning("Could not send the completion notification: %s", exc)

    def _scan_and_process(self, settings):
        return self._run_pipeline(
            CONTENT_TYPE_MOVIE, "movies", settings,
            self._clean_movie_titles, self._scan_movies,
            lambda settings, progress: self._process_batch(CONTENT_TYPE_MOVIE, settings, progress),
            self._generate_movie_strm,
        )

    # --- title cleanup (cosmetic, independent of selection) -----------------

    def _clean_movie_titles(self, settings):
        from apps.vod.models import Movie

        return self._clean_titles_for_model(Movie, settings, "movie", "movies")

    def _clean_series_titles(self, settings):
        from apps.vod.models import Series

        return self._clean_titles_for_model(Series, settings, "series", "series")

    def _clean_titles_for_model(self, model_cls, settings, noun_singular, noun_plural):
        from .title_cleanup import parse_tag_list, strip_title_tags

        tags = parse_tag_list(settings.get("title_cleanup_tags"))
        dry_run = bool(settings.get("dry_run", True))
        if not tags:
            return {"status": "ok", "message": "No tags configured — nothing to do."}

        changed = 0
        skipped_no_id = 0
        examples = []
        fields = ("id", "name", "custom_properties", "tmdb_id", "imdb_id")
        for obj in model_cls.objects.all().only(*fields).iterator():
            cleaned = strip_title_tags(obj.name, tags)
            if cleaned == obj.name:
                continue
            if not obj.tmdb_id and not obj.imdb_id:
                # Dispatcharr's own refresh matches ID-less movies/series by
                # exact (name, year) against the raw name the provider still
                # sends (see apps/vod/tasks.py lookup_by_name_year) —
                # renaming would make that match fail and create a
                # duplicate on the next provider scan. Leave these untouched.
                skipped_no_id += 1
                continue
            changed += 1
            if len(examples) < 3:
                examples.append(f"'{obj.name}' -> '{cleaned}'")
            if not dry_run:
                # Keep the provider's original name so this is reversible —
                # renaming is a real write to Dispatcharr's own data, not a
                # plugin-local decision.
                props = obj.custom_properties or {}
                props.setdefault("vod_manager_original_name", obj.name)
                obj.custom_properties = props
                obj.name = cleaned
                obj.save(update_fields=["name", "custom_properties"])

        verb = "would rename" if dry_run else "renamed"
        noun = noun_singular if changed == 1 else noun_plural
        msg = f"{verb} {changed} {noun}."
        if skipped_no_id:
            msg += f" Skipped {skipped_no_id} with no TMDB/IMDB id (rename would break Dispatcharr's own dedup on next provider scan)."
        if examples:
            msg += " e.g. " + "; ".join(examples)
        return {"status": "ok", "message": msg}

    # --- scan / enqueue -----------------------------------------------------

    def _enqueue_changed(self, content_type, relation_model, id_field):
        """Requeue every title that is new or whose relation ids differ from
        the last pass. The relations are read in one query and compared in
        memory: a query per title made the scan outlast the browser timeout.
        requeue(), not enqueue(): enqueue() is a silent no-op for a title
        already processed, which is exactly the case a changed relation set
        must reset."""
        current = {}
        for content_id, relation_id in relation_model.objects.filter(
            m3u_account__is_active=True
        ).values_list(id_field, "id"):
            current.setdefault(content_id, set()).add(relation_id)
        changed = self.store.changed_content_ids(content_type, current)
        for content_id in changed:
            self.store.requeue(content_type, content_id)
        return len(current), len(changed)

    def _scan_movies(self, settings):
        from apps.vod.models import Movie, M3UMovieRelation

        scanned, enqueued = self._enqueue_changed(
            CONTENT_TYPE_MOVIE, M3UMovieRelation, "movie_id"
        )

        return {
            "status": "ok",
            "message": f"Scanned {scanned} movies, enqueued {enqueued} new/changed.",
        }

    # --- process ------------------------------------------------------------
    #
    # This plugin no longer measures anything. vod-probe writes each relation's
    # quality, languages and bitrate into its custom_properties; a title is
    # decided only once every one of its relations has an answer, so a relation
    # nobody has looked at yet is never mistaken for a loser. A title that is
    # not ready waits and is tried again at the next run.

    def _remember_failed_relations(self, content_type, content_id, relation_model, id_field):
        """Record the relation ids a title failed with. They are otherwise
        only recorded on success, so a title that always fails looked new on
        every Scan, was queued again at the head of the next batch and failed
        again. Scan now requeues it only if its relations change."""
        try:
            current = set(
                relation_model.objects.filter(
                    m3u_account__is_active=True, **{id_field: content_id}
                ).values_list("id", flat=True)
            )
            self.store.set_known_relation_ids(content_type, content_id, current)
        except Exception as exc:  # noqa: BLE001 - the title is already marked as failed
            import logging

            logging.getLogger("vod_manager.pipeline").warning(
                "Could not record the relations of failed %s %s: %s", content_type, content_id, exc
            )

    def _process_batch(self, content_type, settings, on_progress=None):
        from apps.vod.models import M3UMovieRelation, M3USeriesRelation

        is_movie = content_type == CONTENT_TYPE_MOVIE
        lock = f"process_{content_type}_batch"
        acquired, held_since = self.store.try_acquire_lock(lock)
        if not acquired:
            return self._busy_lock_message("A movie batch" if is_movie else "A series batch", held_since)
        try:
            from .exclusions import parse_excluded_ids

            target_qualities = _parse_csv_list(settings.get("target_qualities"))
            target_languages = _parse_csv_list(settings.get("target_languages"))
            exclude_unmatched_language = bool(settings.get("exclude_unmatched_language", False))
            exclude_unmatched_quality = bool(settings.get("exclude_unmatched_quality", False))
            keep_one_per_tier = bool(settings.get("keep_one_version_per_tier", False))
            excluded_tmdbids = parse_excluded_ids(
                settings.get("excluded_movie_tmdbids") if is_movie else settings.get("excluded_series_tmdbids")
            )
            dry_run = bool(settings.get("dry_run", True))
            batch_size = int(settings.get("batch_size", 25) or 25) if is_movie \
                else int(settings.get("series_batch_size", 5) or 5)

            content_ids = self.store.claim_batch(content_type, batch_size)
            if not content_ids:
                return {
                    "status": "ok",
                    "message": "Nothing queued. Run Scan Movies first." if is_movie
                    else "Nothing queued. Run Scan Series first.",
                    "queue_empty": True,
                }

            run_id = self.store.start_run(content_type, dry_run)
            relation_model, id_field = (
                (M3UMovieRelation, "movie_id") if is_movie else (M3USeriesRelation, "series_id")
            )
            process_one = self._process_one_movie if is_movie else self._process_one_series
            errors = pruned_total = processed = waiting = 0

            for content_id in content_ids:
                try:
                    pruned_total += process_one(
                        content_id, target_qualities, target_languages,
                        exclude_unmatched_language, exclude_unmatched_quality, keep_one_per_tier,
                        excluded_tmdbids, dry_run,
                    )
                    self.store.mark_done(content_type, content_id)
                    processed += 1
                except _WaitingForMeasurements:
                    self.store.mark_waiting(content_type, content_id)
                    waiting += 1
                except Exception as exc:  # noqa: BLE001 - surfaced via mark_error
                    errors += 1
                    processed += 1
                    self.store.mark_error(content_type, content_id, exc)
                    self._remember_failed_relations(content_type, content_id, relation_model, id_field)
                if on_progress:
                    on_progress()

            note = f"{waiting} waiting for vod-probe" if waiting else ""
            self.store.finish_run(run_id, processed, errors, pruned_total, note=note)
            unit = "" if is_movie else " series"
            what = "relations" if is_movie else "episode relation(s)"
            msg = (
                f"Processed {processed}{unit} ({errors} errors), "
                f"{'would prune' if dry_run else 'pruned'} {pruned_total} {what}."
            )
            if waiting:
                msg += f" {waiting} waiting for vod-probe to measure them."
            return {
                "status": "ok", "message": msg, "claimed": len(content_ids),
                "processed": processed, "errors": errors, "pruned": pruned_total, "waiting": waiting,
            }
        finally:
            self.store.release_lock(lock)

    def _process_one_movie(
        self, movie_id, target_qualities, target_languages, exclude_unmatched_language,
        exclude_unmatched_quality, keep_one_per_tier, excluded_tmdbids, dry_run,
    ):
        """Select the winning relation(s) of one movie from vod-probe's
        measurements and, unless dry_run, prune the losers. A movie whose
        TMDB id is in excluded_tmdbids skips measurement and selection
        entirely: it is pruned to nothing, like a title with zero winners.
        Returns the number of relations pruned (or that would be)."""
        from apps.vod.models import M3UMovieRelation
        from . import measurements
        from .selection import Candidate, select_winners

        relations = list(
            M3UMovieRelation.objects.filter(movie_id=movie_id, m3u_account__is_active=True)
            .select_related("movie")
        )
        if not relations:
            self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, set())
            return 0

        if excluded_tmdbids and str(relations[0].movie.tmdb_id) in excluded_tmdbids:
            winner_ids = set()
        else:
            states = [measurements.state(r.custom_properties) for r in relations]
            if measurements.MISSING in states:
                raise _WaitingForMeasurements()
            candidates = [
                Candidate(
                    r.id, measurements.languages(r.custom_properties), measurements.tier(r.custom_properties),
                    bitrate=measurements.bitrate(r.custom_properties),
                )
                for r, state in zip(relations, states) if state == measurements.MEASURED
            ]
            if not candidates:
                raise RuntimeError(f"movie {movie_id}: no relation could be measured")

            winners = select_winners(
                candidates, target_languages, target_qualities, exclude_unmatched_language, exclude_unmatched_quality,
                keep_one_per_tier,
            )
            winner_ids = {c.relation_id for c in winners}

        all_ids = {r.id for r in relations}
        loser_ids = all_ids - winner_ids

        if dry_run:
            self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, all_ids)
            return len(loser_ids)

        if loser_ids:
            M3UMovieRelation.objects.filter(id__in=loser_ids).delete()
        self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, winner_ids)
        return len(loser_ids)

    # --- series: scan / enqueue ------------------------------------------
    #
    # Mirrors _scan_movies exactly, one level up: this only tracks each
    # series' set of M3USeriesRelation ids (its provider "sources"), not
    # its episodes. Episodes aren't loaded yet at scan time — Dispatcharr
    # only fetches a series' episode list on demand (apps/vod/tasks.py,
    # refresh_series_episodes: "only called on-demand"), so there is
    # nothing episode-level to compare here. A known limitation this
    # implies: a provider adding new episodes to an *already-processed*
    # series, with no change to its M3USeriesRelation set, won't be
    # detected by this scan alone — only _process_one_series (which always
    # re-fetches when a relation isn't yet marked episodes_fetched, and
    # re-derives the episode list every time it runs) can catch that, and
    # only for series that get reprocessed. Documented rather than solved
    # here.

    def _scan_series(self, settings):
        from apps.vod.models import Series, M3USeriesRelation

        scanned, enqueued = self._enqueue_changed(
            CONTENT_TYPE_SERIES, M3USeriesRelation, "series_id"
        )

        return {
            "status": "ok",
            "message": f"Scanned {scanned} series, enqueued {enqueued} new/changed.",
        }

    def _scan_and_process_series(self, settings):
        return self._run_pipeline(
            CONTENT_TYPE_SERIES, "series", settings,
            self._clean_series_titles, self._scan_series,
            lambda settings, progress: self._process_batch(CONTENT_TYPE_SERIES, settings, progress),
            self._generate_series_strm,
        )

    # --- series: process ---------------------------------------------------

    def _process_one_series(
        self, series_id, target_qualities, target_languages, exclude_unmatched_language,
        exclude_unmatched_quality, keep_one_per_tier, excluded_tmdbids, dry_run,
    ):
        """Select the winning relation(s) of every episode of one series from
        vod-probe's measurements and, unless dry_run, prune the losers. The
        whole series is checked before anything is deleted: an episode nobody
        has measured yet (or whose result Dispatcharr erased by reloading the
        series) makes the series wait. A series whose TMDB id is in
        excluded_tmdbids skips measurement and selection entirely: every
        episode relation is pruned, like every episode having zero winners.
        Returns the number of episode relations pruned (or that would be)."""
        from apps.vod.models import M3USeriesRelation, M3UEpisodeRelation
        from . import measurements
        from .selection import Candidate, select_winners

        series_relations = list(
            M3USeriesRelation.objects.filter(series_id=series_id, m3u_account__is_active=True)
            .select_related("series")
        )
        if not series_relations:
            self.store.set_known_relation_ids(CONTENT_TYPE_SERIES, series_id, set())
            return 0

        excluded = bool(excluded_tmdbids) and str(series_relations[0].series.tmdb_id) in excluded_tmdbids

        if not excluded and not all(measurements.series_ready(r.custom_properties) for r in series_relations):
            raise _WaitingForMeasurements()

        by_episode = {}
        for relation in M3UEpisodeRelation.objects.filter(
            episode__series_id=series_id, m3u_account__is_active=True
        ):
            by_episode.setdefault(relation.episode_id, []).append(relation)

        if excluded:
            loser_ids = {r.id for relations in by_episode.values() for r in relations}
        else:
            if not by_episode:
                raise _WaitingForMeasurements()

            decisions, without_candidates = [], 0
            for ep_relations in by_episode.values():
                states = [measurements.state(r.custom_properties) for r in ep_relations]
                if measurements.MISSING in states:
                    raise _WaitingForMeasurements()
                candidates = [
                    Candidate(
                        r.id, measurements.languages(r.custom_properties), measurements.tier(r.custom_properties),
                        bitrate=measurements.bitrate(r.custom_properties),
                    )
                    for r, state in zip(ep_relations, states) if state == measurements.MEASURED
                ]
                if not candidates:
                    without_candidates += 1
                    continue
                winners = select_winners(
                    candidates, target_languages, target_qualities,
                    exclude_unmatched_language, exclude_unmatched_quality, keep_one_per_tier,
                )
                winner_ids = {c.relation_id for c in winners}
                decisions.append({r.id for r in ep_relations} - winner_ids)

            if without_candidates == len(by_episode):
                raise RuntimeError(f"series {series_id}: no episode relation could be measured")

            loser_ids = set().union(*decisions) if decisions else set()

        if loser_ids and not dry_run:
            M3UEpisodeRelation.objects.filter(id__in=loser_ids).delete()

        self.store.set_known_relation_ids(
            CONTENT_TYPE_SERIES, series_id, {r.id for r in series_relations}
        )
        return len(loser_ids)

    # --- status ---------------------------------------------------------

    def _queue_status(self, content_type):
        counts = self.store.queue_counts(content_type)
        last = self.store.last_run(content_type)
        running_since = self.store.lock_held_since(
            self._pipeline_lock(content_type), self._PIPELINE_LOCK_STALE_SECONDS
        )
        running = (
            f"[RUNNING, last activity {int(time.time() - running_since)}s ago] " if running_since else ""
        )
        msg = (
            f"{running}pending={counts['pending']} in_progress={counts['in_progress']} waiting={counts['waiting']} "
            f"done={counts['done']} error={counts['error']}"
        )
        if last:
            mode = "dry-run" if last["dry_run"] else "live"
            msg += (
                f" | last run ({mode}): processed={last['processed']} "
                f"errors={last['errors']} pruned={last['pruned_relations']}"
            )
        return {"status": "ok", "message": msg}

    # --- catalog stats (quality/language composition over time) ---------

    def _catalog_stats(self, settings):
        """Snapshot of the *currently kept* (post-prune) catalogue, broken
        down by quality tier — how many movies/series are actually 4K vs
        1080p etc. right now, not what the provider's category names
        claim. Recorded to catalog_stats every time this runs (manually,
        or automatically at the end of Scan + Process), so composition can
        be compared over time rather than only seen as a one-off report."""
        from apps.vod.models import M3UMovieRelation, M3UEpisodeRelation
        from . import measurements

        movie_rows = list(
            M3UMovieRelation.objects.filter(m3u_account__is_active=True).values_list(
                "id", "movie_id", "custom_properties__probe__status", "custom_properties__probe__tier"
            )
        )
        episode_rows = list(
            M3UEpisodeRelation.objects.filter(m3u_account__is_active=True).values_list(
                "id", "episode__series_id", "custom_properties__probe__status", "custom_properties__probe__tier"
            )
        )
        movie_relation_ids = [(rid, title_id) for rid, title_id, _, _ in movie_rows]
        episode_relation_ids = [(rid, title_id) for rid, title_id, _, _ in episode_rows]
        movie_quality_by_relation = {
            rid: measurements.usable_tier(status, tier) for rid, _, status, tier in movie_rows
        }
        episode_quality_by_relation = {
            rid: measurements.usable_tier(status, tier) for rid, _, status, tier in episode_rows
        }

        def _aggregate(pairs, quality_by_relation):
            # quality_label -> {"titles": set(title_id), "relations": count}
            buckets = {}
            for relation_id, title_id in pairs:
                label = quality_by_relation.get(relation_id) or "unprobed"
                bucket = buckets.setdefault(label, {"titles": set(), "relations": 0})
                bucket["titles"].add(title_id)
                bucket["relations"] += 1
            return buckets

        movie_buckets = _aggregate(movie_relation_ids, movie_quality_by_relation)
        series_buckets = _aggregate(episode_relation_ids, episode_quality_by_relation)

        snapshot_rows = []
        for label, b in movie_buckets.items():
            snapshot_rows.append({
                "content_type": "movie", "quality_label": label,
                "title_count": len(b["titles"]), "relation_count": b["relations"],
            })
        for label, b in series_buckets.items():
            snapshot_rows.append({
                "content_type": "series", "quality_label": label,
                "title_count": len(b["titles"]), "relation_count": b["relations"],
            })
        self.store.save_catalog_stats_snapshot(snapshot_rows)

        def _format(buckets, noun):
            total_titles = len(set().union(*[b["titles"] for b in buckets.values()])) if buckets else 0
            parts = [f"{noun}: {total_titles} total"]
            for label in ("2160p", "1080p", "720p", "480p", "sd", "unknown", "unprobed"):
                if label in buckets:
                    parts.append(f"{label}={len(buckets[label]['titles'])}")
            return " ".join(parts)

        msg = _format(movie_buckets, "Movies") + " | " + _format(series_buckets, "Series")
        return {"status": "ok", "message": msg}

    # --- .strm generation ---------------------------------------------------
    #
    # Bypasses Dispatcharr's native Xtream API entirely (it always collapses
    # a title's kept relations to whichever M3U account has the highest
    # priority, regardless of category — verified with real ffprobe testing
    # against the raw provider stream). Each .strm is pinned to one exact relation via
    # Dispatcharr's generic proxy endpoint instead, so Emby/Jellyfin (or any
    # media server that reads .strm files) get every kept quality tier as a
    # genuinely distinct, correctly-labelled, independently playable file.
    # This does not help pure Xtream clients (TiviMate, etc.) — that path is
    # a Dispatcharr core limitation, not something fixable from here.

    def _generate_movie_strm(self, settings):
        acquired, held_since = self.store.try_acquire_lock("generate_movie_strm")
        if not acquired:
            return self._busy_lock_message("Movie .strm generation", held_since)
        try:
            from apps.vod.models import M3UMovieRelation
            from . import measurements
            from .strm import best_quality_first, build_proxy_url, id_tag, plan_suffixes, remove_stale_files, sanitize_filename, write_strm_if_changed

            blocker = self._generate_blocker(CONTENT_TYPE_MOVIE, settings)
            if blocker:
                return blocker
            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            subfolder = (settings.get("strm_movies_subfolder") or "movies").strip() or "movies"
            library_dir = os.path.join(library_root, subfolder)
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))

            relations = list(
                M3UMovieRelation.objects.filter(m3u_account__is_active=True)
                .select_related("movie")
                .order_by("movie_id", "id")
            )
            quality_by_relation = {r.id: measurements.tier(r.custom_properties) for r in relations}

            by_movie = {}
            for rel in relations:
                by_movie.setdefault(rel.movie_id, []).append(rel)

            created = updated = unchanged = errors = skipped_no_id = 0
            current_paths = set()
            for movie_relations in by_movie.values():
                movie = movie_relations[0].movie
                if require_id and not movie.tmdb_id and not movie.imdb_id:
                    # Not written this run, and therefore absent from
                    # current_paths below — an already-generated file for
                    # this movie gets cleaned up by the stale-file pass just
                    # like a pruned relation would be.
                    skipped_no_id += 1
                    continue
                tag = id_tag(movie.tmdb_id, movie.imdb_id) if include_id_tag else ""
                folder_name = sanitize_filename(movie.name + tag)
                movie_dir = os.path.join(library_dir, folder_name)
                suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in movie_relations])

                for rel, suffix in best_quality_first(movie_relations, suffixes):
                    # Jellyfin only groups several files as versions of one
                    # movie when each file name starts character-for-character
                    # with the folder name, tag included — so the file name
                    # must repeat exactly what the folder is named, not just
                    # the plain title.
                    path = os.path.join(movie_dir, f"{folder_name}{suffix}.strm")
                    url = build_proxy_url(base_url, "movie", str(movie.uuid), rel.stream_id)
                    try:
                        result = write_strm_if_changed(path, url)
                    except OSError:
                        errors += 1
                        continue
                    current_paths.add(path)
                    if result == "created":
                        created += 1
                    elif result == "updated":
                        updated += 1
                    else:
                        unchanged += 1

            # Anything this plugin wrote last time but didn't write again just
            # now belongs to a relation that's been pruned, a movie that's
            # gone entirely, or a movie now skipped by "Skip titles with no
            # id" — safe to remove precisely because we tracked writing it
            # ourselves, unlike scanning the folder for "any .strm".
            stale = self.store.get_strm_manifest(CONTENT_TYPE_MOVIE) - current_paths
            removed = remove_stale_files(stale, stop_dir=library_dir)
            self.store.save_strm_manifest(CONTENT_TYPE_MOVIE, current_paths)

            msg = (
                f"{created} created, {updated} updated, {unchanged} unchanged, "
                f"{removed} removed across {len(by_movie)} movies."
            )
            if skipped_no_id:
                msg += f" {skipped_no_id} movie(s) skipped (no TMDB/IMDB id)."
            if errors:
                msg += f" {errors} file write error(s) — check the path is writable."
            return {"status": "ok", "message": msg}
        finally:
            self.store.release_lock("generate_movie_strm")

    def _generate_series_strm(self, settings):
        acquired, held_since = self.store.try_acquire_lock("generate_series_strm")
        if not acquired:
            return self._busy_lock_message("Series .strm generation", held_since)
        try:
            from apps.vod.models import M3UEpisodeRelation

            from . import measurements
            from .strm import best_quality_first, build_proxy_url, id_tag, plan_suffixes, remove_stale_files, sanitize_filename, write_strm_if_changed

            blocker = self._generate_blocker(CONTENT_TYPE_SERIES, settings)
            if blocker:
                return blocker
            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            subfolder = (settings.get("strm_series_subfolder") or "series").strip() or "series"
            library_dir = os.path.join(library_root, subfolder)
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))

            relations = list(
                M3UEpisodeRelation.objects.filter(m3u_account__is_active=True)
                .select_related("episode", "episode__series")
                .order_by("episode_id", "id")
            )
            quality_by_relation = {r.id: measurements.tier(r.custom_properties) for r in relations}

            by_episode = {}
            for rel in relations:
                by_episode.setdefault(rel.episode_id, []).append(rel)

            created = updated = unchanged = errors = 0
            current_paths = set()
            series_seen = set()
            series_skipped_no_id = set()
            for episode_relations in by_episode.values():
                episode = episode_relations[0].episode
                series = episode.series
                if require_id and not series.tmdb_id and not series.imdb_id:
                    # Same reasoning as the movie side: not written this run,
                    # so an already-generated episode file for this series
                    # gets cleaned up by the stale-file pass below.
                    series_skipped_no_id.add(series.id)
                    continue
                series_seen.add(series.id)

                # Jellyfin 12.0+ groups episode versions by season/episode
                # number, not by this tag (unlike movies, see Limitations) —
                # repeated here only for naming consistency with movies.
                tag = id_tag(series.tmdb_id, series.imdb_id) if include_id_tag else ""
                series_folder_name = sanitize_filename(series.name + tag)
                series_dir = os.path.join(library_dir, series_folder_name)
                season_num = episode.season_number or 1
                episode_num = episode.episode_number or 0
                base_filename = f"{series_folder_name} - S{season_num:02d}E{episode_num:02d}"
                season_dir = os.path.join(series_dir, f"Season {season_num:02d}")

                suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in episode_relations])

                for rel, suffix in best_quality_first(episode_relations, suffixes):
                    path = os.path.join(season_dir, f"{base_filename}{suffix}.strm")
                    url = build_proxy_url(base_url, "episode", str(episode.uuid), rel.stream_id)
                    try:
                        result = write_strm_if_changed(path, url)
                    except OSError:
                        errors += 1
                        continue
                    current_paths.add(path)
                    if result == "created":
                        created += 1
                    elif result == "updated":
                        updated += 1
                    else:
                        unchanged += 1

            stale = self.store.get_strm_manifest(CONTENT_TYPE_EPISODE) - current_paths
            removed = remove_stale_files(stale, stop_dir=library_dir)
            self.store.save_strm_manifest(CONTENT_TYPE_EPISODE, current_paths)

            msg = (
                f"{created} created, {updated} updated, {unchanged} unchanged, "
                f"{removed} removed across {len(series_seen)} series."
            )
            if series_skipped_no_id:
                msg += f" {len(series_skipped_no_id)} series skipped (no TMDB/IMDB id)."
            if errors:
                msg += f" {errors} file write error(s) — check the path is writable."
            return {"status": "ok", "message": msg}
        finally:
            self.store.release_lock("generate_series_strm")

    def _delete_strm_files(self, settings):
        """Deletes every file and folder inside the configured Movies/Series
        .strm subfolders (the subfolders themselves are kept, in case a
        media server has them mounted as its library root), and clears the
        .strm manifest so the next Generate starts from a clean slate."""
        import shutil

        library_root = (settings.get("strm_library_path") or "").strip()
        if not library_root:
            return {"status": "error", "message": "Set 'Library root path' in [.STRM OUTPUT] first."}
        movies_subfolder = (settings.get("strm_movies_subfolder") or "movies").strip() or "movies"
        series_subfolder = (settings.get("strm_series_subfolder") or "series").strip() or "series"

        cleared = []
        for subfolder in (movies_subfolder, series_subfolder):
            target = os.path.join(library_root, subfolder)
            if not os.path.isdir(target):
                continue
            for entry in os.listdir(target):
                entry_path = os.path.join(target, entry)
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path)
                else:
                    os.remove(entry_path)
            cleared.append(target)

        self.store.save_strm_manifest(CONTENT_TYPE_MOVIE, [])
        self.store.save_strm_manifest(CONTENT_TYPE_EPISODE, [])

        if not cleared:
            return {"status": "ok", "message": "Nothing to delete — no .strm folders found at the configured path."}
        return {"status": "ok", "message": f"Cleared: {', '.join(cleared)}."}

    def _prune_orphaned_state(self, settings):
        """Deletes the plugin's own rows that point at things Dispatcharr no
        longer has. Dispatcharr's cleanup removes relations, and the movies
        and series left with none, when a provider group is disabled or a
        catalogue is re-imported — but nothing removes the queue, known-
        relations rows keyed on the deleted ids, so they pile up. A content type Dispatcharr holds none of is skipped: an empty
        table is more likely a re-import in progress than a real wipe, and
        every stored row would look orphaned."""
        from apps.vod.models import Movie, Series

        in_progress = sum(
            self.store.queue_counts(t)["in_progress"] for t in (CONTENT_TYPE_MOVIE, CONTENT_TYPE_SERIES)
        )
        if in_progress:
            return {
                "status": "error",
                "message": f"{in_progress} title(s) are in progress — wait for the batch to finish first.",
            }

        dry_run = bool(settings.get("dry_run", True))
        title_sets = (
            ("movies", CONTENT_TYPE_MOVIE, Movie),
            ("series", CONTENT_TYPE_SERIES, Series),
        )
        parts, skipped = [], []

        for label, content_type, model in title_sets:
            live = set(model.objects.values_list("id", flat=True))
            stored = self.store.stored_content_ids(content_type)
            if stored and not live:
                skipped.append(label)
                continue
            dead = stored - live
            if dead and not dry_run:
                queue, known = self.store.delete_content_rows(content_type, dead)
                parts.append(f"{len(dead)} {label} ({queue} queue + {known} known-relations rows)")
            elif dead:
                parts.append(f"{len(dead)} {label}")

        verb = "Would remove" if dry_run else "Removed"
        msg = f"{verb} " + ", ".join(parts) + "." if parts else "Nothing to prune — no orphaned rows."
        if skipped:
            msg += f" Skipped (Dispatcharr currently has none): {', '.join(skipped)}."
        if dry_run and parts:
            msg += " Turn Dry Run off to delete them."
        return {"status": "ok", "message": msg}

    def _retry_errored_titles(self):
        movies = self.store.requeue_errors(CONTENT_TYPE_MOVIE)
        series = self.store.requeue_errors(CONTENT_TYPE_SERIES)
        return {
            "status": "ok",
            "message": (
                f"Re-queued {movies} movie(s) and {series} series that had failed"
                f"{' — run Scan + Process to retry them.' if movies or series else '.'}"
            ),
        }

    def _reset_plugin_state(self, settings):
        """Wipes this plugin's own sidecar state (queues, known
        relation sets, stop requests, run history, catalog stats, .strm
        tracking) so the next Scan/Process starts completely from scratch.
        Never touches Dispatcharr's own database or any real .strm file —
        pair with Delete .strm Files if you also want those gone."""
        self.store.reset_all()
        return {
            "status": "ok",
            "message": (
                "Plugin state reset: queues, known relations, "
                "run history, catalog stats and .strm tracking all cleared. "
                "Dispatcharr's own catalogue is untouched."
            ),
        }

    # --- scheduling (django-celery-beat) --------------------------------
    #
    # No formal plugin scheduling API exists in Dispatcharr (verified in
    # apps/plugins/loader.py — no schedule-related method). Dispatcharr
    # itself runs on Celery + django-celery-beat internally (M3U/EPG
    # refresh, backups), so a plugin can register its own PeriodicTask
    # directly. Same pattern as the real, published vod2mlib plugin.
    # Settings are snapshotted into the PeriodicTask's
    # kwargs at Apply time, not read live — re-click Apply after changing
    # any other setting to refresh what the scheduled run actually uses.

    _VALID_SCHEDULE_TARGETS = (
        "scan_and_process",
        "scan_movies",
        "clean_movie_titles",
        "clean_series_titles",
        "scan_and_process_series",
        "scan_series",
        "generate_movie_strm",
        "generate_series_strm",
    )

    def _parse_cron(self, cron_expr):
        fields = (cron_expr or "").split()
        if len(fields) != 5:
            raise ValueError(
                f"Cron expression must have exactly 5 fields (minute hour dom month dow), got: {cron_expr!r}"
            )
        return fields  # minute, hour, day_of_month, month_of_year, day_of_week

    def _apply_schedule(self, settings):
        cron_expr = settings.get("schedule_cron") or "0 */6 * * *"
        target = settings.get("schedule_target") or "scan_and_process"
        tz_str = (settings.get("schedule_timezone") or "").strip() or "UTC"

        if target not in self._VALID_SCHEDULE_TARGETS:
            return {"status": "error", "message": f"Invalid schedule_target: {target}"}

        try:
            minute, hour, dom, month, dow = self._parse_cron(cron_expr)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        try:
            import pytz

            if tz_str not in pytz.all_timezones_set:
                return {"status": "error", "message": f"Unknown timezone: {tz_str}"}
        except ImportError:
            pass  # pytz not available: skip the friendly check, let CrontabSchedule validate

        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
        except ImportError as e:
            return {
                "status": "error",
                "message": f"django-celery-beat not available ({e}); scheduling requires it.",
            }

        import json

        try:
            schedule, _ = CrontabSchedule.objects.get_or_create(
                minute=minute,
                hour=hour,
                day_of_month=dom,
                month_of_year=month,
                day_of_week=dow,
                timezone=tz_str,
            )
        except Exception as e:
            return {"status": "error", "message": f"Invalid cron expression: {e}"}

        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}

        task, created = PeriodicTask.objects.update_or_create(
            name=self.SCHEDULE_TASK_NAME,
            defaults={
                "crontab": schedule,
                "task": self.SCHEDULED_TASK_CELERY_NAME,
                "queue": "dvr",  # the one queue Dispatcharr reserves for long-running background work
                "kwargs": json.dumps({"action": target, "settings": snapshot}),
                "enabled": True,
                "description": f"Scheduled run for {self.name} v{self.version}",
            },
        )

        verb = "Created" if created else "Updated"
        return {
            "status": "ok",
            "message": f"{verb} schedule: '{cron_expr}' ({tz_str}) -> {target}.",
        }

    def _remove_schedule(self):
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed; nothing to remove."}

        deleted, _ = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).delete()
        return {"status": "ok", "message": f"Removed {deleted} scheduled task(s)."}

    def _schedule_status(self):
        try:
            from django_celery_beat.models import PeriodicTask
        except ImportError:
            return {"status": "ok", "message": "django-celery-beat not installed."}

        task = PeriodicTask.objects.filter(name=self.SCHEDULE_TASK_NAME).first()
        if not task:
            return {"status": "ok", "message": "No schedule registered."}

        crontab = str(task.crontab) if task.crontab else "?"
        last_run = task.last_run_at.isoformat() if task.last_run_at else "never"
        try:
            action = json.loads(task.kwargs or "{}").get("action")
        except ValueError:
            action = None
        invalid = (
            f" | INVALID target '{action}' — click Apply with a valid Scheduled action"
            if action not in self._VALID_SCHEDULE_TARGETS
            else ""
        )
        return {
            "status": "ok",
            "message": (
                f"Schedule: {crontab} | enabled={task.enabled} | "
                f"last run: {last_run} | total runs: {task.total_run_count}{invalid}"
            ),
        }


# Registered at module import time so the Celery worker process (which
# loads plugins the same way Dispatcharr's web process does) knows this
# task name before django-celery-beat ever tries to dispatch it. Must stay
# at module level, not inside the Plugin class — Celery discovers tasks by
# import, not by introspecting arbitrary class attributes.
try:
    from celery import shared_task as _vod_manager_shared_task

    @_vod_manager_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod_manager_scheduled_run(action="scan_and_process", settings=None, scheduled=True):
        import logging

        logger = logging.getLogger("vod_manager.schedule")
        result = Plugin().run(
            action, {},
            {"logger": logger, "settings": settings or {}, "scheduled": scheduled, "background": True},
        )
        if result.get("status") == "error":
            logger.error("Scheduled action '%s' failed: %s", action, result.get("message"))
        if not scheduled:
            return result
        try:
            from django.utils import timezone
            from django_celery_beat.models import PeriodicTask

            PeriodicTask.objects.filter(name=Plugin.SCHEDULE_TASK_NAME).update(
                last_run_at=timezone.now()
            )
        except Exception as e:
            logger.warning("Failed to bump PeriodicTask.last_run_at: %s", e)
        return result
except Exception as _celery_register_err:  # pragma: no cover - environment-dependent
    import logging

    logging.getLogger("vod_manager.schedule").error(
        "Could not register scheduled Celery task (scheduling will be unavailable): %s",
        _celery_register_err,
    )
