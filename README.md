# VOD Manager

Curates Dispatcharr's VOD catalogue for movies and series: reads the companion plugin **vod-probe**'s per-relation quality/language/bitrate measurements, decides which version(s) of each title to keep, and prunes the rest from Dispatcharr's own database.

- **Selection** — keep every version matching your quality/language settings (default), or just the single best per quality tier.
- **Exclusions** — permanently exclude specific titles by TMDB id, regardless of the settings above.
- **Title cleanup** — optionally strips junk provider prefixes from titles (cosmetic only, never affects selection).
- **`.strm` generation** — optionally writes one file per kept relation, pinned directly to it, so Emby/Jellyfin can show true multi-version playback (Dispatcharr's own API only ever serves one version per title to a player).
- **Scheduling** — optionally runs one action on its own cron, independent of Dispatcharr's own refresh.

Everything defaults to the safe choice: Dry run is ON, cleanup keeps every matching version, and nothing is scheduled until you set it up. See [Settings and actions](docs/settings.md) for every option, or [Concepts](docs/concepts.md) for how it all fits together.

## Status

Exercised against a real Dispatcharr test instance, not just unit-tested: vod-probe-driven selection, per-title exclusion, and `.strm` generation (including the Jellyfin-compatible naming) have all been run for real, dry-run and with actual file writes. Live (destructive) pruning on this vod-probe-based architecture specifically has only been dry-run so far — validate with Dry run ON on your own catalogue before turning it off, as [First import](docs/first-import.md) walks through.

## Install

1. Clone or download this repo into Dispatcharr's plugins directory (`/data/plugins` by default, overridable with the `DISPATCHARR_PLUGINS_DIR` env var). The folder name becomes the plugin key, e.g. `/data/plugins/vod_manager/`.
2. In Dispatcharr → Plugins, enable **VOD Manager**.
3. Install and enable **vod-probe** as well, and let it measure your catalogue first. VOD Manager does not run `ffprobe` itself: it decides from the quality, languages and bitrate that vod-probe writes into each relation. A title whose relations vod-probe has not measured yet waits for the next run.

After installing or updating the plugin, restart Dispatcharr once.

## Quick start

Keep **Dry run** ON and the batch size small, then: **Scan + Process** (it runs in the background until the queue is empty; follow it with **Queue Status**), check the picks, turn Dry run OFF, **Clean Titles**, **Generate .strm Files**. Same for series with the `[SERIES]` actions. The full walkthrough is in [First import](docs/first-import.md).

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

State lives in `<plugin folder>/data/state.sqlite3` by default (override with the `VOD_MANAGER_DATA_DIR` env var). It holds the processing queue, the known relations, the `.strm` manifest and the run history — nothing here is tracked by Dispatcharr's own database or migrations.

## License

MIT — see [LICENSE](LICENSE).
