# VOD Manager

Curates Dispatcharr's VOD catalogue automatically, for movies and series alike: reads what the companion plugin **vod-probe** measured for every relation (source) behind a duplicated title, and keeps the ones that match your target quality and language settings — by default every matching version, or one winner per tier if you turn on more aggressive cleanup — deleting the rest from Dispatcharr's own database. Cleanup is optional: leave the quality and language settings empty to keep everything, or turn off Dry run only once you trust the picks. Optionally writes pinned `.strm` files so Emby/Jellyfin can show real, distinct multi-version playback for a single title.

## Status

Validated end to end against a real Dispatcharr instance, dry-run and live: probing, quality-tier selection with bitrate tie-break, relation pruning, title cleanup, scheduling, and `.strm` generation have all been exercised for real on a live catalogue, not just unit-tested.

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
