# .strm files and Emby / Jellyfin

## Why

A player connected to Dispatcharr through the Xtream API gets one version per title and cannot choose another. Emby and Jellyfin can show several versions of one title if each is a separate file. **Generate Movie / Series .strm Files** writes one `.strm` per kept relation (every version your quality/language settings keep, by default more than one per tier — see [Concepts](concepts.md)), pinned to that exact relation through Dispatcharr's `/proxy/vod/<type>/<uuid>?stream_id=` endpoint, so every version is independently playable and goes through Dispatcharr (connection handling, no provider credentials in the files).

A `.strm` can only point at a title Dispatcharr has imported, since the URL needs the title's uuid in Dispatcharr's database.

## Setup

In the `[.STRM OUTPUT]` section set:

- **Dispatcharr base URL**: reachable from your media server (a LAN address, not `localhost`).
- **Library root path**: where the files are written, inside the Dispatcharr container (default `/data/strm`). Mount the same folder into Emby/Jellyfin.
- **Movies / Series subfolder names** (defaults `movies` and `series`).

## Naming

```
movies/Title (2021) [tmdbid-123]/Title (2021) [tmdbid-123] - 01 - 2160p.strm
movies/Title (2021) [tmdbid-123]/Title (2021) [tmdbid-123] - 02 - 1080p.strm
series/Show [tmdbid-456]/Season 01/Show [tmdbid-456] - S01E01 - 01 - 2160p.strm
```

A rank (`01`, `02`, ...) is always present, best quality first, even for a title with a single kept version (`- 01 - 1080p`) — so a title going from one version to two, or back, never renames the file it already had just because the count crossed that boundary. The name carries the quality only: two versions of the same tier differing by language or HDR are told apart by rank alone. A relation that could not be probed is named `unprobed` (or `v2`, `v3` when there are several).

The folder tag `[tmdbid-…]` or `[imdbid-…]` is optional (`strm_include_id_tag`); when it's on, it's repeated on every file name too, movies and episodes alike — the tag sits right after the title, matching the folder name exactly, before the ` - quality` part. Jellyfin only recognises several **movie** files as versions of one film when each file name starts character-for-character with the folder name, tag included; Emby doesn't need this but isn't bothered by it either. Episodes repeat the tag too, for consistency, though it isn't what makes episode grouping work in Jellyfin 12.0+ (released 2026-09-07): that groups by season + episode number within the same season folder, which our `S01E01` naming already gives it regardless of the tag — see [Limitations](limitations.md) for how new and untested that Jellyfin feature still is. `strm_require_id` skips titles with no TMDB/IMDB id entirely.

## Running it

**Generate refuses to run while its queue has `pending`, `in_progress` or `waiting` items**: generating early would name unmeasured titles `unprobed` and write files for relations that are about to be pruned. Finish Scan + Process first.

It is safe to re-run. Unchanged files are left alone (their modification date is preserved, so it does not trigger a full media-server rescan). A relation that is later pruned, or a title that disappears, has its file removed on the next run, along with folders left empty. Only files this plugin wrote are ever touched: the plugin keeps a manifest of what it wrote, so NFOs, posters and anything you added by hand stay.

## Which version Emby plays by default

Emby seems to keep the version it meets **first** as the default one, whatever the file names say. The plugin therefore writes the best quality first when it creates files, but this only affects files created from then on, and it is not guaranteed since Emby's rule is not documented. For a title that is already in Emby's library, delete its folder under `/data/strm/`, remove the item in Emby, run Generate again, then rescan.
