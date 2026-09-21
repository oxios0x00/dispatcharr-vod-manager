# First import

Follow this order the first time, on a catalogue you haven't validated yet.

0. **Let vod-probe measure the catalogue first.** VOD Manager only decides from its results; a title that is not measured yet waits.
1. **Keep Dry run ON** (the default). Nothing is deleted while it is on.
2. **Set a small batch size** (5 to 10 movies, 5 series) for the first pass.
3. **Scan.** Click **[MOVIES] Scan**. It queues the titles whose relations are new; running it again on an unchanged catalogue queues nothing.
4. **Process.** Click **[MOVIES] Scan + Process**. It returns at once and keeps working in the background, batch after batch, until the queue is empty. Click **[MOVIES] Queue Status** to follow it (it starts with `[RUNNING ...]` while it works and reads `pending=0 in_progress=0` at the end; titles vod-probe has not measured yet show as `waiting`), and **[MOVIES] Stop** to end it after the current batch (run Scan + Process again to carry on). A second click while it runs is refused with "already running".
5. **Check what it would prune.** Compare the picks on a few titles you know by hand: each relation's `custom_properties.probe` (visible through the API's `providers` endpoint) holds what vod-probe measured.
6. **Turn Dry run OFF** only once the picks look right. Pruning deletes real relations in Dispatcharr's database.
7. **Clean titles, then generate.** Once the queue is empty, run **[MOVIES] Clean Titles**, then **[MOVIES] Generate .strm Files**, in that order — cleaning after generating would rename the files right after writing them.

Then do the same for series with the `[SERIES]` actions and **[SERIES] Queue Status**. A series with empty or incomplete episode lists is reloaded by vod-probe (**Reload Incomplete Series**); until then it waits.

## Scheduling comes last

Leave **Schedule (5-field cron)** empty during a first import or against a large catalogue. Run everything by hand and watch the results until the queues settle; schedule it only when you trust the picks it makes on its own. See [Scheduling](scheduling.md).
