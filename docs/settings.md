# Settings and actions

A setting you never saved uses its default: Dispatcharr fills unsaved fields with the defaults below when an action runs, so an empty-looking field is not necessarily empty.

## Selection

| Setting | Default | What it does |
|---|---|---|
| Dry run | ON | Decides but deletes nothing. Pruning is a real delete on Dispatcharr's side. |
| Qualities to keep (`target_qualities`) | `2160p,1080p` | Tiers in scope. Empty means every tier the title has. A tier not listed is pruned; if none of the listed tiers exist, the best available one is kept instead. |
| Exclude titles with none of the target qualities | OFF | ON drops a title entirely when none of its versions is in a quality you keep (for example a title that only exists in 720p when you keep `2160p,1080p`). OFF keeps its best available version. Turn Dry run ON first to see what it would remove. |
| Target languages (`target_languages`) | `fre,eng` | ISO 639-2 codes. Empty means every version in scope is kept, language aside. |
| Exclude relations matching none of the target languages | OFF | ON drops a tier whose versions match no target language at all, which can leave a title with nothing. This concerns **languages only**; for quality, see the row under Qualities to keep. |
| Keep one version per quality tier | OFF | OFF (default): every version matching your quality/language settings is kept — cleanup only removes what falls outside them, so Emby/Jellyfin can still show every matching version. ON: only the smallest set of versions covering `target_languages` is kept per tier (ties go to the higher bitrate) — the original, more aggressive cleanup. Leave both quality and language settings empty to keep literally everything vod-probe measured. Beyond its effect on Emby/Jellyfin, turning this ON **and** targeting a single quality in `target_qualities` is also the only way to make a pure Xtream/IPTV player receive the version the plugin picked — see [Limitations](limitations.md) and [Concepts](concepts.md). With the default (OFF), or with several qualities targeted, more than one relation survives and Dispatcharr picks between them on its own. |

## Exclusions

| Setting | Default | What it does |
|---|---|---|
| Movies to exclude (`excluded_movie_tmdbids`) | empty | One TMDB id per line, optionally followed by a free comment (e.g. `603 wrong match`) — everything after the id is ignored. A listed movie is pruned entirely (subject to Dry run), bypassing quality/language settings and vod-probe measurement altogether: no waiting, no selection, every relation goes. |
| Series to exclude (`excluded_series_tmdbids`) | empty | Same format, matched against the series' own TMDB id (a separate namespace from movies). A series with no TMDB id can't be excluded this way. |

An excluded title's relations reappear, and get pruned again, the next time Dispatcharr's own VOD refresh re-imports them — same as any other pruned relation reappearing while its group stays enabled (see [Add or remove a group](add-or-remove-a-group.md)).

## Processing

| Setting | Default | What it does |
|---|---|---|
| Batch size (movies per batch) | 25 | Movies taken per batch. Scan + Process runs batches one after another; the size only sets how often progress and the Stop request are checked. |
| Batch size (series per batch) | 5 | Series per batch. One series can mean dozens of episodes. |

## Titles

| Setting | Default | What it does |
|---|---|---|
| Tags to strip | a list of common provider prefixes | One literal prefix per line, matched case-insensitively at the start of a title (`NF -`, `4K-AMZ -`, ...); the longest matching prefix wins and stacked tags are stripped repeatedly (up to 5 passes). Cosmetic only. Titles with no TMDB/IMDB id are skipped, since renaming them would make Dispatcharr's own matching create a duplicate. |
| Run title cleanup automatically with Scan + Process | OFF | Otherwise only the Clean Titles buttons run it. |

## `.strm` output

| Setting | Default | What it does |
|---|---|---|
| Dispatcharr base URL | empty | Baked into every `.strm`; must be reachable from your media server. |
| Library root path | `/data/strm` | Where files are written, inside the container. |
| Movies / Series subfolder | `movies` / `series` | Folder names under the root. |
| Run .strm generation automatically with Scan + Process | OFF | ON: Generate also runs at the end of every Scan + Process, manual click or scheduled. |
| Include `[tmdbid-…]` in the folder name | ON | Tags the title folder for Emby/Jellyfin; also repeated on every file name (movies and episodes) — needed for Jellyfin to group movie versions (episode grouping works differently, see [Limitations](limitations.md)). Flipping it either way renames every tagged folder, and every file inside it, at the next Generate. |
| Skip titles with no TMDB/IMDB id | ON | No `.strm` at all for those titles (and an existing one is removed) — some providers never expose an id for certain content, confirmed happening for entire series catalogues on at least one provider, and a media server has nothing reliable to identify the file by regardless of the title text. OFF: it still gets a `.strm`, named from the raw provider title text. |

## Schedule

`Schedule (5-field cron)` (empty), `Schedule timezone` (empty, meaning UTC) and `Scheduled action` (Scan + Process): see [Scheduling](scheduling.md).

## Actions

- **[MOVIES] / [SERIES]**: Scan + Process (background run), Scan, Queue Status, Stop, Clean Titles, Generate .strm Files.
- **[MOVIES + SERIES] Scan + Process**: runs the Movies pipeline to completion, then Series, in one background task — the way to cover both from the plugin's single schedule slot (see [Scheduling](scheduling.md)) instead of picking just one.
- **[MAINTENANCE]**: Catalog Stats (quality and language composition), Retry Errored Titles (puts titles that failed back in the queue; a failed title is otherwise retried only when its relations change), Delete .strm Files, Prune Orphaned State (deletes the plugin's own queue and known-relation rows for titles Dispatcharr has deleted; honours Dry Run, refuses while a batch is running, skips a content type with nothing queued, and never touches Dispatcharr's data or `.strm` files), Reset Plugin State (wipes the queues and history — the next scan starts from scratch; never touches Dispatcharr's own data).
- **[SCHEDULE]**: Apply, Remove, Status.

Scan + Process and both Generate actions refuse to start while an earlier run of the same action is still going.
