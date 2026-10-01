# .strm files and Emby / Jellyfin

## Why

A player connected to Dispatcharr through the Xtream API gets one version per title and cannot choose another. Emby and Jellyfin can show several versions of one title if each is a separate file. **Generate Movie / Series .strm Files** writes one `.strm` per kept relation (every version your quality/language settings keep, by default more than one per tier — see [Concepts](concepts.md)), pinned to that exact relation through Dispatcharr's `/proxy/vod/<type>/<uuid>?stream_id=` endpoint, so every version is independently playable and goes through Dispatcharr (connection handling, no provider credentials in the files).

A `.strm` can only point at a title Dispatcharr has imported, since the URL needs the title's uuid in Dispatcharr's database.

## Setup

In the `[.STRM OUTPUT]` section set:

- **Dispatcharr base URL**: reachable from your media server (a LAN address, not `localhost`).
- **Library root path**: where the files are written, inside the Dispatcharr container (default `/data/strm`). Mount the same folder into Emby/Jellyfin.
- **Movies / Series subfolder names** (defaults `movies` and `series`), unless **Language profiles** is filled: its lines then name the folders (see [Language profiles](#language-profiles)).

## Naming

```
movies/Title (2021) [tmdbid-123]/Title (2021) [tmdbid-123] - 01 - 2160p.strm
movies/Title (2021) [tmdbid-123]/Title (2021) [tmdbid-123] - 02 - 1080p.strm
series/Show [tmdbid-456]/Season 01/Show [tmdbid-456] - S01E01 - 01 - 2160p.strm
```

A rank (`01`, `02`, ...) is always present, best quality first, even for a title with a single kept version (`- 01 - 1080p`) — so a title going from one version to two, or back, never renames the file it already had just because the count crossed that boundary. The name carries the quality only: two versions of the same tier differing by language or HDR are told apart by rank alone. A relation that could not be probed is named `unprobed` (or `v2`, `v3` when there are several).

The folder tag `[tmdbid-…]` or `[imdbid-…]` is optional (`strm_include_id_tag`); when it's on, it's repeated on every file name too, movies and episodes alike — the tag sits right after the title, matching the folder name exactly, before the ` - quality` part. Jellyfin only recognises several **movie** files as versions of one film when each file name starts character-for-character with the folder name, tag included; Emby doesn't need this but isn't bothered by it either. Episodes repeat the tag too, for consistency, though it isn't what makes episode grouping work in Jellyfin 12.0+ (released 2026-09-07): that groups by season + episode number within the same season folder, which our `S01E01` naming already gives it regardless of the tag — see [Limitations](limitations.md) for how new and untested that Jellyfin feature still is. `strm_require_id` skips titles with no TMDB/IMDB id entirely.

## Language profiles

As far as we could find in their forums, Emby and Jellyfin do not choose between several versions of a film by the viewer's language: the version they propose first depends on what the device can play and on quality (Emby), or on resolution then file name (Jellyfin), not on the user's preferred audio language. To give each audience its own library, **Language profiles** (`strm_profiles`) writes the `.strm` files into several folders by audio language. Point one media-server library at each folder and restrict users to the libraries they need.

One profile per line, `name : languages : movies folder : series folder`, optionally followed by `: exclusif`:

```
arabe     : ara           : movies-ar : series-ar : exclusif
principal : all           : movies    : series
```

- **languages**: ISO 639-2 codes separated by commas (`ara,tur`), or `all` (also `*`), which matches every version, including ones vod-probe could not measure. A version matches a profile when it has at least one of the profile's languages.
- **Which profiles receive a version**: every profile it matches, except that a version matching an `exclusif` profile goes only to the first such profile (in line order) and nowhere else. Order therefore only matters between `exclusif` profiles.
- **A version matching no profile** is written nowhere, and the Generate message counts them. Add an `all` profile to catch the rest.
- Folder names are plain names under the library root; each profile needs its own movies and series folder. Lines starting with `#` are ignored. A malformed line stops Generate, Scan + Process and Delete before anything is written, pruned or deleted, and the message names the line.
- Ranks (`01`, `02`, ...) are numbered inside each profile's folder, so `01` is the best version that profile holds.
- Empty setting: one implicit `all` profile on the movies/series subfolder names above, i.e. the behaviour before profiles existed.

**`exclusif` is designed to be strongly excluding.** A file with 15 or 20 audio languages that includes the exclusive profile's language leaves every other folder, so the main library can end up without the best version of a film, or without the film at all if that was its only version. Series are routed episode by episode, so a show whose episodes differ in languages can be split across two folders. Use `exclusif` only when each audience really has its own library and nobody needs the other one's titles.

**What Scan + Process keeps** is the target languages plus every language named by a profile, so a version kept for a profile is never pruned for lacking a target language. This applies from the next processing of a title; versions already pruned earlier are not restored by it. With no target languages set, nothing is language-filtered, as before.

## Running it

**Generate refuses to run while its queue has `pending`, `in_progress` or `waiting` items**: generating early would name unmeasured titles `unprobed` and write files for relations that are about to be pruned. Finish Scan + Process first.

It is safe to re-run. Unchanged files are left alone (their modification date is preserved, so it does not trigger a full media-server rescan). A relation that is later pruned, or a title that disappears, has its file removed on the next run, along with folders left empty. Only files this plugin wrote are ever touched: the plugin keeps a manifest of what it wrote, so posters and anything you added by hand stay untouched.

## The `.nfo` sidecar

When **Also write a .nfo sidecar next to each .strm** (`strm_write_nfo`, off by default) is on, Generate also writes a Kodi-style `.nfo` next to each `.strm`, named `<the .strm file's own name>.nfo` (appended, never a plain extension swap — Jellyfin's own native NFO saver would otherwise collide with it on a title's 2nd+ version, since it computes exactly that swap for those). It carries the TMDB/IMDB id and the streamdetails vod-probe already measured — video, audio and, since vod-probe 1.2.0, subtitle tracks — so a future companion Emby/Jellyfin plugin can populate MediaInfo without ever probing the stream itself:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <tmdbid>631842</tmdbid>
  <imdbid>tt6791350</imdbid>
  <fileinfo>
    <streamdetails>
      <video>
        <codec>hevc</codec>
        <width>3840</width>
        <height>2160</height>
        <bitrate>15000000</bitrate>
        <framerate>23.976</framerate>
        <durationinseconds>7215.5</durationinseconds>
        <hdrtype>dolbyvision</hdrtype>
      </video>
      <audio>
        <codec>eac3</codec>
        <channels>6</channels>
        <language>fre</language>
        <bitrate>768000</bitrate>
      </audio>
      <audio>
        <codec>aac</codec>
        <channels>2</channels>
        <language>eng</language>
      </audio>
      <subtitle>
        <codec>subrip</codec>
        <language>fre</language>
      </subtitle>
      <subtitle>
        <codec>subrip</codec>
        <language>eng</language>
        <forced>true</forced>
      </subtitle>
      <subtitle>
        <codec>subrip</codec>
        <language>eng</language>
        <hearingimpaired>true</hearingimpaired>
      </subtitle>
    </streamdetails>
    <totalbitrate>18000000</totalbitrate>
    <size>2568945112</size>
    <container>matroska,webm</container>
  </fileinfo>
</movie>
```

An episode's `.nfo` uses `<episodedetails>` as its root instead, and never carries `<imdbid>` (Kodi only places that tag at the `<tvshow>` root, which this plugin never writes). `<forced>`/`<hearingimpaired>` are only present when the provider actually flags a track as such, and every tag is simply left out when vod-probe didn't measure the value (an audio track with no bitrate, a probe block from before `size` existed).

**`<durationinseconds>` is missing on most episodes by design.** vod-probe probes one episode per season and version, then gives its result to the others (status `inferred`) minus the duration, which belongs to one episode. Only individually probed episodes carry it; set vod-probe to probe every episode to get it everywhere, then run Generate again (it detects the changed content on its own). Movies are always probed individually. Nothing else is written — no title, plot or cast: Emby/Jellyfin fill those in on their own from the id. A relation vod-probe hasn't measured yet (or whose probe failed) gets its `.strm` but no `.nfo` — streamdetails are never fabricated. The `.nfo` is tracked in the same orphan-cleanup manifest as the `.strm`, so it disappears along with it when a title is pruned or Delete .strm Files runs.

## Which version Emby plays by default

Emby seems to keep the version it meets **first** as the default one, whatever the file names say. The plugin therefore writes the best quality first when it creates files, but this only affects files created from then on, and it is not guaranteed since Emby's rule is not documented. For a title that is already in Emby's library, delete its folder under `/data/strm/`, remove the item in Emby, run Generate again, then rescan.
