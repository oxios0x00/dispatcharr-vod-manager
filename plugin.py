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
import os

from .pipeline import CONTENT_TYPE_EPISODE, CONTENT_TYPE_MOVIE, CONTENT_TYPE_SERIES, PipelineMixin
from .schedule import ScheduleMixin

# Sibling submodules (.measurements/.selection/.store) are imported lazily inside
# methods below, not at module top level — matches the defensive pattern
# used by other Dispatcharr plugins in this ecosystem (see e.g.
# iptv_checker/plugin.py's `from . import notify_report, reports` done
# inside function bodies) to avoid stale references across a plugin
# reload cycle. PipelineMixin and ScheduleMixin are the exception: they
# hold no Django state of their own at import time, just methods later
# calls into Django through, same as importing selection.py or store.py.


class Plugin(PipelineMixin, ScheduleMixin):
    name = "VOD Manager"
    version = "2.4.4"
    description = (
        "Curates Dispatcharr's VOD catalogue from vod-probe's measurements: keeps the versions matching "
        "your quality/language settings and prunes the rest. Optional .strm generation for Emby/Jellyfin. "
        "Needs vod-probe. Dry-run by default."
    )
    author = "oxios0x00"
    help_url = ""

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
            "id": "_section_settings",
            "label": "[SETTINGS]",
            "type": "info",
            "description": (
                "Changing any setting below only affects titles processed afterward, never "
                "retroactively, and an already-pruned relation isn't recoverable from a setting "
                "change alone. For a reliable result after a change, run [MAINTENANCE] Reset "
                "Plugin State, then Scan + Process again from scratch."
            ),
        },
        {
            "id": "target_qualities",
            "label": "Qualities to keep (one winner per tier)",
            "type": "string",
            "default": "2160p,1080p",
            "help_text": "Comma-separated, best first. Empty = every tier. Falls back to the best available tier if none listed exist.",
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
            "help_text": "OFF (default): keep every matching version per tier. ON: one winner per tier (old, more aggressive behaviour).",
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
            "description": "Permanently exclude specific titles by TMDB id, regardless of the settings above.",
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
            "description": "Same select/prune pipeline as Films, per episode, using the settings above.",
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
            "description": "Optional: writes one .strm per kept relation, for Emby/Jellyfin multi-version playback. Run order: Scan + Process until Queue Status is empty, then Generate.",
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
            "help_text": "ON (default): tags the folder and file names with the title's TMDB/IMDB id, for Emby/Jellyfin identification and Jellyfin's multi-version grouping. Flipping this renames the whole tagged tree on the next Generate.",
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
            "description": "Runs one action on its own cron. Fill in the fields below, click Apply, then restart Dispatcharr once. Leave Schedule empty until you trust the picks.",
        },
        {
            "id": "schedule_cron",
            "label": "Schedule (5-field cron)",
            "type": "string",
            "default": "",
            "help_text": "'minute hour day-of-month month day-of-week'. Empty falls back to every 6 hours when Apply is clicked. Best set a few minutes after Dispatcharr's VOD refresh and vod-probe.",
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

        # A sibling of this plugin's own folder under Dispatcharr's plugins
        # directory, not a subfolder of it: updating a plugin replaces only
        # its own folder (apps/plugins/api_views.py's install/overwrite path
        # renames it to a backup and deletes that backup once the new
        # version is in place) — a sibling folder is untouched by that swap,
        # so the queue, known relations, .strm manifest and run history all
        # survive an update instead of silently resetting.
        data_dir = os.environ.get(
            "VOD_MANAGER_DATA_DIR",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "vod_manager_data"),
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

    # --- background runs -----------------------------------------------------
    #
    # Scan + Process runs in a Celery worker, not in the request that clicked
    # it: a whole catalogue takes longer than the browser (about a minute
    # here) or nginx (300 s) will wait, so a synchronous click ended
    # in a 504 while the work carried on unseen. The click now only queues the
    # task; Queue Status shows how far it got and Stop ends it after the
    # current batch.

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
                    f"{queue['in_progress']} in progress, {queue['waiting']} waiting — "
                    f"finish Scan + Process first ({queue_name})."
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

    # --- title cleanup (cosmetic, independent of selection) -----------------

    def _clean_movie_titles(self, settings):
        from apps.vod.models import Movie

        return self._clean_titles_for_model(Movie, settings, "movie", "movies")

    def _clean_series_titles(self, settings):
        from apps.vod.models import Series

        return self._clean_titles_for_model(Series, settings, "series", "series")

    def _clean_titles_for_model(self, model_cls, settings, noun_singular, noun_plural):
        import logging

        from .title_cleanup import parse_tag_list, strip_title_tags

        logger = logging.getLogger("vod_manager.title_cleanup")
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
        msg = f"{verb} {changed} {noun}"
        if skipped_no_id:
            msg += f", {skipped_no_id} skipped (no TMDB/IMDB id)"
        msg += "."
        if examples:
            logger.info("%s %s: %s", verb, noun, "; ".join(examples))
        return {"status": "ok", "message": msg, "changed": changed, "skipped_no_id": skipped_no_id}

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
