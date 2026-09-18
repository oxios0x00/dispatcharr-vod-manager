# VOD Manager — prototype (Films only)

Curates Dispatcharr's VOD catalogue automatically: probes every relation
(source) behind a duplicated movie with `ffprobe`, picks the winning
relation(s) per configurable target quality/language, and deletes the
losing relations from Dispatcharr's own database so its native Xtream API
serves one clean version per title. See `../../NOTES.md` in the project
root for the full design trail and the reasoning behind every choice
below.

## Status

Validated end to end against a real Dispatcharr instance, dry-run and
live (see NOTES.md points 9, 11, 13, 14): probing, quality-tier selection
with bitrate tie-break, relation pruning, title cleanup, and scheduling
have all been exercised for real, not just unit-tested. `.strm`
generation (see below) is new and unit-tested only so far — not yet
run against a real Dispatcharr instance.

## Install

1. Copy this folder as-is into Dispatcharr's plugins directory as
   `vod_manager` (the folder name becomes the plugin key):
   ```
   /data/plugins/vod_manager/
   ```
   (`/data/plugins` is Dispatcharr's default; overridable via the
   `DISPATCHARR_PLUGINS_DIR` env var — check your compose file.)
2. In Dispatcharr → Plugins, enable **VOD Manager**.
3. `ffprobe` needs to be on `PATH` inside the Dispatcharr container.
   Dispatcharr's own base image is built `FROM
   lscr.io/linuxserver/ffmpeg:...` (confirmed in
   `reference/Dispatcharr/docker/DispatcharrBase`), so this should already
   be true — worth a quick `docker exec <container> which ffprobe` to
   confirm on your actual setup before the first real run.

## First run checklist

1. Leave **Dry run** ON (the default).
2. Set **Batch size** small (5-10) for the first pass.
3. Click **Scan Movies** — enqueues movies whose relation set is new.
4. Click **Process Batch** a few times, then **Queue Status** to check
   progress.
5. Inspect what it *would* prune (check Dispatcharr's own logs / the
   plugin's `state.sqlite3` `relation_probes` table) before trusting it.
6. Only turn **Dry run** OFF once the picks look right on a sample you've
   checked by hand — pruning deletes real `M3UMovieRelation` rows (see
   NOTES.md point 5 on why deletion, not a soft flag, is the only lever
   Dispatcharr's plugin API actually gives us).

## .strm generation (Emby/Jellyfin, real multi-version playback)

Dispatcharr's native Xtream API always collapses a title's kept
relations down to whichever M3U account has the highest priority,
regardless of category — a pure Xtream client (TiviMate, etc.) can
never select a specific quality tier this way (verified with real
ffprobe testing — see NOTES.md point 25). **Generate Movie/Series .strm
Files** sidesteps this: each `.strm` is pinned to one exact relation via
Dispatcharr's generic `/proxy/vod/<type>/<uuid>?stream_id=` endpoint
(the same mechanism the `vod2mlib`/`emby-xtream` plugins use for
movies), so every kept quality tier becomes a genuinely distinct,
correctly-labelled (`Title - 01 - 2160p.strm`, `Title - 02 - 1080p.strm`
— the rank prefix keeps Emby/Jellyfin's alphabetical sort in quality
order, since plain text sorts "1080p" before "2160p"), and
independently playable file. This does **not** help pure Xtream/IPTV
clients — only media servers that read `.strm` files from disk.

Set **Dispatcharr base URL**, **Library root path**, and the two
subfolder names first (`[.STRM OUTPUT]` section), then either click
**Generate Movie .strm Files** / **Generate Series .strm Files**
yourself whenever you want, turn on **Run .strm generation automatically
with Scan + Process** to fold it into the regular cycle, or pick one as
its own dedicated scheduled action. All three call the same code, so
pick whichever fits your workflow.

Re-running is safe and cheap either way: unchanged files are left alone
(mtime preserved, so it doesn't force a full media-server library
rescan), new relations get a new file, and a relation that's later
pruned (or a title that disappears entirely) has its `.strm` file(s)
removed automatically on the next run — tracked by this plugin itself
(a manifest of exactly what it wrote last time), not guessed from
folder contents, so anything you added by hand (NFOs, posters, your own
files) is never touched.

## Scheduling (run automatically)

Uses django-celery-beat directly (no formal plugin scheduling API exists
in Dispatcharr — see NOTES.md point 15). Set **Schedule (5-field cron)**,
**Schedule timezone**, and **Scheduled action**, then click **Apply
Schedule**.

**Important**: after installing or updating this plugin, restart
Dispatcharr once. A Celery worker only registers a plugin's scheduled
task at its own process startup (`worker_process_init` in
`dispatcharr/celery.py`) — without a restart, **Apply Schedule** succeeds
silently but the task never actually fires, and **Schedule Status** stays
stuck at "last run: never" forever. Use **Test Fire Schedule Now** after
a restart to confirm it's picked up before trusting the cron.

## Data location

State lives in `<plugin folder>/../vod_manager_data/state.sqlite3` by
default (override with the `VOD_MANAGER_DATA_DIR` env var). It holds the
probe cache, the processing queue, and run history — nothing here is
tracked by Dispatcharr's own database or migrations (see NOTES.md
point 4 for why).
