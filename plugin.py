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

from .pipeline import CONTENT_TYPE_EPISODE, CONTENT_TYPE_MOVIE, CONTENT_TYPE_SERIES, PipelineMixin
from .schedule import ScheduleMixin


def _load_manifest():
    """plugin.json is the single source of truth for name/version/description/
    author/fields/actions. Dispatcharr's loader (apps/plugins/loader.py,
    _load_plugin) reads these off the instantiated Plugin class first and
    only falls back to the manifest when the class leaves them empty — so a
    hand-copied duplicate here silently wins once a plugin is enabled, and a
    manifest-only edit (e.g. a newly added field) never reaches it. Confirmed
    the hard way: a field added to plugin.json alone never appeared in the
    UI for this already-enabled plugin until it was added here too. Reading
    the same file instead of copying it makes that class of bug impossible."""
    path = os.path.join(os.path.dirname(__file__), "plugin.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_MANIFEST = _load_manifest()

# Sibling submodules (.measurements/.selection/.store) are imported lazily inside
# methods below, not at module top level — matches the defensive pattern
# used by other Dispatcharr plugins in this ecosystem (see e.g.
# iptv_checker/plugin.py's `from . import notify_report, reports` done
# inside function bodies) to avoid stale references across a plugin
# reload cycle. PipelineMixin and ScheduleMixin are the exception: they
# hold no Django state of their own at import time, just methods later
# calls into Django through, same as importing selection.py or store.py.


class Plugin(PipelineMixin, ScheduleMixin):
    name = _MANIFEST["name"]
    version = _MANIFEST["version"]
    description = _MANIFEST["description"]
    author = _MANIFEST["author"]
    help_url = _MANIFEST.get("help_url", "")
    fields = _MANIFEST["fields"]
    actions = _MANIFEST["actions"]

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
        if action_id == "delete_strm_files":
            if not context.get("background"):
                return self._start_delete_strm_files(settings)
            return self._run_delete_strm_files_in_background(settings)
        if action_id == "clean_movie_titles":
            return self._clean_movie_titles(settings)
        if action_id == "clean_series_titles":
            return self._clean_series_titles(settings)
        if action_id == "scan_movies":
            return self._scan_movies(settings)
        if action_id == "scan_and_process":
            return self._scan_and_process(settings)
        if action_id == "scan_and_process_both":
            return self._scan_and_process_both(settings)
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

    def _profiles(self, settings):
        """The .strm routing profiles; raises profiles.ProfileError when the
        setting is malformed. An empty setting is one implicit profile on
        the plain movies/series subfolder settings."""
        from .profiles import parse_profiles

        return parse_profiles(
            settings.get("strm_profiles"),
            (settings.get("strm_movies_subfolder") or "movies").strip() or "movies",
            (settings.get("strm_series_subfolder") or "series").strip() or "series",
        )

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
        from .profiles import ProfileError

        try:
            self._profiles(settings)
        except ProfileError as exc:
            return {"status": "error", "message": f".STRM profiles: {exc}. Nothing was written or removed."}
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

    # --- delete .strm files (background: real filesystem I/O over the whole
    # library, the same "longer than the browser/nginx will wait" reasoning
    # as Scan + Process and Generate — it once ran synchronously and crashed
    # a uwsgi worker (OSError: too many open files) on a ~18k-file library) --

    _DELETE_STRM_LOCK = "delete_strm_files"
    _DELETE_STRM_LOCK_STALE_SECONDS = 3600

    def _start_delete_strm_files(self, settings):
        held_since = self.store.lock_held_since(self._DELETE_STRM_LOCK, self._DELETE_STRM_LOCK_STALE_SECONDS)
        if held_since:
            return self._busy_lock_message("Delete .strm Files", held_since, self._DELETE_STRM_LOCK_STALE_SECONDS)
        _, error = self._enqueue_background("delete_strm_files", settings, scheduled=False)
        if error:
            return error
        return {
            "status": "ok",
            "message": "Delete .strm Files started in the background; a notification appears when it is done.",
        }

    def _run_delete_strm_files_in_background(self, settings):
        import logging

        logger = logging.getLogger("vod_manager.delete_strm_files")
        acquired, held_since = self.store.try_acquire_lock(self._DELETE_STRM_LOCK, self._DELETE_STRM_LOCK_STALE_SECONDS)
        if not acquired:
            return self._busy_lock_message("Delete .strm Files", held_since, self._DELETE_STRM_LOCK_STALE_SECONDS)
        try:
            result = self._delete_strm_files(settings)
        finally:
            self.store.release_lock(self._DELETE_STRM_LOCK)
        logger.info("Delete .strm Files finished: %s", result.get("message"))
        self._notify_run_finished(
            "delete-strm-files", result.get("message", ""), stopped=result.get("status") == "error",
            logger=logger, title="VOD Manager: Delete .strm Files " + ("failed" if result.get("status") == "error" else "done"),
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
            from .nfo import build_nfo_xml, nfo_path
            from .profiles import assign
            from .strm import best_quality_first, build_proxy_url, id_tag, plan_suffixes, remove_stale_files_in_library, sanitize_filename, write_strm

            blocker = self._generate_blocker(CONTENT_TYPE_MOVIE, settings)
            if blocker:
                return blocker
            dry_run = bool(settings.get("dry_run", True))
            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            profiles = self._profiles(settings)
            profile_dirs = [os.path.join(library_root, p.movies_dir) for p in profiles]
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))
            write_nfo = bool(settings.get("strm_write_nfo"))

            relations = list(
                M3UMovieRelation.objects.filter(m3u_account__is_active=True)
                .select_related("movie")
                .order_by("movie_id", "id")
            )
            quality_by_relation = {r.id: measurements.tier(r.custom_properties) for r in relations}
            languages_by_relation = {r.id: measurements.languages(r.custom_properties) for r in relations}

            by_movie = {}
            for rel in relations:
                by_movie.setdefault(rel.movie_id, []).append(rel)

            created = updated = unchanged = errors = skipped_no_id = unrouted = 0
            nfo_written = 0
            written_by_profile = {p.name: 0 for p in profiles}
            current_paths = set()
            current_nfo_paths = set()
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

                routed = {}
                for rel in movie_relations:
                    for profile in assign(profiles, languages_by_relation[rel.id]):
                        routed.setdefault(profile, []).append(rel)
                if not routed:
                    # No profile takes any version of this movie; like a
                    # skipped id, its files are cleaned by the stale pass.
                    unrouted += 1
                    continue

                for profile, profile_relations in routed.items():
                    movie_dir = os.path.join(library_root, profile.movies_dir, folder_name)
                    suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in profile_relations])

                    for rel, suffix in best_quality_first(profile_relations, suffixes):
                        # Jellyfin only groups several files as versions of one
                        # movie when each file name starts character-for-character
                        # with the folder name, tag included — so the file name
                        # must repeat exactly what the folder is named, not just
                        # the plain title.
                        path = os.path.join(movie_dir, f"{folder_name}{suffix}.strm")
                        url = build_proxy_url(base_url, "movie", str(movie.uuid), rel.stream_id)
                        try:
                            result = write_strm(path, url, dry_run)
                        except OSError:
                            errors += 1
                            continue
                        current_paths.add(path)
                        written_by_profile[profile.name] += 1
                        if result == "created":
                            created += 1
                        elif result == "updated":
                            updated += 1
                        else:
                            unchanged += 1

                        if not write_nfo:
                            continue
                        xml = build_nfo_xml("movie", movie.tmdb_id, movie.imdb_id, rel.custom_properties)
                        if xml is None:
                            continue
                        sidecar = nfo_path(path)
                        try:
                            nfo_result = write_strm(sidecar, xml, dry_run)
                        except OSError:
                            errors += 1
                            continue
                        current_nfo_paths.add(sidecar)
                        if nfo_result in ("created", "updated"):
                            nfo_written += 1

            # Anything this plugin wrote last time but didn't write again just
            # now belongs to a relation that's been pruned, a movie that's
            # gone entirely, or a movie now skipped by "Skip titles with no
            # id" — safe to remove precisely because we tracked writing it
            # ourselves, unlike scanning the folder for "any .strm". Tracked
            # in its own manifest, separate from .nfo sidecars, so "removed"
            # keeps meaning exactly what it always has.
            stale = self.store.get_strm_manifest(CONTENT_TYPE_MOVIE) - current_paths
            stale_nfo = self.store.get_strm_manifest(CONTENT_TYPE_MOVIE + "_nfo") - current_nfo_paths
            if dry_run:
                removed = len(stale)
                nfo_removed = len(stale_nfo)
            else:
                removed = remove_stale_files_in_library(stale, library_root, profile_dirs)
                nfo_removed = remove_stale_files_in_library(stale_nfo, library_root, profile_dirs)
                self.store.save_strm_manifest(CONTENT_TYPE_MOVIE, current_paths)
                self.store.save_strm_manifest(CONTENT_TYPE_MOVIE + "_nfo", current_nfo_paths)

            prefix = "Would: " if dry_run else ""
            msg = f"{prefix}{created} new, {updated} updated, {removed} removed ({len(by_movie)} movies)."
            if nfo_written or nfo_removed:
                msg += f" {nfo_written} nfo written, {nfo_removed} nfo removed."
            if skipped_no_id:
                msg += f" {skipped_no_id} skipped (no id)."
            if len(profiles) > 1:
                msg += " By profile: " + ", ".join(f"{name} {n}" for name, n in written_by_profile.items()) + "."
            if unrouted:
                msg += f" {unrouted} movie(s) matched no profile."
            if errors:
                msg += f" {errors} write error(s)."
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
            from .nfo import build_nfo_xml, nfo_path
            from .profiles import assign
            from .strm import best_quality_first, build_proxy_url, id_tag, plan_suffixes, remove_stale_files_in_library, sanitize_filename, write_strm

            blocker = self._generate_blocker(CONTENT_TYPE_SERIES, settings)
            if blocker:
                return blocker
            dry_run = bool(settings.get("dry_run", True))
            base_url = (settings.get("strm_dispatcharr_url") or "").strip()
            library_root = (settings.get("strm_library_path") or "").strip()
            profiles = self._profiles(settings)
            profile_dirs = [os.path.join(library_root, p.series_dir) for p in profiles]
            include_id_tag = bool(settings.get("strm_include_id_tag"))
            require_id = bool(settings.get("strm_require_id"))
            write_nfo = bool(settings.get("strm_write_nfo"))

            relations = list(
                M3UEpisodeRelation.objects.filter(m3u_account__is_active=True)
                .select_related("episode", "episode__series")
                .order_by("episode_id", "id")
            )
            quality_by_relation = {r.id: measurements.tier(r.custom_properties) for r in relations}
            languages_by_relation = {r.id: measurements.languages(r.custom_properties) for r in relations}

            by_episode = {}
            for rel in relations:
                by_episode.setdefault(rel.episode_id, []).append(rel)

            created = updated = unchanged = errors = unrouted = 0
            nfo_written = 0
            written_by_profile = {p.name: 0 for p in profiles}
            current_paths = set()
            current_nfo_paths = set()
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

                # Jellyfin 12.0+ groups episode versions by season/episode
                # number, not by this tag (unlike movies, see Limitations) —
                # repeated here only for naming consistency with movies.
                tag = id_tag(series.tmdb_id, series.imdb_id) if include_id_tag else ""
                series_folder_name = sanitize_filename(series.name + tag)
                season_num = episode.season_number or 1
                episode_num = episode.episode_number or 0
                base_filename = f"{series_folder_name} - S{season_num:02d}E{episode_num:02d}"

                routed = {}
                for rel in episode_relations:
                    for profile in assign(profiles, languages_by_relation[rel.id]):
                        routed.setdefault(profile, []).append(rel)
                if not routed:
                    unrouted += 1
                    continue
                series_seen.add(series.id)

                for profile, profile_relations in routed.items():
                    season_dir = os.path.join(
                        library_root, profile.series_dir, series_folder_name, f"Season {season_num:02d}"
                    )
                    suffixes = plan_suffixes([quality_by_relation.get(r.id) for r in profile_relations])

                    for rel, suffix in best_quality_first(profile_relations, suffixes):
                        path = os.path.join(season_dir, f"{base_filename}{suffix}.strm")
                        url = build_proxy_url(base_url, "episode", str(episode.uuid), rel.stream_id)
                        try:
                            result = write_strm(path, url, dry_run)
                        except OSError:
                            errors += 1
                            continue
                        current_paths.add(path)
                        written_by_profile[profile.name] += 1
                        if result == "created":
                            created += 1
                        elif result == "updated":
                            updated += 1
                        else:
                            unchanged += 1

                        if not write_nfo:
                            continue
                        xml = build_nfo_xml("episodedetails", episode.tmdb_id, episode.imdb_id, rel.custom_properties)
                        if xml is None:
                            continue
                        sidecar = nfo_path(path)
                        try:
                            nfo_result = write_strm(sidecar, xml, dry_run)
                        except OSError:
                            errors += 1
                            continue
                        current_nfo_paths.add(sidecar)
                        if nfo_result in ("created", "updated"):
                            nfo_written += 1

            stale = self.store.get_strm_manifest(CONTENT_TYPE_EPISODE) - current_paths
            stale_nfo = self.store.get_strm_manifest(CONTENT_TYPE_EPISODE + "_nfo") - current_nfo_paths
            if dry_run:
                removed = len(stale)
                nfo_removed = len(stale_nfo)
            else:
                removed = remove_stale_files_in_library(stale, library_root, profile_dirs)
                nfo_removed = remove_stale_files_in_library(stale_nfo, library_root, profile_dirs)
                self.store.save_strm_manifest(CONTENT_TYPE_EPISODE, current_paths)
                self.store.save_strm_manifest(CONTENT_TYPE_EPISODE + "_nfo", current_nfo_paths)

            prefix = "Would: " if dry_run else ""
            msg = f"{prefix}{created} new, {updated} updated, {removed} removed ({len(series_seen)} series)."
            if nfo_written or nfo_removed:
                msg += f" {nfo_written} nfo written, {nfo_removed} nfo removed."
            if series_skipped_no_id:
                msg += f" {len(series_skipped_no_id)} skipped (no id)."
            if len(profiles) > 1:
                msg += " By profile: " + ", ".join(f"{name} {n}" for name, n in written_by_profile.items()) + "."
            if unrouted:
                msg += f" {unrouted} episode(s) matched no profile."
            if errors:
                msg += f" {errors} write error(s)."
            return {"status": "ok", "message": msg}
        finally:
            self.store.release_lock("generate_series_strm")

    def _delete_strm_files(self, settings):
        """Deletes every file and folder inside the configured Movies/Series
        .strm subfolders (every profile's, see strm_profiles; the subfolders
        themselves are kept, in case a media server has them mounted as its
        library root), plus the files this plugin wrote in the folder of a
        profile since removed from the settings, and clears the .strm
        manifest so the next Generate starts from a clean slate."""
        import shutil

        library_root = (settings.get("strm_library_path") or "").strip()
        if not library_root:
            return {"status": "error", "message": "Set 'Library root path' in [.STRM OUTPUT] first."}
        from .profiles import ProfileError

        try:
            profiles = self._profiles(settings)
        except ProfileError as exc:
            return {"status": "error", "message": f".STRM profiles: {exc}. Nothing was deleted."}
        subfolders = []
        for profile in profiles:
            for subfolder in (profile.movies_dir, profile.series_dir):
                if subfolder not in subfolders:
                    subfolders.append(subfolder)
        # A profile removed from the settings since the last Generate left
        # files behind that this plugin wrote. Only those files (known from
        # the manifests) go, never the whole folder: it is no longer ours.
        root = os.path.normpath(library_root)
        known_dirs = [os.path.join(root, sub) + os.sep for sub in subfolders]
        orphaned_files = [
            path
            for kind in (CONTENT_TYPE_MOVIE, CONTENT_TYPE_EPISODE, CONTENT_TYPE_MOVIE + "_nfo", CONTENT_TYPE_EPISODE + "_nfo")
            for path in self.store.get_strm_manifest(kind)
            if not any(os.path.normpath(path).startswith(d) for d in known_dirs)
            and os.path.exists(path)
        ]
        dry_run = bool(settings.get("dry_run", True))

        cleared = []
        for subfolder in subfolders:
            target = os.path.join(library_root, subfolder)
            if not os.path.isdir(target):
                continue
            entries = os.listdir(target)
            if not entries:
                continue
            if dry_run:
                cleared.append(f"{target} ({len(entries)} entries)")
                continue
            for entry in entries:
                entry_path = os.path.join(target, entry)
                if os.path.isdir(entry_path):
                    shutil.rmtree(entry_path)
                else:
                    os.remove(entry_path)
            cleared.append(target)

        if orphaned_files:
            if dry_run:
                cleared.append(f"{len(orphaned_files)} file(s) of removed profiles")
            else:
                from .strm import remove_stale_files_in_library

                remove_stale_files_in_library(orphaned_files, library_root)
                cleared.append(f"{len(orphaned_files)} file(s) of removed profiles")

        if not cleared:
            return {"status": "ok", "message": "Nothing to delete — no .strm folders found at the configured path."}
        if dry_run:
            return {
                "status": "ok",
                "message": f"Would clear: {', '.join(cleared)}. The .strm manifest would also be cleared. Turn Dry Run off to do it.",
            }

        self.store.save_strm_manifest(CONTENT_TYPE_MOVIE, [])
        self.store.save_strm_manifest(CONTENT_TYPE_EPISODE, [])
        self.store.save_strm_manifest(CONTENT_TYPE_MOVIE + "_nfo", [])
        self.store.save_strm_manifest(CONTENT_TYPE_EPISODE + "_nfo", [])

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
        relation sets, stop requests, run history, catalog stats) so the
        next Scan/Process starts completely from scratch. Does not touch
        the .strm manifest — that tracks what's really on disk, unrelated
        to which titles need re-deciding, and Generate needs it intact to
        keep telling a genuine orphan apart from a file it just wrote.
        Never touches Dispatcharr's own database or any real .strm file —
        pair with Delete .strm Files if you also want those gone."""
        dry_run = bool(settings.get("dry_run", True))
        counts = self.store.table_row_counts()
        total = sum(counts.values())
        if total == 0:
            return {"status": "ok", "message": "Nothing to reset — plugin state is already empty."}
        detail = ", ".join(f"{n} {table}" for table, n in counts.items() if n)
        if dry_run:
            return {
                "status": "ok",
                "message": f"Would reset: {detail}. Dispatcharr's own catalogue would stay untouched. Turn Dry Run off to do it.",
            }
        self.store.reset_all()
        return {
            "status": "ok",
            "message": f"Plugin state reset: {detail}. Dispatcharr's own catalogue is untouched.",
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
        plugin = Plugin()
        live = plugin._live_settings(settings) if scheduled else (settings or {})
        result = plugin.run(
            action, {},
            {"logger": logger, "settings": live, "scheduled": scheduled, "background": True},
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
