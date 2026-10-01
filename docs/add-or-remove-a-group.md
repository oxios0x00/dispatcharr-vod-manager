# Adding or removing a provider group

Groups are managed in Dispatcharr, not in this plugin. The plugin reacts to what Dispatcharr has imported.

## Adding a group

1. **Enable the group** on the M3U account (VOD group management, movie or series categories).
2. **Run a VOD refresh of the account** so Dispatcharr imports its titles. Titles already known (same TMDB id) get one more relation; new titles are created.
3. **Click Scan + Process** (movies) or the `[SERIES]` equivalent. A title that gained a relation is put back in the queue; it waits until vod-probe has measured the **new** relation, then the selection runs again over all the versions.
4. **Generate `.strm` files** once the queue is empty.

Whether a new, better version replaces the old one depends on your settings: with the default `2160p,1080p` and **Keep one version per quality tier** off, both tiers are kept in full — the new relation adds to what is already there rather than replacing it. Turn that setting on for the old one-winner-per-tier behaviour, and a tier that isn't listed is pruned either way as soon as a listed tier exists.

## Removing a group

1. **Disable the group** on the M3U account.
2. **Run a VOD refresh of the account** (manual, or wait for the scheduled one). The refresh skips disabled categories, then deletes every relation it did not see again, and every movie or series left with no relation in any other group. Episodes go with their series.
3. **Click Generate Movie / Series .strm Files.** It compares against the manifest of what it wrote last time and deletes the orphaned `.strm` files and the folders left empty. The plugin never does this by itself: without this click the files stay on disk. Generate refuses to run while a queue still has `pending` or `in_progress` items, so finish Scan + Process first.
4. **Scan the library in Emby/Jellyfin** so it drops the items whose files are gone.

What survives: a title also present in another enabled group keeps that group's versions. Only the removed group's `.strm` files go, and a remaining file's name can change (`- 02 - 1080p` becomes `- 1080p` once it is the only version left).

Good to know: the deletion is permanent on Dispatcharr's side. Re-enabling the group brings everything back at the next refresh, but vod-probe has to measure all of it again. The plugin's own queue rows for the removed titles stay in `state.sqlite3` until you run **[MAINTENANCE] Prune Orphaned State** (with Dry Run on first to see how many).
