"""VOD Manager — automatic quality/language curation for Dispatcharr's VOD
catalogue.

Probes each M3UMovieRelation/M3UEpisodeRelation for a title with ffprobe,
picks the winning relation(s) per the target_qualities/target_languages
algorithm, and prunes the losing relations from Dispatcharr's own
database. Optionally also generates .strm files pinned directly to each
kept relation (see strm.py), for media servers that can't get real
multi-version playback through Dispatcharr's native Xtream API alone.

Every design decision referenced in comments below was validated against
a real Dispatcharr instance and the real Dispatcharr source.
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

CONTENT_TYPE_MOVIE = "movie"
CONTENT_TYPE_SERIES = "series"
CONTENT_TYPE_EPISODE = "episode"

# Sibling submodules (.probe/.selection/.store) are imported lazily inside
# methods below, not at module top level — matches the defensive pattern
# used by other Dispatcharr plugins in this ecosystem (see e.g.
# iptv_checker/plugin.py's `from . import notify_report, reports` done
# inside function bodies) to avoid stale references across a plugin
# reload cycle.


def _parse_csv_list(value):
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


class _RateLimiter:
    """Minimum-interval limiter shared across worker threads: caps probes
    per second independently of how many run concurrently."""

    def __init__(self, max_per_second):
        self._min_interval = 1.0 / max_per_second if max_per_second > 0 else 0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self):
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_allowed)
            self._next_allowed = start_at + self._min_interval
        sleep_for = start_at - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)


class Plugin:
    name = "VOD Manager"
    version = "1.0.3"
    description = (
        "Probes movie/series stream quality and language with ffprobe, keeps one "
        "winner per configured tier, and prunes the rest — with optional .strm "
        "generation for real multi-version playback in Emby/Jellyfin. Dry-run by "
        "default."
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
                "ON: probes and decides winners but never deletes anything. "
                "Turn OFF once a test batch looks right — pruning is a real "
                "delete, only recoverable via Dispatcharr's own next refresh."
            ),
        },
        {
            "id": "target_qualities",
            "label": "Qualities to keep (one winner per tier)",
            "type": "string",
            "default": "2160p,1080p",
            "help_text": (
                "Comma-separated, best first. Keeps one relation per listed "
                "tier the title actually has (e.g. one 2160p + one 1080p), "
                "never eliminates a title for lacking a tier. If none of the "
                "tiers match, falls back to the best quality available."
            ),
        },
        {
            "id": "target_languages",
            "label": "Target languages (ISO 639-2, e.g. fre,eng)",
            "type": "string",
            "default": "fre,eng",
            "help_text": "Comma-separated. Keeps the fewest relations whose audio languages together cover this set.",
        },
        {
            "id": "exclude_unmatched_language",
            "label": "Exclude relations matching none of the target languages",
            "type": "boolean",
            "default": False,
            "help_text": (
                "OFF (default): a tier with no matching language keeps its best-bitrate relation anyway. "
                "ON: that tier is dropped entirely — can leave a title with nothing kept if no tier matches."
            ),
        },
        {
            "id": "batch_size",
            "label": "Batch size (movies per click)",
            "type": "number",
            "default": 25,
            "min": 1,
            "help_text": "Start small (5-25) to validate before scaling up.",
        },
        {
            "id": "max_concurrent_probes",
            "label": "Max concurrent probes",
            "type": "number",
            "default": 2,
            "min": 1,
            "help_text": "Stay below your provider's connection limit — probing uses a real stream connection.",
        },
        {
            "id": "max_probes_per_second",
            "label": "Max probes started per second",
            "type": "number",
            "default": 1,
            "min": 0,
            "help_text": "Caps launch rate independently of concurrency.",
        },
        {
            "id": "circuit_breaker_error_ratio",
            "label": "Auto-pause queue if error ratio exceeds",
            "type": "number",
            "default": 0.5,
            "min": 0,
            "max": 1,
            "step": 0.1,
            "help_text": "Pauses the queue once this fraction of a batch fails, instead of hammering a struggling provider.",
        },
        {
            "id": "_section_series",
            "label": "[SERIES]",
            "type": "info",
            "description": (
                "Same probe/select/prune pipeline as Films, per episode. Uses the "
                "same quality/language/dry-run/concurrency settings above."
            ),
        },
        {
            "id": "series_batch_size",
            "label": "Batch size (series per click)",
            "type": "number",
            "default": 5,
            "min": 1,
            "help_text": "Smaller than the Films batch size — one series can fan out into dozens of episodes.",
        },
        {
            "id": "episode_sampling",
            "label": "Episode sampling",
            "type": "select",
            "default": "first_only",
            "options": [
                {"value": "first_only", "label": "Probe first episode per season, apply to the rest (recommended)"},
                {"value": "sample_n", "label": "Probe first N episodes per season (see setting below)"},
                {"value": "all", "label": "Probe every episode individually"},
            ],
            "help_text": (
                "Probing every episode is slow on long shows. 'First only' assumes a season is encoded "
                "consistently; 'Probe first N' verifies that assumption before extrapolating, falling back "
                "to per-episode probing if a season turns out inconsistent."
            ),
        },
        {
            "id": "episode_sample_size",
            "label": "Episodes to probe per season (if sampling = 'Probe first N')",
            "type": "number",
            "default": 2,
            "min": 1,
            "help_text": "Higher = more confidence a season is consistently encoded, at the cost of more probes.",
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
            "help_text": "Literal prefixes, matched case-insensitively at the start of the title. Longest match wins; stacked tags are stripped repeatedly.",
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
            "description": (
                "Dispatcharr's Xtream API always serves whichever relation's M3U account has the highest "
                "priority, regardless of category — a plain IPTV client can't select a specific quality tier. "
                "Writing .strm files pinned to each exact relation sidesteps that, for media servers (Emby, "
                "Jellyfin) that read them from disk. Safe to re-run: unchanged files are untouched, and a "
                "pruned/removed relation's file is deleted automatically (only files this plugin tracks — "
                "nothing added by hand is touched). Recommended order: Scan, then Process Batch repeatedly "
                "until Queue Status reads 0 pending and 0 in progress, then Clean Titles, then Generate. "
                "Both Generate actions refuse to run at all while their queue still has pending or "
                "in-progress items, precisely to prevent that ordering mistake — generating too early would "
                "give unprobed titles a '- unprobed' filename and write files for relations about to be "
                "pruned, both of which get silently cleaned up (renamed/deleted) on the next Generate anyway, "
                "so nothing is gained by rushing it."
            ),
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
            "help_text": "OFF (default): only runs from the Generate buttons. When ON, this only applies to the scheduled Scan + Process (cron or Test Fire) — clicking Run manually never auto-generates, since Dispatcharr may still be matching newly-scanned content against providers and writing .strm files mid-match can produce bad/incomplete entries.",
        },
        {
            "id": "strm_include_id_tag",
            "label": "Include [tmdbid-####] / [imdbid-ttXXXXXXX] in the movie/series folder name",
            "type": "boolean",
            "default": False,
            "help_text": "OFF (default): the folder is named from the title only, as before. ON: appends the Jellyfin/Emby external-id tag to the movie or series folder (not the files inside it — they'd all share the same id, so it would be pure redundancy) when the title has a TMDB or IMDB id, so the media server identifies it by id instead of guessing from text — falls back to the plain title when neither id is known. Turning this on renames every existing tagged folder on the next Generate run (old paths are removed automatically, same as any other pruned relation) — a one-time, deliberate library-wide rename, not something to flip casually on a library your media server is actively using.",
        },
        {
            "id": "strm_require_id",
            "label": "Skip titles with no TMDB/IMDB id",
            "type": "boolean",
            "default": False,
            "help_text": "OFF (default): a title with no id still gets a .strm, named from the raw provider title text alone. ON: skip generating (or remove an already-generated) .strm entirely for a title with neither id — some providers never expose one for certain content (confirmed happening for entire series catalogues on at least one provider), and a media server has nothing reliable to identify that file by regardless of how clean the title text is.",
        },
        {
            "id": "_section_reprobe",
            "label": "[TARGETED RE-PROBE]",
            "type": "info",
            "description": (
                "A relation is only ever probed once and trusted forever — if a provider swaps the file "
                "behind a stream_id without changing it (confirmed happening for real), the cached quality "
                "can go stale silently. Fixes just the one relation you point at, not the whole catalogue."
            ),
        },
        {
            "id": "reprobe_stream_id",
            "label": "Stream ID to force re-probe",
            "type": "string",
            "default": "",
            "help_text": "Copy from a .strm file's URL (?stream_id=...). Works for both movie and episode relations.",
        },
        {
            "id": "_section_schedule",
            "label": "[SCHEDULE]",
            "type": "info",
            "description": "Runs on its own cron, independent of Dispatcharr's own refresh. To activate: 1) fill in Schedule (cron), Schedule timezone, and Scheduled action below, 2) click the [SCHEDULE] Apply action — this registers the schedule but does not run it yet, 3) after installing or updating this plugin, restart Dispatcharr once (a Celery worker only picks up a newly-registered scheduled task at its own startup — Apply succeeding is not enough on its own). Use [SCHEDULE] Test Fire Now to run it immediately and confirm it's wired up, and [SCHEDULE] Status to check when it last actually ran. Re-click Apply any time you change Schedule/timezone/action or any setting the scheduled run itself should use — settings are snapshotted at Apply time, not read live. Warning: leave the cron unset (Schedule left empty) during a first import or against a large catalogue — run Scan/Process/Clean Titles/Generate manually and watch the results until the queues settle down, then schedule it once you're confident in the picks it's making unattended.",
        },
        {
            "id": "schedule_cron",
            "label": "Schedule (5-field cron)",
            "type": "string",
            "default": "",
            "help_text": "'minute hour day-of-month month day-of-week'. Leave empty until you're ready to schedule — Apply Schedule falls back to every 6 hours if left blank when clicked.",
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
                {"value": "scan_and_process", "label": "Scan + Process Batch (recommended)"},
                {"value": "scan_movies", "label": "Scan Movies only"},
                {"value": "process_batch", "label": "Process Batch only"},
                {"value": "clean_movie_titles", "label": "Clean Movie Titles only"},
                {"value": "clean_series_titles", "label": "Clean Series Titles only"},
                {"value": "scan_and_process_series", "label": "Scan + Process Series (recommended)"},
                {"value": "scan_series", "label": "Scan Series only"},
                {"value": "process_series", "label": "Process Series Batch only"},
                {"value": "retry_empty_series_fetches", "label": "Retry Empty Series Episode Fetches only"},
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
            "description": "One click: scan for changed movies, then probe/select/prune the batch. Refuses to start a second one while an earlier click is still processing.",
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
            "id": "process_batch",
            "label": "[MOVIES] Process Batch",
            "description": "Probe + select + (unless Dry Run) prune the next queued batch. Refuses to start a second one while an earlier click is still processing.",
            "button_label": "Process",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "queue_status",
            "label": "[MOVIES] Queue Status",
            "description": "Pending/in-progress/done/error counts and the last run.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "pause_queue",
            "label": "[MOVIES] Pause Queue",
            "description": "Stop Process Batch until resumed.",
            "button_label": "Pause",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "resume_queue",
            "label": "[MOVIES] Resume Queue",
            "description": "Clear a pause (manual or circuit-breaker).",
            "button_label": "Resume",
            "button_variant": "outline",
            "button_color": "teal",
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
            "description": "Write one .strm per kept movie relation (needs the [.STRM OUTPUT] settings). Refuses to run while the Movies queue still has pending/in-progress items, or while an earlier click is still generating — finish Process Batch first.",
            "button_label": "Generate",
            "button_variant": "outline",
            "button_color": "cyan",
        },
        {
            "id": "scan_and_process_series",
            "label": "[SERIES] Scan + Process",
            "description": "One click: scan for changed series, then probe/select/prune per episode. Refuses to start a second one while an earlier click is still processing.",
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
            "id": "process_series",
            "label": "[SERIES] Process Batch",
            "description": "Fetch each queued series' episodes if needed, then probe/select/prune. Refuses to start a second one while an earlier click is still processing.",
            "button_label": "Process",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "series_queue_status",
            "label": "[SERIES] Queue Status",
            "description": "Pending/in-progress/done/error counts and the last run, for series.",
            "button_label": "Status",
            "button_variant": "outline",
            "button_color": "blue",
        },
        {
            "id": "pause_series_queue",
            "label": "[SERIES] Pause Queue",
            "description": "Stop Process Series Batch until resumed. Independent of the Movies pause.",
            "button_label": "Pause",
            "button_variant": "outline",
            "button_color": "orange",
        },
        {
            "id": "resume_series_queue",
            "label": "[SERIES] Resume Queue",
            "description": "Clear a pause (manual or circuit-breaker) on the series queue.",
            "button_label": "Resume",
            "button_variant": "outline",
            "button_color": "teal",
        },
        {
            "id": "retry_empty_series_fetches",
            "label": "[SERIES] Retry Empty Episode Fetches",
            "description": "Dispatcharr marks a series-relation as 'episodes fetched' after any provider response that doesn't error — even an empty one — and never retries it again on its own (Dispatcharr/Dispatcharr#556 is the closest existing report, though that one's about a crash, not a silent empty response). This finds relations stuck exactly that way (fetched=true, zero episodes) and re-queues their series for a fresh attempt on the next Process Series Batch. Capped at 3 retries per series so a title that's genuinely empty on the provider's side doesn't get retried forever.",
            "button_label": "Retry Empty Fetches",
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
            "description": "Write one .strm per kept episode relation (needs the [.STRM OUTPUT] settings). Refuses to run while the Series queue still has pending/in-progress items, or while an earlier click is still generating — finish Process Series Batch first.",
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
            "id": "reprobe_by_stream_id",
            "label": "[MAINTENANCE] Force Re-probe by Stream ID",
            "description": "Clears the cached probe for one relation (movie or episode) and re-queues its title, using the Stream ID above. Click Process Batch/Series afterward to actually redo it.",
            "button_label": "Re-probe",
            "button_variant": "outline",
            "button_color": "orange",
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
            "id": "reset_plugin_state",
            "label": "[MAINTENANCE] Reset Plugin State",
            "description": "Wipes the probe cache, queues, known relations, run history and catalog stats — starts fresh on the next Scan/Process.",
            "button_label": "Reset",
            "button_variant": "outline",
            "button_color": "red",
            "confirm": {
                "required": True,
                "title": "Reset all plugin state?",
                "message": "Clears every probe result and queue this plugin has recorded — the next Process Batch/Series will re-probe everything from scratch. Dispatcharr's own movies/series/relations are untouched.",
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
        {
            "id": "test_fire_schedule",
            "label": "[SCHEDULE] Test Fire Now",
            "description": "Runs the scheduled action immediately, bypassing the cron timer.",
            "button_label": "Test Fire",
            "button_variant": "outline",
            "button_color": "grape",
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
        if action_id == "clean_movie_titles":
            return self._clean_movie_titles(settings)
        if action_id == "clean_series_titles":
            return self._clean_series_titles(settings)
        if action_id == "scan_movies":
            return self._scan_movies(settings)
        if action_id == "process_batch":
            return self._process_batch(settings)
        if action_id == "scan_and_process":
            return self._scan_and_process(settings, scheduled=scheduled)
        if action_id == "queue_status":
            return self._queue_status(CONTENT_TYPE_MOVIE)
        if action_id == "pause_queue":
            self.store.set_paused(CONTENT_TYPE_MOVIE, True)
            return {"status": "ok", "message": "Queue paused."}
        if action_id == "resume_queue":
            self.store.set_paused(CONTENT_TYPE_MOVIE, False)
            return {"status": "ok", "message": "Queue resumed."}
        if action_id == "scan_series":
            return self._scan_series(settings)
        if action_id == "process_series":
            return self._process_series_batch(settings)
        if action_id == "scan_and_process_series":
            return self._scan_and_process_series(settings, scheduled=scheduled)
        if action_id == "series_queue_status":
            return self._queue_status(CONTENT_TYPE_SERIES)
        if action_id == "catalog_stats":
            return self._catalog_stats(settings)
        if action_id == "generate_movie_strm":
            return self._generate_movie_strm(settings)
        if action_id == "generate_series_strm":
            return self._generate_series_strm(settings)
        if action_id == "delete_strm_files":
            return self._delete_strm_files(settings)
        if action_id == "reset_plugin_state":
            return self._reset_plugin_state(settings)
        if action_id == "reprobe_by_stream_id":
            return self._reprobe_by_stream_id(settings)
        if action_id == "pause_series_queue":
            self.store.set_paused(CONTENT_TYPE_SERIES, True)
            return {"status": "ok", "message": "Series queue paused."}
        if action_id == "resume_series_queue":
            self.store.set_paused(CONTENT_TYPE_SERIES, False)
            return {"status": "ok", "message": "Series queue resumed."}
        if action_id == "retry_empty_series_fetches":
            return self._retry_empty_series_fetches(settings)
        if action_id == "apply_schedule":
            return self._apply_schedule(settings)
        if action_id == "remove_schedule":
            return self._remove_schedule()
        if action_id == "schedule_status":
            return self._schedule_status()
        if action_id == "test_fire_schedule":
            return self._test_fire_schedule(settings)
        return {"status": "error", "message": f"Unknown action '{action_id}'"}

    def _busy_lock_message(self, human_name, held_since):
        """A re-click or an automatic retry landed on a second uwsgi worker
        while the first invocation was still running — without this, both
        would run to completion in parallel, each pinning its own worker
        for the whole batch, which is exactly what starved every other
        worker and made the whole UI look frozen during the 2026-09-18
        production incident. held_since comes from Store.try_acquire_lock;
        the message doubles as an explanation for why a fresh restart
        doesn't need a manual unlock (see that method's stale_after)."""
        elapsed = int(time.time() - held_since) if held_since else 0
        return {
            "status": "error",
            "message": (
                f"{human_name} is already running (started {elapsed}s ago) — wait for it to "
                "finish, or check Queue Status for progress, before starting another. If "
                "Dispatcharr restarted while one was running, this clears itself automatically "
                "after an hour."
            ),
        }

    def _scan_and_process(self, settings, scheduled=False):
        parts = []
        if settings.get("auto_clean_titles"):
            clean_result = self._clean_movie_titles(settings)
            parts.append(clean_result.get("message", ""))
        scan_result = self._scan_movies(settings)
        batch_result = self._process_batch(settings)
        parts.append(scan_result.get("message", ""))
        parts.append(batch_result.get("message", ""))
        if scheduled and settings.get("auto_generate_strm"):
            strm_result = self._generate_movie_strm(settings)
            parts.append(strm_result.get("message", ""))
        self._catalog_stats(settings)
        return {"status": "ok", "message": " | ".join(parts)}

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
                # plugin-local decision like probe results are.
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

    def _scan_movies(self, settings):
        from apps.vod.models import Movie, M3UMovieRelation

        enqueued = 0
        scanned = 0
        # Kept simple: iterate every movie with at least one active
        # relation. On a very large catalogue this single synchronous
        # action call can be slow — a Celery-backed background scan would
        # be the natural next step if that becomes a real bottleneck.
        movie_ids = (
            M3UMovieRelation.objects.filter(m3u_account__is_active=True)
            .values_list("movie_id", flat=True)
            .distinct()
        )
        for movie_id in movie_ids:
            scanned += 1
            current_ids = set(
                M3UMovieRelation.objects.filter(
                    movie_id=movie_id, m3u_account__is_active=True
                ).values_list("id", flat=True)
            )
            known_ids = self.store.get_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id)
            if known_ids is None or current_ids != known_ids:
                # requeue(), not enqueue(): enqueue() is INSERT ... ON
                # CONFLICT DO NOTHING, a silent no-op for a title that was
                # already processed (status done/error from a prior run) —
                # exactly the case a changed relation set needs to reset.
                self.store.requeue(CONTENT_TYPE_MOVIE, movie_id)
                enqueued += 1

        return {
            "status": "ok",
            "message": f"Scanned {scanned} movies, enqueued {enqueued} new/changed.",
        }

    # --- process batch ------------------------------------------------------

    def _process_batch(self, settings):
        acquired, held_since = self.store.try_acquire_lock("process_movie_batch")
        if not acquired:
            return self._busy_lock_message("A movie batch", held_since)
        try:
            if self.store.is_paused(CONTENT_TYPE_MOVIE):
                return {"status": "ok", "message": "Queue is paused — resume it to process."}

            target_qualities = _parse_csv_list(settings.get("target_qualities"))
            target_languages = _parse_csv_list(settings.get("target_languages"))
            exclude_unmatched_language = bool(settings.get("exclude_unmatched_language", False))
            dry_run = bool(settings.get("dry_run", True))
            batch_size = int(settings.get("batch_size", 25) or 25)
            max_concurrent = max(1, int(settings.get("max_concurrent_probes", 2) or 2))
            max_per_second = float(settings.get("max_probes_per_second", 1) or 0)
            breaker_ratio = float(settings.get("circuit_breaker_error_ratio", 0.5) or 0.5)

            movie_ids = self.store.claim_batch(CONTENT_TYPE_MOVIE, batch_size)
            if not movie_ids:
                return {"status": "ok", "message": "Nothing queued. Run Scan Movies first."}

            run_id = self.store.start_run(CONTENT_TYPE_MOVIE, dry_run)
            limiter = _RateLimiter(max_per_second)
            errors = 0
            pruned_total = 0
            processed = 0
            breaker_tripped = False

            with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                futures = {
                    pool.submit(
                        self._process_one_movie, mid, target_qualities, target_languages,
                        exclude_unmatched_language, dry_run, limiter,
                    ): mid
                    for mid in movie_ids
                }
                for future in as_completed(futures):
                    movie_id = futures[future]
                    try:
                        pruned = future.result()
                        pruned_total += pruned
                        self.store.mark_done(CONTENT_TYPE_MOVIE, movie_id)
                    except Exception as exc:  # noqa: BLE001 - surfaced via mark_error
                        errors += 1
                        self.store.mark_error(CONTENT_TYPE_MOVIE, movie_id, exc)
                    processed += 1

                    if processed >= 5 and not breaker_tripped:
                        if errors / processed > breaker_ratio:
                            self.store.set_paused(CONTENT_TYPE_MOVIE, True)
                            breaker_tripped = True

            note = "circuit breaker tripped" if breaker_tripped else ""
            self.store.finish_run(run_id, processed, errors, pruned_total, note=note)

            msg = (
                f"Processed {processed} ({errors} errors), "
                f"{'would prune' if dry_run else 'pruned'} {pruned_total} relations."
            )
            if breaker_tripped:
                msg += " Error rate too high — queue auto-paused."
            return {"status": "ok", "message": msg}
        finally:
            self.store.release_lock("process_movie_batch")

    def _process_one_movie(
        self, movie_id, target_qualities, target_languages, exclude_unmatched_language, dry_run, limiter
    ):
        """Retry wrapper around _process_one_movie_once: under load, a
        worker thread's DB connection checkout can occasionally hit a
        transient gevent scheduling error ("This operation would block
        forever") unrelated to the stream itself — confirmed empirically by
        immediately retrying failed titles and seeing them succeed. One
        retry is enough; anything else (a genuinely dead stream, a real
        bug) is not this class of error and should surface immediately."""
        try:
            return self._process_one_movie_once(
                movie_id, target_qualities, target_languages, exclude_unmatched_language, dry_run, limiter
            )
        except Exception as exc:
            if "would block forever" not in str(exc):
                raise
            return self._process_one_movie_once(
                movie_id, target_qualities, target_languages, exclude_unmatched_language, dry_run, limiter
            )

    def _process_one_movie_once(
        self, movie_id, target_qualities, target_languages, exclude_unmatched_language, dry_run, limiter
    ):
        """Probe every active relation for one movie, select winners, prune
        losers (unless dry_run). Returns the number of relations pruned
        (or that would be pruned, under dry_run)."""
        from apps.vod.models import M3UMovieRelation
        from .probe import PROBE_SCHEMA_VERSION, probe_stream
        from .selection import Candidate, select_winners

        relations = list(
            M3UMovieRelation.objects.filter(
                movie_id=movie_id, m3u_account__is_active=True
            )
        )
        if not relations:
            self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, set())
            return 0

        candidates = []
        for relation in relations:
            cached = self.store.get_probe(CONTENT_TYPE_MOVIE, relation.id)
            stale = (
                cached is None
                or not cached.get("ok")
                or cached.get("probe_version", 0) != PROBE_SCHEMA_VERSION
            )
            if stale:
                url = relation.get_stream_url()
                if not url:
                    continue
                limiter.wait()
                result = probe_stream(url)
                self.store.save_probe(CONTENT_TYPE_MOVIE, relation.id, result)
                cached = self.store.get_probe(CONTENT_TYPE_MOVIE, relation.id)
            if not cached or not cached.get("ok"):
                continue
            import json as _json

            candidates.append(
                Candidate(
                    relation.id,
                    _json.loads(cached["audio_languages"] or "[]"),
                    cached["quality_label"],
                    bitrate=cached.get("video_bitrate"),
                )
            )

        if not candidates:
            # Every relation failed to probe: leave the catalogue untouched
            # and let this title get retried on a future pass rather than
            # guessing a winner with zero data.
            raise RuntimeError(f"movie {movie_id}: no relation could be probed")

        winners = select_winners(candidates, target_languages, target_qualities, exclude_unmatched_language)
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

        enqueued = 0
        scanned = 0
        series_ids = (
            M3USeriesRelation.objects.filter(m3u_account__is_active=True)
            .values_list("series_id", flat=True)
            .distinct()
        )
        for series_id in series_ids:
            scanned += 1
            current_ids = set(
                M3USeriesRelation.objects.filter(
                    series_id=series_id, m3u_account__is_active=True
                ).values_list("id", flat=True)
            )
            known_ids = self.store.get_known_relation_ids(CONTENT_TYPE_SERIES, series_id)
            if known_ids is None or current_ids != known_ids:
                self.store.requeue(CONTENT_TYPE_SERIES, series_id)
                enqueued += 1

        return {
            "status": "ok",
            "message": f"Scanned {scanned} series, enqueued {enqueued} new/changed.",
        }

    # --- series: retry a fetch Dispatcharr wrongly considers "done" --------
    #
    # apps/vod/tasks.py's refresh_series_episodes() sets
    # custom_properties['episodes_fetched'] = True after ANY provider
    # response that doesn't raise — including one with an empty episode
    # list, e.g. from a transient provider glitch. Nothing in Dispatcharr
    # ever re-checks or clears that flag, so a relation unlucky enough to
    # hit an empty response on its one attempt stays stuck at zero
    # episodes forever, even once the provider's real data is fine.
    # _process_one_series_once (above) only calls refresh_series_episodes
    # when this flag is falsy, so flipping it back and re-queuing the
    # series is enough to force a genuine retry through the normal
    # pipeline — no separate retry codepath needed. Reported upstream
    # as a distinct case from Dispatcharr/Dispatcharr#556 (that one's
    # triggered by a crash during the sync, logged as an ERROR; this one
    # is a silent, error-free empty response).
    _MAX_EMPTY_FETCH_RETRIES = 3

    def _retry_empty_series_fetches(self, settings):
        from django.db.models import Count
        from apps.vod.models import M3USeriesRelation

        candidates = (
            M3USeriesRelation.objects.filter(m3u_account__is_active=True)
            .annotate(n_episodes=Count("episode_relations"))
            .filter(n_episodes=0)
        )

        reset_relations = 0
        capped = 0
        series_to_requeue = set()
        for relation in candidates:
            props = relation.custom_properties or {}
            if not props.get("episodes_fetched"):
                # Never fetched at all yet — not "stuck", just not reached
                # by the normal pipeline yet. Leave it alone.
                continue

            key = f"empty_series_retry_count:{relation.id}"
            attempts = self.store.get_state(key, 0)
            if attempts >= self._MAX_EMPTY_FETCH_RETRIES:
                capped += 1
                continue

            props["episodes_fetched"] = False
            relation.custom_properties = props
            relation.save(update_fields=["custom_properties"])
            self.store.set_state(key, attempts + 1)
            reset_relations += 1
            series_to_requeue.add(relation.series_id)

        for series_id in series_to_requeue:
            self.store.requeue(CONTENT_TYPE_SERIES, series_id)

        msg = (
            f"Reset {reset_relations} relation(s) across {len(series_to_requeue)} series — "
            "re-queued for a fresh fetch on the next Process Series Batch."
        )
        if capped:
            msg += f" {capped} relation(s) skipped (already retried {self._MAX_EMPTY_FETCH_RETRIES}x, likely genuinely empty)."
        return {"status": "ok", "message": msg}

    def _scan_and_process_series(self, settings, scheduled=False):
        parts = []
        if settings.get("auto_clean_titles"):
            clean_result = self._clean_series_titles(settings)
            parts.append(clean_result.get("message", ""))
        scan_result = self._scan_series(settings)
        batch_result = self._process_series_batch(settings)
        parts.append(scan_result.get("message", ""))
        parts.append(batch_result.get("message", ""))
        if scheduled and settings.get("auto_generate_strm"):
            strm_result = self._generate_series_strm(settings)
            parts.append(strm_result.get("message", ""))
        self._catalog_stats(settings)
        return {"status": "ok", "message": " | ".join(parts)}

    # --- series: process batch --------------------------------------------

    def _process_series_batch(self, settings):
        acquired, held_since = self.store.try_acquire_lock("process_series_batch")
        if not acquired:
            return self._busy_lock_message("A series batch", held_since)
        try:
            if self.store.is_paused(CONTENT_TYPE_SERIES):
                return {"status": "ok", "message": "Series queue is paused — resume it to process."}

            target_qualities = _parse_csv_list(settings.get("target_qualities"))
            target_languages = _parse_csv_list(settings.get("target_languages"))
            exclude_unmatched_language = bool(settings.get("exclude_unmatched_language", False))
            episode_sampling = settings.get("episode_sampling") or "first_only"
            episode_sample_size = int(settings.get("episode_sample_size", 2) or 2)
            dry_run = bool(settings.get("dry_run", True))
            batch_size = int(settings.get("series_batch_size", 5) or 5)
            max_concurrent = max(1, int(settings.get("max_concurrent_probes", 2) or 2))
            max_per_second = float(settings.get("max_probes_per_second", 1) or 0)
            breaker_ratio = float(settings.get("circuit_breaker_error_ratio", 0.5) or 0.5)

            series_ids = self.store.claim_batch(CONTENT_TYPE_SERIES, batch_size)
            if not series_ids:
                return {"status": "ok", "message": "Nothing queued. Run Scan Series first."}

            run_id = self.store.start_run(CONTENT_TYPE_SERIES, dry_run)
            limiter = _RateLimiter(max_per_second)
            errors = 0
            pruned_total = 0
            processed = 0
            breaker_tripped = False

            with ThreadPoolExecutor(max_workers=max_concurrent) as pool:
                futures = {
                    pool.submit(
                        self._process_one_series, sid, target_qualities, target_languages,
                        exclude_unmatched_language, episode_sampling, episode_sample_size, dry_run, limiter,
                    ): sid
                    for sid in series_ids
                }
                for future in as_completed(futures):
                    series_id = futures[future]
                    try:
                        pruned = future.result()
                        pruned_total += pruned
                        self.store.mark_done(CONTENT_TYPE_SERIES, series_id)
                    except Exception as exc:  # noqa: BLE001 - surfaced via mark_error
                        errors += 1
                        self.store.mark_error(CONTENT_TYPE_SERIES, series_id, exc)
                    processed += 1

                    if processed >= 5 and not breaker_tripped:
                        if errors / processed > breaker_ratio:
                            self.store.set_paused(CONTENT_TYPE_SERIES, True)
                            breaker_tripped = True

            note = "circuit breaker tripped" if breaker_tripped else ""
            self.store.finish_run(run_id, processed, errors, pruned_total, note=note)

            msg = (
                f"Processed {processed} series ({errors} errors), "
                f"{'would prune' if dry_run else 'pruned'} {pruned_total} episode relation(s)."
            )
            if breaker_tripped:
                msg += " Error rate too high — queue auto-paused."
            return {"status": "ok", "message": msg}
        finally:
            self.store.release_lock("process_series_batch")

    def _process_one_series(
        self, series_id, target_qualities, target_languages, exclude_unmatched_language,
        episode_sampling, episode_sample_size, dry_run, limiter,
    ):
        """Retry wrapper — see _process_one_movie for why: a worker
        thread's DB connection checkout can occasionally hit a transient
        gevent scheduling error unrelated to the series itself."""
        try:
            return self._process_one_series_once(
                series_id, target_qualities, target_languages, exclude_unmatched_language,
                episode_sampling, episode_sample_size, dry_run, limiter,
            )
        except Exception as exc:
            if "would block forever" not in str(exc):
                raise
            return self._process_one_series_once(
                series_id, target_qualities, target_languages, exclude_unmatched_language,
                episode_sampling, episode_sample_size, dry_run, limiter,
            )

    def _process_one_series_once(
        self, series_id, target_qualities, target_languages, exclude_unmatched_language,
        episode_sampling, episode_sample_size, dry_run, limiter,
    ):
        """For one series: ensure every active source's episode list is
        loaded, then probe + select + (unless dry_run) prune per episode.
        Returns the number of episode relations pruned (or that would be)."""
        from apps.vod.models import Series, M3USeriesRelation, Episode, M3UEpisodeRelation
        from apps.vod.tasks import refresh_series_episodes
        from .probe import PROBE_SCHEMA_VERSION, probe_stream
        from .selection import Candidate, select_winners

        series = Series.objects.get(id=series_id)
        series_relations = list(
            M3USeriesRelation.objects.filter(series=series, m3u_account__is_active=True)
        )
        if not series_relations:
            self.store.set_known_relation_ids(CONTENT_TYPE_SERIES, series_id, set())
            return 0

        for relation in series_relations:
            if not (relation.custom_properties or {}).get("episodes_fetched"):
                limiter.wait()
                refresh_series_episodes(
                    relation.m3u_account, series, relation.external_series_id
                )

        episodes = list(
            Episode.objects.filter(series=series).order_by("season_number", "episode_number")
        )

        pruned_total = 0
        episodes_with_no_candidates = 0
        # Episode sampling, keyed per (season, m3u_account): probe the first
        # `sample_target_n` episodes for real; if they all agree on quality
        # and audio languages, the first one becomes that combination's
        # confirmed representative and every later episode in the same
        # season/account reuses its result instead of a fresh ffprobe call.
        # If they disagree, the season is treated as inconsistently encoded
        # and every remaining episode is probed for real instead of risking
        # an extrapolation that's just as likely to be wrong as right.
        # sample_target_n=None ("all") never establishes a representative.
        sample_target_n = {
            "first_only": 1,
            "all": None,
        }.get(episode_sampling)
        if episode_sampling == "sample_n":
            sample_target_n = max(1, int(episode_sample_size or 2))
        season_state = {}

        for episode in episodes:
            ep_relations = list(
                M3UEpisodeRelation.objects.filter(
                    episode=episode, m3u_account__is_active=True
                )
            )
            if not ep_relations:
                continue

            candidates = []
            for ep_relation in ep_relations:
                cached = self.store.get_probe(CONTENT_TYPE_EPISODE, ep_relation.id)
                stale = (
                    cached is None
                    or not cached.get("ok")
                    or cached.get("probe_version", 0) != PROBE_SCHEMA_VERSION
                )
                if stale:
                    import json as _json

                    sample_key = (episode.season_number, ep_relation.m3u_account_id)
                    state = season_state.setdefault(
                        sample_key,
                        {"probes": [], "reference": None, "consistent": True, "representative": None},
                    )

                    if state["representative"] is not None:
                        source_probe = self.store.get_probe(CONTENT_TYPE_EPISODE, state["representative"])
                        if source_probe and source_probe.get("ok"):
                            self.store.save_probe(
                                CONTENT_TYPE_EPISODE, ep_relation.id, source_probe,
                                sampled_from_relation_id=state["representative"],
                            )
                            cached = self.store.get_probe(CONTENT_TYPE_EPISODE, ep_relation.id)
                            stale = False

                    if stale:
                        url = ep_relation.get_stream_url()
                        if not url:
                            continue
                        limiter.wait()
                        result = probe_stream(url)
                        self.store.save_probe(CONTENT_TYPE_EPISODE, ep_relation.id, result)
                        cached = self.store.get_probe(CONTENT_TYPE_EPISODE, ep_relation.id)
                        if cached and cached.get("ok"):
                            state["probes"].append(ep_relation.id)
                            signature = (
                                cached["quality_label"],
                                tuple(sorted(_json.loads(cached["audio_languages"] or "[]"))),
                            )
                            if state["reference"] is None:
                                state["reference"] = signature
                            elif signature != state["reference"]:
                                state["consistent"] = False
                            if (
                                sample_target_n is not None
                                and state["consistent"]
                                and len(state["probes"]) >= sample_target_n
                            ):
                                state["representative"] = state["probes"][0]
                if not cached or not cached.get("ok"):
                    continue
                import json as _json

                candidates.append(
                    Candidate(
                        ep_relation.id,
                        _json.loads(cached["audio_languages"] or "[]"),
                        cached["quality_label"],
                        bitrate=cached.get("video_bitrate"),
                    )
                )

            if not candidates:
                episodes_with_no_candidates += 1
                continue

            winners = select_winners(candidates, target_languages, target_qualities, exclude_unmatched_language)
            winner_ids = {c.relation_id for c in winners}
            all_ids = {r.id for r in ep_relations}
            loser_ids = all_ids - winner_ids

            if dry_run:
                pruned_total += len(loser_ids)
                continue

            if loser_ids:
                M3UEpisodeRelation.objects.filter(id__in=loser_ids).delete()
            pruned_total += len(loser_ids)

        if episodes and episodes_with_no_candidates == len(episodes):
            raise RuntimeError(f"series {series_id}: no episode relation could be probed")

        self.store.set_known_relation_ids(
            CONTENT_TYPE_SERIES, series_id,
            {r.id for r in series_relations},
        )
        return pruned_total

    # --- status ---------------------------------------------------------

    def _queue_status(self, content_type):
        counts = self.store.queue_counts(content_type)
        last = self.store.last_run(content_type)
        paused = self.store.is_paused(content_type)
        msg = (
            f"pending={counts['pending']} in_progress={counts['in_progress']} "
            f"done={counts['done']} error={counts['error']}"
            f"{' [PAUSED]' if paused else ''}"
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

        movie_relation_ids = list(
            M3UMovieRelation.objects.filter(m3u_account__is_active=True)
            .values_list("id", "movie_id")
        )
        episode_relation_ids = list(
            M3UEpisodeRelation.objects.filter(m3u_account__is_active=True)
            .values_list("id", "episode__series_id")
        )

        movie_quality_by_relation = self.store.get_quality_labels(
            CONTENT_TYPE_MOVIE, [rid for rid, _ in movie_relation_ids]
        )
        episode_quality_by_relation = self.store.get_quality_labels(
            CONTENT_TYPE_EPISODE, [rid for rid, _ in episode_relation_ids]
        )

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
            from .strm import build_proxy_url, id_tag, plan_suffixes, remove_stale_files, sanitize_filename, write_strm_if_changed

            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            if not base_url or not library_root:
                return {
                    "status": "error",
                    "message": "Set both 'Dispatcharr base URL' and 'Library root path' in [.STRM OUTPUT] first.",
                }
            queue = self.store.queue_counts(CONTENT_TYPE_MOVIE)
            if queue["pending"] or queue["in_progress"]:
                return {
                    "status": "error",
                    "message": (
                        f"{queue['pending']} movie(s) pending, {queue['in_progress']} in progress — "
                        "finish Process Batch first (Queue Status should read 0 pending and 0 in "
                        "progress). Generating now would give still-unprobed titles a '- unprobed' "
                        "filename and write a file for a relation that's about to be pruned, only for "
                        "it to disappear on the next Generate run."
                    ),
                }
            subfolder = (settings.get("strm_movies_subfolder") or "movies").strip() or "movies"
            library_dir = os.path.join(library_root, subfolder)
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))

            relations = list(
                M3UMovieRelation.objects.filter(m3u_account__is_active=True)
                .select_related("movie")
                .order_by("movie_id", "id")
            )
            quality_by_relation = self.store.get_quality_labels(CONTENT_TYPE_MOVIE, [r.id for r in relations])

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
                safe_name = sanitize_filename(movie.name)
                tag = id_tag(movie.tmdb_id, movie.imdb_id) if include_id_tag else ""
                movie_dir = os.path.join(library_dir, sanitize_filename(movie.name + tag))
                suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in movie_relations])

                for rel, suffix in zip(movie_relations, suffixes):
                    # The id tag lives on the folder only — every file inside
                    # it shares the same movie, so repeating the tag on each
                    # one would be pure redundancy.
                    path = os.path.join(movie_dir, f"{safe_name}{suffix}.strm")
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

            from .strm import build_proxy_url, id_tag, plan_suffixes, remove_stale_files, sanitize_filename, write_strm_if_changed

            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            if not base_url or not library_root:
                return {
                    "status": "error",
                    "message": "Set both 'Dispatcharr base URL' and 'Library root path' in [.STRM OUTPUT] first.",
                }
            queue = self.store.queue_counts(CONTENT_TYPE_SERIES)
            if queue["pending"] or queue["in_progress"]:
                return {
                    "status": "error",
                    "message": (
                        f"{queue['pending']} series pending, {queue['in_progress']} in progress — "
                        "finish Process Series Batch first (Series Queue Status should read 0 pending "
                        "and 0 in progress). Generating now would give still-unprobed titles a "
                        "'- unprobed' filename and write a file for a relation that's about to be "
                        "pruned, only for it to disappear on the next Generate run."
                    ),
                }
            subfolder = (settings.get("strm_series_subfolder") or "series").strip() or "series"
            library_dir = os.path.join(library_root, subfolder)
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))

            relations = list(
                M3UEpisodeRelation.objects.filter(m3u_account__is_active=True)
                .select_related("episode", "episode__series")
                .order_by("episode_id", "id")
            )
            quality_by_relation = self.store.get_quality_labels(CONTENT_TYPE_EPISODE, [r.id for r in relations])

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

                # The id tag lives on the series folder only — every episode
                # under it shares the same series, so repeating the tag on
                # each episode filename would be pure redundancy.
                safe_series = sanitize_filename(series.name)
                tag = id_tag(series.tmdb_id, series.imdb_id) if include_id_tag else ""
                series_dir = os.path.join(library_dir, sanitize_filename(series.name + tag))
                season_num = episode.season_number or 1
                episode_num = episode.episode_number or 0
                base_filename = f"{safe_series} - S{season_num:02d}E{episode_num:02d}"
                season_dir = os.path.join(series_dir, f"Season {season_num:02d}")

                suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in episode_relations])

                for rel, suffix in zip(episode_relations, suffixes):
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

    def _reset_plugin_state(self, settings):
        """Wipes this plugin's own sidecar state (probe cache, queues, known
        relation sets, pause flags, run history, catalog stats, .strm
        tracking) so the next Scan/Process starts completely from scratch.
        Never touches Dispatcharr's own database or any real .strm file —
        pair with Delete .strm Files if you also want those gone."""
        self.store.reset_all()
        return {
            "status": "ok",
            "message": (
                "Plugin state reset: probe cache, queues, known relations, "
                "run history, catalog stats and .strm tracking all cleared. "
                "Dispatcharr's own catalogue is untouched."
            ),
        }

    def _reprobe_by_stream_id(self, settings):
        """Targeted fix for a provider silently swapping the file behind a
        stream_id after this plugin already probed and classified it (a
        real incident, not theoretical): clears just that one relation's
        cached probe and re-queues its movie/series, without touching anything
        else in the catalogue. Does not itself re-probe — Process Batch /
        Process Series Batch does that on the next click, same as any other
        queued title."""
        from apps.vod.models import M3UMovieRelation, M3UEpisodeRelation

        stream_id = (settings.get("reprobe_stream_id") or "").strip()
        if not stream_id:
            return {
                "status": "error",
                "message": "Set 'Stream ID to force re-probe' first — copy it from a .strm file's URL (?stream_id=...).",
            }

        movie_rel = (
            M3UMovieRelation.objects.filter(stream_id=stream_id, m3u_account__is_active=True)
            .select_related("movie")
            .first()
        )
        if movie_rel:
            self.store.delete_probe(CONTENT_TYPE_MOVIE, movie_rel.id)
            self.store.requeue(CONTENT_TYPE_MOVIE, movie_rel.movie_id)
            return {
                "status": "ok",
                "message": (
                    f"Cleared cached probe for '{movie_rel.movie.name}' and re-queued it — "
                    "click Process Batch to re-probe."
                ),
            }

        episode_rel = (
            M3UEpisodeRelation.objects.filter(stream_id=stream_id, m3u_account__is_active=True)
            .select_related("episode", "episode__series")
            .first()
        )
        if episode_rel:
            self.store.delete_probe(CONTENT_TYPE_EPISODE, episode_rel.id)
            self.store.requeue(CONTENT_TYPE_SERIES, episode_rel.episode.series_id)
            return {
                "status": "ok",
                "message": (
                    f"Cleared cached probe for one relation of '{episode_rel.episode.series.name}' "
                    "and re-queued the series — click Process Series Batch to re-probe (other "
                    "episodes keep using their own existing cache)."
                ),
            }

        return {"status": "error", "message": f"No active relation found with stream_id={stream_id!r}."}

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
        "process_batch",
        "clean_movie_titles",
        "clean_series_titles",
        "scan_and_process_series",
        "scan_series",
        "process_series",
        "generate_movie_strm",
        "generate_series_strm",
        "retry_empty_series_fetches",
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
        return {
            "status": "ok",
            "message": (
                f"Schedule: {crontab} | enabled={task.enabled} | "
                f"last run: {last_run} | total runs: {task.total_run_count}"
            ),
        }

    def _test_fire_schedule(self, settings):
        target = settings.get("schedule_target") or "scan_and_process"
        if target not in self._VALID_SCHEDULE_TARGETS:
            return {"status": "error", "message": f"Invalid schedule_target: {target}"}

        snapshot = {k: v for k, v in (settings or {}).items() if not k.startswith("schedule_")}

        # _vod_manager_scheduled_run is defined at module level below this
        # class; resolved by name at call time, not import time, so no
        # forward-reference issue — but registration can fail (e.g. Celery
        # not importable), in which case the name won't exist at all.
        task_fn = globals().get("_vod_manager_scheduled_run")
        if task_fn is None:
            return {"status": "error", "message": "Scheduled Celery task failed to register at plugin load — check server logs."}

        try:
            async_result = task_fn.apply_async(
                kwargs={"action": target, "settings": snapshot}, queue="dvr"
            )
        except Exception as e:
            return {"status": "error", "message": f"Failed to enqueue test fire on Celery: {e}"}

        return {
            "status": "ok",
            "message": f"Test fire enqueued ({target}); task id {async_result.id}. Check Schedule Status once the worker finishes.",
        }


# Registered at module import time so the Celery worker process (which
# loads plugins the same way Dispatcharr's web process does) knows this
# task name before django-celery-beat ever tries to dispatch it. Must stay
# at module level, not inside the Plugin class — Celery discovers tasks by
# import, not by introspecting arbitrary class attributes.
try:
    from celery import shared_task as _vod_manager_shared_task

    @_vod_manager_shared_task(name=Plugin.SCHEDULED_TASK_CELERY_NAME)
    def _vod_manager_scheduled_run(action="scan_and_process", settings=None):
        import logging

        logger = logging.getLogger("vod_manager.schedule")
        result = Plugin().run(action, {}, {"logger": logger, "settings": settings or {}, "scheduled": True})
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
