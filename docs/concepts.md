# Concepts

## Titles, relations and versions

Dispatcharr stores one **title** (a `Movie`, a `Series` and its `Episode`s) and, behind it, one or more **relations**: each relation is one entry of one provider group (category) for that title, with its own `stream_id`. Two groups offering the same film (say `AMAZON MOVIES 4K` and `FR - FILM 4K`) give one Movie with two relations — the two **versions** of the title. Titles are merged by TMDB id, or by exact name and year when the provider gives no id. Episodes work the same way: one Episode, one relation per stream.

Dispatcharr only imports the categories you **enabled** on the M3U account. A group that isn't enabled does not exist as far as Dispatcharr — and this plugin — is concerned.

## What Dispatcharr serves to a player

For an IPTV player (TiviMate and similar) Dispatcharr returns **one** version per movie or episode: it sorts the relations by the account's priority, then by id, and takes the first. The player cannot pick another version, and the choice has nothing to do with quality — with a single account it is simply the oldest relation.

That is why this plugin exists. It **reads** what vod-probe measured for every relation, **selects** the ones worth keeping, and (unless Dry run is on) **prunes** the others from Dispatcharr's database, so what remains — and therefore what the player is served — is the version you want.

## Measurements

This plugin measures nothing. The companion plugin **vod-probe** probes every relation once with `ffprobe` and writes the result into the relation itself (`custom_properties`): the quality tier (`2160p`, `1080p`, `720p`, `480p`, `sd`, taken from the real resolution, never from the provider's label), the audio languages, the overall bitrate and a status (`ok`, `inferred` for an episode that received the result of a sibling, `error` or `unreachable`). vod-probe also loads the episodes of a series and decides how many it probes.

VOD Manager decides a title only when **every** relation of it has an answer. A relation nobody has looked at yet is never treated as a loser: the title waits (`waiting` in Queue Status) and is tried again at the next Scan + Process. A relation vod-probe could not measure (`error`, `unreachable`) counts as a loser when another version was measured, and a title where none was measured is put in `error`. When Dispatcharr reloads a series it erases what vod-probe wrote on its episodes; those titles simply wait until vod-probe has measured them again.

A series version also carries its own summary status, separate from its episodes': `ok` once vod-probe has a full answer, `error` if every episode it sampled failed, `partial` if some seasons answered and others are confirmed dead, or `pending` while it's still being sampled. VOD Manager only moves on to per-episode selection once every series version of a title reads `ok`, `error` or `partial` — all three are final answers, only `pending` means keep waiting. This is what stops a series whose every episode is unreachable from waiting forever: without it, such a series would sit at `pending` indefinitely, indistinguishable from one vod-probe simply hadn't gotten to yet.

## Selection

For each title, every tier listed in `target_qualities` (default `2160p,1080p`) is in scope — an **empty** list means every tier the title has is in scope, with no quality filter at all. Within a tier, a version is kept when its audio covers one of `target_languages` (default `fre,eng`); an empty list means every version in the tier is kept, language aside.

**Keep one version per quality tier** (off by default) decides what happens to the versions that match, within one tier:

- **Off** (default): every matching version is kept. Cleanup only removes what falls outside your quality/language settings — duplicates in the tiers and languages you asked for are left alone, so Emby/Jellyfin can still show them all. Leave both `target_qualities` and `target_languages` empty to keep literally everything vod-probe measured.
- **On**: only the smallest set of versions whose audio tracks together cover `target_languages` is kept per tier (ties go to the higher bitrate) — the original, more aggressive cleanup, one winner per tier.

Two rules apply in both modes:

- A title is **never dropped for lacking a requested tier**: if `target_qualities` is non-empty and none of its tiers exist for that title, the best available tier is used instead — which is why an occasional `720p` remains when that is all a title has. Turning on **Exclude titles with none of the target qualities** drops such titles instead.
- A tier where nothing matches `target_languages` falls back to keeping everything in it (every version, or the single best-bitrate one with **Keep one version per quality tier** on), unless **Exclude relations matching none of the target languages** is on, which drops it instead.

Nothing in the selection currently considers the video codec or the HDR type. A 3840x2160 H.264 SDR file and an HEVC Dolby Vision file are both simply `2160p`.

## Exclusions

A title listed by TMDB id in **Movies to exclude** or **Series to exclude** (see [Settings](settings.md)) skips all of the above: no measurement is read, no quality/language check runs, every one of its relations is pruned outright — the same end state as a title with zero winners, just decided without needing an answer from vod-probe first.

## The queue

The plugin works through a queue. **Scan** puts a title in it when its set of relations is new or has changed; **Scan + Process** then takes it batch after batch (`batch_size` movies, `series_batch_size` series), selecting and pruning each title, until the queue is empty or you stop it. It runs in a background task, so the click returns at once; **Queue Status** tells you whether it is still running and how many titles remain.

## `.strm` files

Dispatcharr's own API only serves one version, so for Emby and Jellyfin the plugin writes a `.strm` file per kept relation, pinned to that exact relation through Dispatcharr's proxy. See [.strm files and Emby / Jellyfin](strm-and-emby.md).
