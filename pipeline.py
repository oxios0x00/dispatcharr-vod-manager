"""Scan/process orchestration: the background run loop that scans for
changed titles, selects and prunes them batch after batch from vod-probe's
measurements, and (optionally) generates .strm files at the end.

A mixin, not a standalone class: Plugin(PipelineMixin, ScheduleMixin) combines
this with plugin.py's own field/action definitions and generate/maintenance
logic into the single class Dispatcharr's loader expects. Methods here call
self._clean_movie_titles/self._generate_movie_strm/self._catalog_stats/etc.,
which live on Plugin itself — that resolves fine at runtime regardless of
which file defines which method, same as any other mixin.
"""
import time

CONTENT_TYPE_MOVIE = "movie"
CONTENT_TYPE_SERIES = "series"
CONTENT_TYPE_EPISODE = "episode"


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


def _system_timezone_name():
    """Dispatcharr's own configured system time zone (Settings > System),
    the same one it uses for its own scheduled tasks (core/scheduling.py)."""
    try:
        from core.models import CoreSettings

        return CoreSettings.get_system_time_zone() or "UTC"
    except Exception:
        return "UTC"


def _format_local_now():
    """Current time in Dispatcharr's configured system time zone, so a
    notification states unambiguously when its run actually happened —
    needed once more than one schedule/content type can finish close
    together and the notification list alone doesn't make that clear."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(_system_timezone_name())
    except Exception:
        tz = None
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S %Z").strip()


class _WaitingForMeasurements(Exception):
    """A title has a relation vod-probe has not measured yet."""


class PipelineMixin:
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
        running — without this, both would run to completion in parallel,
        each pinning a worker for the whole batch. held_since comes from
        Store.try_acquire_lock or lock_held_since; a lock left by a run
        that died clears itself after stale_after seconds."""
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
        "scan_and_process_both": (CONTENT_TYPE_MOVIE, CONTENT_TYPE_SERIES),
    }
    # A run renews its lock between batches; a lock that has been silent this
    # long belongs to a run that died with its worker.
    _PIPELINE_LOCK_STALE_SECONDS = 900

    @staticmethod
    def _pipeline_lock(content_type):
        return f"scan_and_process_{content_type}"

    def _enqueue_background(self, action, settings, scheduled):
        """Queue `action` on the Celery worker. Returns (task_id, None), or
        (None, error_result) when it cannot be queued. The task itself is
        registered in plugin.py (it needs the concrete Plugin class) —
        imported here, not at module load time, so this module doesn't need
        plugin.py to already be fully loaded."""
        from . import plugin as _plugin_module

        task_fn = getattr(_plugin_module, "_vod_manager_scheduled_run", None)
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

    def _start_background(self, action_id, settings, scheduled):
        content_types = self._BACKGROUND_ACTIONS[action_id]
        if isinstance(content_types, str):
            content_types = (content_types,)
        for content_type in content_types:
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
            # Holding the lock means no other run is working this queue, so
            # anything still in progress was cut short by a restart.
            recovered = self.store.requeue_in_progress(content_type)
            recovered_msg = (
                f"Recovered {recovered} {unit} left in progress by an interrupted run." if recovered else ""
            )
            # Titles that were waiting for vod-probe get another chance now.
            self.store.requeue_waiting(content_type)
            # A stop asked for while nothing ran must not cancel this run.
            self.store.clear_stop(content_type)
            # Only worth a line in the notification when it actually renamed
            # something — the skipped-count otherwise repeats unchanged on
            # every single run and crowds out the parts that vary.
            clean_result = clean(settings) if settings.get("auto_clean_titles") else None
            clean_msg = clean_result.get("message", "") if clean_result and clean_result.get("changed") else ""
            scan_msg = scan(settings).get("message", "")

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
            processed_msg = (
                f"Processed {totals['processed']} ({totals['errors']} err), "
                f"{'would prune' if dry_run else 'pruned'} {totals['pruned']}."
            )
            if stopped:
                stopped_msg = "Stopped on request; run again to carry on."
            elif stop_message and not queue_empty:
                stopped_msg = stop_message
            else:
                stopped_msg = ""
            counts = self.store.queue_counts(content_type)
            waiting_msg = f"{counts['waiting']} waiting on vod-probe." if counts["waiting"] else ""
            errored = counts["error"]
            # Full detail (why, how to retry) lives in docs/troubleshooting.md
            # "Titles in error" — repeating it here every run only pushed out
            # parts of the message that actually change.
            errored_msg = f"{errored} in error." if errored else ""
            generate_msg = generate(settings).get("message", "") if settings.get("auto_generate_strm") else ""
            self._catalog_stats(settings)
            # The bell notification clamps to 5 wrapped lines with no way to
            # expand (Mantine Text lineClamp) — put the outcome that matters
            # most first, background/context last, so a clamp only ever
            # hides the least important part.
            parts = [
                recovered_msg, processed_msg, stopped_msg, waiting_msg, errored_msg,
                clean_msg, scan_msg, generate_msg,
            ]
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
                message=f"[{_format_local_now()}] {message}",
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

    def _scan_and_process_both(self, settings):
        """Movies then Series, back to back in the same background run —
        the way to cover both from the plugin's single schedule slot (one
        cron, one action; see docs/scheduling.md) instead of only one of
        the two ever running automatically."""
        movies = self._scan_and_process(settings)
        series = self._scan_and_process_series(settings)
        status = "error" if "error" in (movies.get("status"), series.get("status")) else "ok"
        return {
            "status": status,
            "message": f"Movies: {movies.get('message', '')} || Series: {series.get('message', '')}",
        }

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
        from apps.vod.models import M3UMovieRelation

        scanned, enqueued = self._enqueue_changed(
            CONTENT_TYPE_MOVIE, M3UMovieRelation, "movie_id"
        )

        return {
            "status": "ok",
            "message": f"Scanned {scanned}, {enqueued} new/changed.",
        }

    # --- process ------------------------------------------------------------
    #
    # This plugin no longer measures anything. vod-probe writes each relation's
    # quality, languages and bitrate into its custom_properties; a title is
    # decided only once every one of its relations has an answer, so a relation
    # nobody has looked at yet is never mistaken for a loser. A title that is
    # not ready waits and is tried again at the next run.

    def _apply_prune(self, dry_run, delete_fn, remember_fn):
        """The only place a prune decision becomes real: a no-op when
        dry_run, otherwise deletes the losers and remembers what's left.
        Both movie and series processing go through this single choke
        point rather than each keeping its own `if dry_run` check, so a
        dry run can never end up doing one of the two writes but not
        the other."""
        if dry_run:
            return
        delete_fn()
        remember_fn()

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
            from .profiles import ProfileError, retention_languages

            target_qualities = _parse_csv_list(settings.get("target_qualities"))
            target_languages = _parse_csv_list(settings.get("target_languages"))
            try:
                target_languages = retention_languages(target_languages, self._profiles(settings))
            except ProfileError as exc:
                return {"status": "error", "message": f".STRM profiles: {exc}. Nothing was pruned."}
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
            self._apply_prune(
                dry_run, lambda: None,
                lambda: self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, set()),
            )
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

        self._apply_prune(
            dry_run,
            lambda: M3UMovieRelation.objects.filter(id__in=loser_ids).delete() if loser_ids else None,
            lambda: self.store.set_known_relation_ids(CONTENT_TYPE_MOVIE, movie_id, winner_ids),
        )
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
        from apps.vod.models import M3USeriesRelation

        scanned, enqueued = self._enqueue_changed(
            CONTENT_TYPE_SERIES, M3USeriesRelation, "series_id"
        )

        return {
            "status": "ok",
            "message": f"Scanned {scanned}, {enqueued} new/changed.",
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
            self._apply_prune(
                dry_run, lambda: None,
                lambda: self.store.set_known_relation_ids(CONTENT_TYPE_SERIES, series_id, set()),
            )
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

        self._apply_prune(
            dry_run,
            lambda: M3UEpisodeRelation.objects.filter(id__in=loser_ids).delete() if loser_ids else None,
            lambda: self.store.set_known_relation_ids(
                CONTENT_TYPE_SERIES, series_id, {r.id for r in series_relations}
            ),
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
