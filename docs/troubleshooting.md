# Troubleshooting

## How do I know Scan + Process is still running?

Scan + Process runs in a background task, so the click only says it started. Click **Queue Status**: while it works the message starts with `[RUNNING, last activity Ns ago]`, followed by the counts of `pending`, `in_progress`, `done` and `error` titles. When it ends, a notification with the totals pops up in Dispatcharr (and stays in the bell until dismissed), and the log gets a `Scan + Process ... finished` line; the notification turns orange with "stopped" if you stopped the run. To stop it, click **Stop**: it finishes the current batch and ends; nothing stays blocked, so you carry on by running Scan + Process again.

**Generate** runs in the background as well: the click returns at once (after checking that the queue is drained and the settings are filled in) and a notification appears when the files are written. Generating tens of thousands of files takes several minutes, which used to end in a 504.

## "Already running", and nothing is running

A run renews its lock after every batch. If Dispatcharr is restarted in the middle of one, the lock clears itself after 15 minutes without activity, and the titles it had reserved are put back in the queue the next time Scan + Process starts. Nothing has to be cleaned by hand; wait a quarter of an hour after a restart before starting it again.

## Generate refuses to run

Its queue still has `pending` or `in_progress` items. Let Scan + Process finish (or wait for it) until Queue Status reads `0 pending, 0 in progress`. Titles in `error` do not block it. This guard exists because generating while the queue is still open would give still-unmeasured titles a `- unprobed` filename and write a file for a relation that's about to be pruned, only for it to disappear on the next Generate run.

## Titles stay `waiting`

Queue Status shows `waiting=N` and Scan + Process ends with "N titles waiting for vod-probe". Those titles have at least one relation vod-probe has not measured. Check that vod-probe is installed, enabled and has run over the catalogue, then run Scan + Process again: each run gives waiting titles another chance. After Dispatcharr reloads a series (the interface does it when a series is opened after 24 hours) the measurements of its episodes are erased and the series waits until vod-probe has redone them.

A series can also wait on its own summary status rather than its episodes: vod-probe marks a series version `pending` while still sampling it, and `ok`/`error`/`partial` once it's done, however that turned out (see [Concepts](concepts.md)). A series stuck at `pending` for a long time even though vod-probe reports nothing left to do is usually one whose every episode failed to probe — ask vod-probe to retry it (its own **Retry Errors** action).

## Titles in `error`

Every relation of the title was `error` or `unreachable` for vod-probe, so nothing could be chosen. They are left alone from then on (a failed title is only queued again when its relations change), and **[MAINTENANCE] Retry Errored Titles** puts them back once vod-probe has measured them again or you think the cause is gone.

## A file is named `unprobed`

That relation has no usable measurement from vod-probe. Let vod-probe measure it, run Scan + Process, then generate again: the file is renamed on the next Generate.

## A relation's quality is out of date

vod-probe measures a relation once. If a provider swaps the file behind a `stream_id`, ask vod-probe to measure that relation again (its own re-probe action); this plugin reads the new result at the next run.

## A series is missing versions, or episodes

Dispatcharr marks a source's episodes as loaded even when the provider answered with an empty or partial list, and never checks again. vod-probe handles this: it reloads a series loaded with no episode by itself, and **Reload Incomplete Series** in vod-probe finds the versions holding fewer episodes than another version and asks the provider. Run it when you suspect gaps, then let vod-probe measure, then run Scan + Process here.

## A `720p` is still there

Titles with nothing better keep their best available tier; see [Concepts](concepts.md).

## Emby plays the wrong version by default

See [.strm files and Emby / Jellyfin](strm-and-emby.md).
