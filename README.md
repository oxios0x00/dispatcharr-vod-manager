# VOD Manager

Curates Dispatcharr's VOD catalogue automatically, for movies and series
alike: probes every relation (source) behind a duplicated title with
`ffprobe`, picks the winning relation(s) per configurable target
quality/language, and deletes the losing relations from Dispatcharr's own
database so its native Xtream API serves one clean version per title.
Optionally writes pinned `.strm` files so Emby/Jellyfin can show real,
distinct multi-version playback for a single title (see below).

## Status

Validated end to end against a real Dispatcharr instance, dry-run and
live: probing, quality-tier selection with bitrate tie-break, relation
pruning, title cleanup, scheduling, and `.strm` generation have all been
exercised for real on a live catalogue, not just unit-tested.

## Install

1. Clone or download this repo into Dispatcharr's plugins directory,
   named `vod_manager` (the folder name becomes the plugin key):
   ```
   /data/plugins/vod_manager/
   ```
   (`/data/plugins` is Dispatcharr's default; overridable via the
   `DISPATCHARR_PLUGINS_DIR` env var — check your compose file.)
2. In Dispatcharr → Plugins, enable **VOD Manager**.
3. `ffprobe` needs to be on `PATH` inside the Dispatcharr container.
   Dispatcharr's own base image is built on top of an ffmpeg image, so
   this should already be true — worth a quick
   `docker exec <container> which ffprobe` to confirm on your actual
   setup before the first real run.

## First run checklist

1. Leave **Dry run** ON (the default).
2. Set **Batch size** small (5-10) for the first pass.
3. Click **Scan Movies** — enqueues movies whose relation set is new.
4. Click **Process Batch** a few times, then **Queue Status** to check
   progress.
5. Inspect what it *would* prune (check Dispatcharr's own logs / the
   plugin's `state.sqlite3` `relation_probes` table) before trusting it.
6. Only turn **Dry run** OFF once the picks look right on a sample you've
   checked by hand — pruning deletes real `M3UMovieRelation` rows.
   Deletion, not a soft flag, is the only lever Dispatcharr's plugin API
   actually gives us for removing a relation from its own catalogue.

## .strm generation (Emby/Jellyfin, real multi-version playback)

Dispatcharr's native Xtream API always collapses a title's kept
relations down to whichever M3U account has the highest priority,
regardless of category — a pure Xtream client (TiviMate, etc.) can
never select a specific quality tier this way (verified with real
ffprobe testing against the raw provider stream). **Generate Movie/Series .strm
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
yourself whenever you want, or pick one as its own dedicated scheduled
action. **Run .strm generation automatically with Scan + Process** only
applies to the *scheduled* Scan + Process (cron or Test Fire Now) —
clicking **Run** manually never auto-generates, since Dispatcharr may
still be matching freshly-scanned content against providers, and writing
`.strm` files mid-match can produce bad or incomplete entries.

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
in Dispatcharr). Set **Schedule (5-field cron)**, **Schedule timezone**,
and **Scheduled action**, then click **Apply Schedule**.

**Important**: after installing or updating this plugin, restart
Dispatcharr once. A Celery worker only registers a plugin's scheduled
task at its own process startup (`worker_process_init` in
`dispatcharr/celery.py`) — without a restart, **Apply Schedule** succeeds
silently but the task never actually fires, and **Schedule Status** stays
stuck at "last run: never" forever. Use **Test Fire Schedule Now** after
a restart to confirm it's picked up before trusting the cron.

## Data location

State lives in `<plugin folder>/data/state.sqlite3` by default (override
with the `VOD_MANAGER_DATA_DIR` env var). It holds the probe cache, the
processing queue, and run history — nothing here is tracked by
Dispatcharr's own database or migrations.
