# Scheduling

The plugin can run one action on its own cron schedule, independent of Dispatcharr's refresh. It uses django-celery-beat directly, since Dispatcharr has no scheduling API for plugins. There is **one schedule slot**: one cron and one action.

## Setting it up

1. Fill in **Schedule (5-field cron)** (`minute hour day-of-month month day-of-week`) and **Scheduled action**. The cron runs in Dispatcharr's own system time zone (Settings > System), the same one Dispatcharr uses for its own scheduled tasks.
2. Click **[SCHEDULE] Apply**. This registers the schedule but does not run it.
3. **Restart Dispatcharr once.** A Celery worker only registers a plugin's scheduled task when it starts. Without the restart, Apply succeeds silently but the task never fires and Status stays on "last run: never".
4. Use **[SCHEDULE] Status** to see when it last ran.

Re-click Apply whenever you change the cron or action, or any setting the scheduled run should use: settings are snapshotted at Apply time, not read live.

## When to schedule it

Set the cron **a few minutes after Dispatcharr's VOD refresh**, and after vod-probe's own run, so that what the refresh brought in has been measured. A title vod-probe has not measured yet is not lost: it waits and is picked up at the next run. The same goes for a series Dispatcharr reloaded meanwhile: a reload erases the measurements vod-probe wrote on its episodes, and the series waits until vod-probe has redone them.

## Advice

Leave the cron empty during a first import or against a large catalogue, and schedule only once you trust the picks. Choose an action that matches what you want to automate; the list includes Scan + Process (movies or series), the individual steps, Clean Titles, and both Generate actions.

With **Run .strm generation automatically with Scan + Process** on, Generate also runs at the end of every Scan + Process, scheduled or a manual Run click. Generate still refuses if a queue is not empty, so on a large catalogue this may generate nothing until the queue is drained.
