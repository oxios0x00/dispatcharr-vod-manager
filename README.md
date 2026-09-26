# VOD Manager

[![Tests](https://github.com/oxios0x00/dispatcharr-vod-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/oxios0x00/dispatcharr-vod-manager/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/oxios0x00/dispatcharr-vod-manager)](../../releases/latest)
[![License](https://img.shields.io/github/license/oxios0x00/dispatcharr-vod-manager)](LICENSE)
[![Dispatcharr](https://img.shields.io/badge/dispatcharr-%E2%89%A50.31.0-blue.svg)](https://github.com/Dispatcharr/Dispatcharr/releases/tag/v0.31.0)

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that curates your VOD catalogue: it decides which version of each movie or episode is worth keeping, prunes the rest, and can write `.strm` files so Emby/Jellyfin show true multi-version playback — something Dispatcharr's own API can't do on its own.

## The problem

An IPTV provider group is rarely one clean version per title. The same movie often shows up in several categories — `4K MOVIES`, `FR MOVIES`, a bundle group — each a separate relation in Dispatcharr, sometimes a real 2160p remux, sometimes a re-encoded 480p file mislabelled as `[4K]`. Dispatcharr merges these into one title but, for a player using its Xtream API, serves back just **one** relation per title: the oldest one in its database, priority order aside — nothing to do with actual quality.

VOD Manager fixes what your catalogue actually contains, not just what one player happens to be handed.

## How it works

VOD Manager doesn't probe anything itself. It works alongside a companion plugin, **[vod-probe](https://github.com/oxios0x00/dispatcharr-vod-probe)**, which runs `ffprobe` once per relation and writes the real resolution, audio languages, bitrate and HDR/codec info into it. VOD Manager reads those results, decides which relation(s) of each title match your quality/language settings, and (unless Dry run is on) deletes the rest from Dispatcharr's own database:

```
provider groups  →  Dispatcharr (relations)  →  vod-probe (measures each one)  →  VOD Manager (decides, prunes, writes .strm)
```

Splitting the two means the expensive part (`ffprobe`, one pass per relation) only ever runs once, and VOD Manager's own job — selection, pruning, `.strm` generation — is pure database work, safe to re-run as often as you like.

## Features

- **Selection** — keep every version matching your quality/language settings (default), or just the single best per quality tier. Never drops a title just for lacking your preferred quality: the best it has is kept instead.
- **Exclusions** — permanently exclude specific titles by TMDB id, regardless of the settings above (a wrong TMDB match, a title you never want probed or pruned).
- **Title cleanup** — strips junk provider prefixes (`NF -`, `4K-AMZ -`, ...) from titles, optionally. Cosmetic only, never affects selection.
- **`.strm` generation** — writes one file per kept relation, pinned directly to it via Dispatcharr's own proxy (no provider credentials in the files), so Emby/Jellyfin can show every kept version as a separate, independently playable file.
- **Scheduling** — runs one action on its own cron, independent of Dispatcharr's own refresh — including a combined "Movies then Series" action, since the plugin only gets one schedule slot.
- **Background, resumable runs** — Scan + Process works through a queue batch by batch, survives a restart (in-progress titles are simply requeued), and picks up exactly where it left off.

Everything defaults to the safe choice: Dry run is ON, cleanup keeps every matching version, and nothing is scheduled until you set it up. See [Settings and actions](docs/settings.md) for every option, or [Concepts](docs/concepts.md) for how it all fits together.

## Requirements

- Dispatcharr, with an M3U/Xtream account that has VOD categories enabled.
- [vod-probe](https://github.com/oxios0x00/dispatcharr-vod-probe) installed and enabled — VOD Manager decides purely from what vod-probe has already measured, and a title vod-probe hasn't looked at yet simply waits.

## Install

1. Download `vod_manager.zip` from the [latest release](../../releases/latest) and extract it into Dispatcharr's plugins directory (`/data/plugins` by default, overridable with the `DISPATCHARR_PLUGINS_DIR` env var) — it extracts as `vod_manager/`, the folder name Dispatcharr uses as the plugin key. Cloning the repo works too, but pulls in tests, docs and CI config the plugin doesn't need at runtime.
2. In Dispatcharr → Plugins, enable **VOD Manager**.
3. Install and enable **vod-probe** as well, and let it measure your catalogue first. VOD Manager does not run `ffprobe` itself: it decides from the quality, languages and bitrate that vod-probe writes into each relation. A title whose relations vod-probe has not measured yet waits for the next run.

After installing or updating the plugin, restart Dispatcharr once.

## Quick start

Keep **Dry run** ON and the batch size small, then: **Scan + Process** (it runs in the background until the queue is empty; follow it with **Queue Status**), check the picks, turn Dry run OFF, **Clean Titles**, **Generate .strm Files**. Same for series with the `[SERIES]` actions, or both together with `[MOVIES + SERIES] Scan + Process` — handy since the plugin's schedule only has one slot (see [Scheduling](docs/scheduling.md)). The full walkthrough is in [First import](docs/first-import.md).

## Documentation

- [Concepts](docs/concepts.md) — titles, relations, measurements, selection, exclusions, the queue.
- [First import](docs/first-import.md) — the order to follow on a fresh catalogue.
- [Adding or removing a provider group](docs/add-or-remove-a-group.md)
- [.strm files and Emby / Jellyfin](docs/strm-and-emby.md)
- [Scheduling](docs/scheduling.md)
- [Settings and actions](docs/settings.md) — every setting with its default.
- [Troubleshooting](docs/troubleshooting.md)
- [Known limitations](docs/limitations.md)

## Data location

State lives in `vod_manager_data/state.sqlite3`, a sibling of this plugin's own folder under Dispatcharr's plugins directory (override with the `VOD_MANAGER_DATA_DIR` env var). It holds the processing queue, the known relations, the `.strm` manifest and the run history — nothing here is tracked by Dispatcharr's own database or migrations. Deliberately kept outside the plugin's own folder: updating a plugin replaces that folder entirely, and Dispatcharr has no separate persistent-data location for a plugin — a sibling folder survives an update untouched, a subfolder wouldn't.

## License

MIT — see [LICENSE](LICENSE).
