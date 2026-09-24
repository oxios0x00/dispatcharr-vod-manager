"""Scheduling: registers the plugin's own django-celery-beat PeriodicTask,
since Dispatcharr has no scheduling API for plugins (verified in
apps/plugins/loader.py — no schedule-related method). Dispatcharr itself
runs on Celery + django-celery-beat internally (M3U/EPG refresh, backups),
so a plugin can register its own PeriodicTask directly.

A mixin, combined into Plugin alongside PipelineMixin — see pipeline.py's
module docstring for why.
"""
import json

from .pipeline import _system_timezone_name


class ScheduleMixin:
    # Names for the django-celery-beat PeriodicTask and the Celery task it
    # points at.
    SCHEDULE_TASK_NAME = "vod_manager.auto_run"
    SCHEDULED_TASK_CELERY_NAME = "vod_manager.scheduled_run"

    # Settings are snapshotted into the PeriodicTask's kwargs at Apply time,
    # not read live — re-click Apply after changing any other setting to
    # refresh what the scheduled run actually uses.
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
        # Same time zone Dispatcharr's own scheduled tasks use
        # (core/scheduling.py) — Settings > System, not a plugin setting.
        tz_str = _system_timezone_name()

        if target not in self._VALID_SCHEDULE_TARGETS:
            return {"status": "error", "message": f"Invalid schedule_target: {target}"}

        try:
            minute, hour, dom, month, dow = self._parse_cron(cron_expr)
        except ValueError as e:
            return {"status": "error", "message": str(e)}

        try:
            from django_celery_beat.models import PeriodicTask, CrontabSchedule
        except ImportError as e:
            return {
                "status": "error",
                "message": f"django-celery-beat not available ({e}); scheduling requires it.",
            }

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
