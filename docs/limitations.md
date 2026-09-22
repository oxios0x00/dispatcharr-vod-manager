# Known limitations

**Imposed by Dispatcharr**

- A player connected through the Xtream API is served **one version** per title, and cannot choose. Multiple versions only exist for media servers reading `.strm` files. This pick is deterministic, not random: Dispatcharr orders a title's surviving relations by M3U account priority, then by relation id — so, all else equal, the *oldest* relation in its database, unrelated to quality. `.strm` files are never affected by this ordering, since each one is pinned to one exact relation via its uuid + `stream_id`. Forcing the Xtream pick to match the plugin's choice needs **Keep one version per quality tier** ON *and* a single quality targeted (see [Settings](settings.md)) — that is the only configuration where exactly one relation survives per title. Otherwise (the default, or several qualities targeted) more than one relation survives and Dispatcharr chooses between them on its own, regardless of this plugin's opinion.
- Only **enabled** categories are imported. Content in a disabled group does not exist for the plugin, and a `.strm` can only point at a title Dispatcharr has imported.
- No automatic switch to another version if a VOD stream fails (that exists for live TV only).
- Plugin actions run inside the web request unless the plugin moves them to a background task: Scan + Process and the two Generate actions do, but Clean Titles and the maintenance actions do not, so a very long one can show a browser timeout while it completes anyway.
- Plugins can only show fixed fields, buttons and a notification: no lists, tables or downloads. Anything long has to be written to a file.
- One plugin schedule slot: one cron, one action.
- Dispatcharr's episode-fetch flag can freeze an empty or incomplete episode list; vod-probe reloads such series (**Reload Incomplete Series**), and until it has, the series waits.

**In the plugin**

- Selection looks at resolution, language and bitrate only. Codec (H.264/H.265) and HDR type (Dolby Vision, HDR10, SDR) are measured by vod-probe, but not used to choose.
- The bitrate used to break a tie is the overall bitrate of the file as vod-probe measures it (the video stream often has none of its own); a stream without any leaves the tie-break useless.
- VOD Manager needs vod-probe: without its measurements every title waits and nothing is pruned.
- `.strm` file names carry the quality only, not the language or HDR.
- Jellyfin 12.0 (released 2026-09-07) added a version selector for episodes, matching the one movies already had (jellyfin/jellyfin#16828, after two earlier attempts stalled — #8004 and #16239). It groups by season + episode number within the same season folder, not by matching a repeated tag, so this plugin's episode `.strm` naming should already qualify — untested on our side, and the feature is only a couple of weeks old with already-filed bugs around episode grouping (jellyfin/jellyfin#17885, #18116), so treat it as unproven rather than solved. On any Jellyfin older than 12.0, several files for one episode still show as duplicate episodes, not a version picker.
- An audio track without a declared language (`und`) never counts towards a target language.
- Language codes are compared as written: a track tagged `fra` does not count as `fre`, nor `deu` as `ger` (the two spellings of one language in ISO 639-2). Two-letter tags (`fr`, `en`) and different capitalisation (`FRE`) are not recognised either. A track tagged any other way than your setting is simply not recognised as your target language.
- Excluding a title by TMDB id (see [Settings](settings.md)) only works for a title that has one — a series with no TMDB id can't be excluded this way. Quality settings never exclude a tier that a title has no alternative to.
- Cache and queue rows of titles Dispatcharr later deleted are not removed automatically; run **[MAINTENANCE] Prune Orphaned State** now and then (harmless meanwhile, they only take space).
- A restart in the middle of a Scan + Process leaves its lock for up to 15 minutes; see [Troubleshooting](troubleshooting.md).
- The default version Emby proposes seems to follow the order the `.strm` files were written, which the plugin arranges best-quality first. A title that already has a 1080p file and gains a 2160p one later will therefore list the 1080p first. Emby's exact rule is unknown and the plugin has no control over it.
