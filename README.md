# VOD Manager

Curates Dispatcharr's VOD catalogue automatically, for movies and series alike: probes every relation (source) behind a duplicated title with `ffprobe`, picks the winning relation(s) per configurable target quality/language, and deletes the losing relations from Dispatcharr's own database so its native Xtream API serves one clean version per title. Optionally writes pinned `.strm` files so Emby/Jellyfin can show real, distinct multi-version playback for a single title (see below).

## Status

Validated end to end against a real Dispatcharr instance, dry-run and live: probing, quality-tier selection with bitrate tie-break, relation pruning, title cleanup, scheduling, and `.strm` generation have all been exercised for real on a live catalogue, not just unit-tested.

## Install

1. Clone or download this repo into Dispatcharr's plugins directory, named `vod_manager` (the folder name becomes the plugin key):
   ```
   /data/plugins/vod_manager/
   ```
   (`/data/plugins` is Dispatcharr's default; overridable via the `DISPATCHARR_PLUGINS_DIR` env var — check your compose file.)
2. In Dispatcharr → Plugins, enable **VOD Manager**.
3. `ffprobe` needs to be on `PATH` inside the Dispatcharr container. Dispatcharr's own base image is built on top of an ffmpeg image, so this should already be true — worth a quick `docker exec <container> which ffprobe` to confirm on your actual setup before the first real run.

## First run checklist

1. Leave **Dry run** ON (the default).
2. Set **Batch size** small (5-10) for the first pass.
3. Click **Scan Movies** — enqueues movies whose relation set is new. Only needs to be clicked again once new content actually shows up; re-running it against an unchanged catalogue enqueues nothing.
4. Click **Process Batch** repeatedly, checking **Queue Status** between clicks, until it reads `pending=0 in_progress=0`. Each click only drains one batch (Batch size), not the whole queue — on a large catalogue this can take many clicks.
5. Inspect what it *would* prune (check Dispatcharr's own logs / the plugin's `state.sqlite3` `relation_probes` table) before trusting it.
6. Only turn **Dry run** OFF once the picks look right on a sample you've checked by hand — pruning deletes real `M3UMovieRelation` rows. Deletion, not a soft flag, is the only lever Dispatcharr's plugin API actually gives us for removing a relation from its own catalogue.
7. Once the queue is fully drained, run **Clean Titles**, then **Generate Movie .strm Files** — in that order. Cleaning titles after generating `.strm` files just means regenerating with different names right after; cleaning first avoids the redundant write.

Same sequence for series, with the `[SERIES]` actions and **Series Queue Status**.

## Retrying series Dispatcharr silently gave up on

Dispatcharr fetches a series' episode list lazily, on demand, and marks that relation `episodes_fetched` the moment the provider responds without an error — even if the response was empty. Nothing in Dispatcharr ever re-checks or clears that flag afterward, so a relation that hit an empty response on its one and only attempt (a transient provider glitch, rate limit, etc.) stays stuck at zero episodes forever, even once the provider's real data is complete. This is a genuine Dispatcharr limitation, not something this plugin causes — reported upstream as a distinct case from [Dispatcharr/Dispatcharr#556](https://github.com/Dispatcharr/Dispatcharr/issues/556) (that one's triggered by a crash during sync; this one is silent and error-free).

**[SERIES] Retry Empty Episode Fetches** finds relations stuck exactly that way (`episodes_fetched=true`, source still active) and re-queues their series for a fresh attempt on the next Process Series Batch. That covers relations with zero episodes, and also *partial* ones: a relation holding fewer episodes than another source of the same series is checked against the provider (one call per suspicious relation, at most 60 per click — click again for the rest) and reset only if the provider really lists more. Capped at 3 retries per relation so a title that's genuinely short on the provider's side doesn't get retried forever. Existing episode relations are never touched by the refetch: it only adds the missing ones, and anything already probed keeps its cached result. It's a manual action (or pick it as a dedicated scheduled action) — it doesn't run automatically as part of Scan + Process, since it's meant as an occasional sweep rather than something to check on every pass.

## .strm generation (Emby/Jellyfin, real multi-version playback)

Dispatcharr's native Xtream API always collapses a title's kept relations down to whichever M3U account has the highest priority, regardless of category — a pure Xtream client (TiviMate, etc.) can never select a specific quality tier this way (verified with real ffprobe testing against the raw provider stream). **Generate Movie/Series .strm Files** sidesteps this: each `.strm` is pinned to one exact relation via Dispatcharr's generic `/proxy/vod/<type>/<uuid>?stream_id=` endpoint (the same mechanism the `vod2mlib`/`emby-xtream` plugins use for movies), so every kept quality tier becomes a genuinely distinct, correctly-labelled (`Title - 01 - 2160p.strm`, `Title - 02 - 1080p.strm` — the rank prefix keeps an alphabetical file listing in quality order, since plain text sorts "1080p" before "2160p"; the best version is also written first, because Emby seems to keep the first version it meets as the default one — not guaranteed, and titles already in Emby's library may need to be re-scanned to pick up the change), and independently playable file. This does **not** help pure Xtream/IPTV clients — only media servers that read `.strm` files from disk.

Set **Dispatcharr base URL**, **Library root path**, and the two subfolder names first (`[.STRM OUTPUT]` section), then either click **Generate Movie .strm Files** / **Generate Series .strm Files** yourself whenever you want, or pick one as its own dedicated scheduled action. **Run .strm generation automatically with Scan + Process** only applies to the *scheduled* Scan + Process (cron or Test Fire Now) — clicking **Run** manually never auto-generates, since Dispatcharr may still be matching freshly-scanned content against providers, and writing `.strm` files mid-match can produce bad or incomplete entries.

Re-running is safe and cheap either way: unchanged files are left alone (mtime preserved, so it doesn't force a full media-server library rescan), new relations get a new file, and a relation that's later pruned (or a title that disappears entirely) has its `.strm` file(s) removed automatically on the next run — tracked by this plugin itself (a manifest of exactly what it wrote last time), not guessed from folder contents, so anything you added by hand (NFOs, posters, your own files) is never touched.

Both Generate actions **refuse to run at all** while their queue (Movies or Series) still has anything `pending` or `in_progress` — generating too early would give a still-unprobed title a `- unprobed` filename and write a file for a relation that's about to be pruned, both of which just get renamed/deleted again on the next Generate once processing catches up. Finish Process Batch (or Process Series Batch) down to `0 pending, 0 in_progress` first.

Process Batch, Scan + Process and both Generate actions (movies and series alike) also refuse to start a second time while an earlier click on that same action is still running — see [Known limitation: long batches can time out the browser](#known-limitation-long-batches-can-time-out-the-browser) below for why that matters. A lock left behind by a Dispatcharr restart mid-batch clears itself automatically after an hour, so this never needs a manual reset.

Two more settings, both OFF by default:

- **Include [tmdbid-####] / [imdbid-ttXXXXXXX] in the movie/series folder name** — appends the Jellyfin/Emby external-id tag to the movie or series folder (not the `.strm` files inside it — they'd all share the same id, so repeating it there would be pure redundancy) when the title has a TMDB or IMDB id, so the media server identifies it by id instead of guessing from text alone. Falls back to the plain title when neither id is known. Turning this on renames every existing tagged folder on the next Generate run — a deliberate, one-time, library-wide rename (the stale-file cleanup above removes the old paths automatically), not something to flip on a library your media server is actively serving without expecting that.
- **Skip titles with no TMDB/IMDB id** — some providers never expose either id for certain content (a whole series catalogue, in one confirmed real case), leaving a media server nothing reliable to identify that file by no matter how clean the title text is. Turn this on to skip generating (or remove an already-generated) `.strm` for those titles entirely instead of shipping one you know Emby/Jellyfin can't match properly.

## Scheduling (run automatically)

Uses django-celery-beat directly (no formal plugin scheduling API exists in Dispatcharr). Set **Schedule (5-field cron)**, **Schedule timezone**, and **Scheduled action**, then click **Apply Schedule**.

**Important**: after installing or updating this plugin, restart Dispatcharr once. A Celery worker only registers a plugin's scheduled task at its own process startup (`worker_process_init` in `dispatcharr/celery.py`) — without a restart, **Apply Schedule** succeeds silently but the task never actually fires, and **Schedule Status** stays stuck at "last run: never" forever. Use **Test Fire Schedule Now** after a restart to confirm it's picked up before trusting the cron.

**Warning**: leave **Schedule (5-field cron)** empty (the default) during a first import or against a large catalogue. Run Scan, Process Batch, Clean Titles and Generate manually and watch the results until the queues settle down — an unattended cron firing every few hours during that period gives you far less visibility into what's happening on a catalogue you haven't validated yet. Schedule it only once you trust the picks it's making on their own.

## Known limitation: long batches can time out the browser

Process Batch, Scan + Process and both Generate actions run synchronously inside Dispatcharr's own web request/response cycle — the click blocks until the whole batch is done, there's no background-task version yet. Dispatcharr's own nginx/uwsgi timeouts are generous (5-10 minutes), but a large batch (or one hitting several dead streams — each `ffprobe` call can take up to its own 25s timeout) can still exceed that. When it does, the browser shows a timeout error even though the batch keeps running and completes correctly server-side — check Queue Status (or Series Queue Status) a minute later and it'll show the real, up-to-date counts regardless of what the browser displayed. Re-clicking because of that error is safe (see above, it'll just say the previous one is still running) but doesn't make it finish any faster. If this happens often, lower Batch size (or Series batch size) until a batch reliably finishes within the timeout window.

## Data location

State lives in `<plugin folder>/data/state.sqlite3` by default (override with the `VOD_MANAGER_DATA_DIR` env var). It holds the probe cache, the processing queue, and run history — nothing here is tracked by Dispatcharr's own database or migrations.

## License

MIT — see [LICENSE](LICENSE).
