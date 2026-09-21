# Known limitations

**Imposed by Dispatcharr**

- A player connected through the Xtream API is served **one version** per title, and cannot choose. Multiple versions only exist for media servers reading `.strm` files.
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
- An audio track without a declared language (`und`) never counts towards a target language.
- Language codes are compared as written: a track tagged `fra` does not count as `fre`, nor `deu` as `ger` (the two spellings of one language in ISO 639-2). Two-letter tags (`fr`, `en`) and different capitalisation (`FRE`) are not recognised either. A track tagged any other way than your setting is simply not recognised as your target language.
- No way to permanently exclude one title, and quality settings never exclude a tier that a title has no alternative to.
- Cache and queue rows of titles Dispatcharr later deleted are not removed automatically; run **[MAINTENANCE] Prune Orphaned State** now and then (harmless meanwhile, they only take space).
- A restart in the middle of a Scan + Process leaves its lock for up to 15 minutes; see [Troubleshooting](troubleshooting.md).
- The default version Emby proposes seems to follow the order the `.strm` files were written, which the plugin arranges best-quality first. A title that already has a 1080p file and gains a 2160p one later will therefore list the 1080p first. Emby's exact rule is unknown and the plugin has no control over it.
