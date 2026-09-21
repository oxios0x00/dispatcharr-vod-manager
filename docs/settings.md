# Settings and actions

A setting you never saved uses its default: Dispatcharr fills unsaved fields with the defaults below when an action runs, so an empty-looking field is not necessarily empty.

## Selection

| Setting | Default | What it does |
|---|---|---|
| Dry run | ON | Decides but deletes nothing. Pruning is a real delete on Dispatcharr's side. |
| Qualities to keep (`target_qualities`) | `2160p,1080p` | One winner per listed tier the title has. Unlisted tiers are pruned; if no listed tier exists the best available is kept. |
| Exclude titles with none of the target qualities | OFF | ON drops a title entirely when none of its versions is in a quality you keep (for example a title that only exists in 720p when you keep `2160p,1080p`). OFF keeps its best available version. Turn Dry run ON first to see what it would remove. |
| Target languages (`target_languages`) | `fre,eng` | ISO 639-2 codes. Keeps the fewest versions whose audio covers these languages. |
| Exclude relations matching none of the target languages | OFF | ON drops a tier whose versions match no target language at all, which can leave a title with nothing. This concerns **languages only**; for quality, see the row under Qualities to keep. |

## Processing

| Setting | Default | What it does |
|---|---|---|
| Batch size (movies per batch) | 25 | Movies taken per batch. Scan + Process runs batches one after another; the size only sets how often progress and the Stop request are checked. |
| Batch size (series per batch) | 5 | Series per batch. One series can mean dozens of episodes. |

## Titles

| Setting | Default | What it does |
|---|---|---|
| Tags to strip | a list of common provider prefixes | One literal prefix per line, matched case-insensitively at the start of a title (`NF -`, `4K-AMZ -`, ...). Cosmetic only. Titles with no TMDB/IMDB id are skipped, since renaming them would make Dispatcharr's own matching create a duplicate. |
| Run title cleanup automatically with Scan + Process | OFF | Otherwise only the Clean Titles buttons run it. |

## `.strm` output

| Setting | Default | What it does |
|---|---|---|
| Dispatcharr base URL | empty | Baked into every `.strm`; must be reachable from your media server. |
| Library root path | `/data/strm` | Where files are written, inside the container. |
| Movies / Series subfolder | `movies` / `series` | Folder names under the root. |
| Run .strm generation automatically with Scan + Process | OFF | Only affects the scheduled Scan + Process, never a manual click. |
| Include `[tmdbid-…]` in the folder name | OFF | Tags the title folder for Emby/Jellyfin. Turning it on renames every tagged folder at the next Generate. |
| Skip titles with no TMDB/IMDB id | OFF | No `.strm` at all for those titles (and an existing one is removed). |

## Schedule

`Schedule (5-field cron)` (empty), `Schedule timezone` (empty, meaning UTC) and `Scheduled action` (Scan + Process): see [Scheduling](scheduling.md).

## Actions

- **[MOVIES] / [SERIES]**: Scan + Process (background run), Scan, Queue Status, Stop, Clean Titles, Generate .strm Files.
- **[MAINTENANCE]**: Catalog Stats (quality and language composition), Retry Errored Titles (puts titles that failed back in the queue; a failed title is otherwise retried only when its relations change), Delete .strm Files, Prune Orphaned State (deletes the plugin's own queue and known-relation rows for titles Dispatcharr has deleted; honours Dry Run, and never touches Dispatcharr's data or `.strm` files), Reset Plugin State (wipes the queues and history — the next scan starts from scratch; never touches Dispatcharr's own data).
- **[SCHEDULE]**: Apply, Remove, Status, Test Fire Now.

Scan + Process and both Generate actions refuse to start while an earlier run of the same action is still going.
