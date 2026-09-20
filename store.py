"""SQLite-backed state for VOD Manager.

Dispatcharr plugins are dynamically imported modules, never registered as a
Django app (confirmed by reading apps/plugins/loader.py) — there is no
migration hook available to declare our own Django models. The only native
DB storage a plugin gets is a single JSONField on PluginConfig, unsuited to
a queue of hundreds of thousands of rows. So we keep our own state in a
sidecar SQLite file inside the plugin's own data directory, following the
pattern already used by other plugins in this ecosystem (see NOTES.md
point 4).
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

try:
    from .probe_summary import dumps_compact, summarize_probe
except ImportError:  # imported as a top-level module by the unit tests
    from probe_summary import dumps_compact, summarize_probe

SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type TEXT NOT NULL,
    content_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    enqueued_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE(content_type, content_id)
);
CREATE INDEX IF NOT EXISTS idx_probe_queue_status ON probe_queue(status);

CREATE TABLE IF NOT EXISTS known_relations (
    content_type TEXT NOT NULL,
    content_id INTEGER NOT NULL,
    relation_ids TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (content_type, content_id)
);

-- relation_id alone is NOT a safe key once more than one content type is
-- stored here: M3UMovieRelation and M3UEpisodeRelation are separate Django
-- tables with their own independent autoincrement ids, so a movie relation
-- and an episode relation can share the same numeric id. Keyed on
-- (content_type, relation_id) from the start for series/episode support.
CREATE TABLE IF NOT EXISTS relation_probes (
    content_type TEXT NOT NULL DEFAULT 'movie',
    relation_id INTEGER NOT NULL,
    probed_at REAL NOT NULL,
    ok INTEGER NOT NULL,
    error TEXT,
    width INTEGER,
    height INTEGER,
    quality_label TEXT,
    video_codec TEXT,
    video_bitrate INTEGER,
    probe_version INTEGER NOT NULL DEFAULT 0,
    hdr_type TEXT,
    audio_languages TEXT,
    audio_description_languages TEXT,
    subtitle_languages TEXT,
    duration_secs REAL,
    raw_json TEXT,
    sampled_from_relation_id INTEGER,
    PRIMARY KEY (content_type, relation_id)
);

CREATE TABLE IF NOT EXISTS plugin_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    content_type TEXT,
    processed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    pruned_relations INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 1,
    note TEXT
);

-- One row per (snapshot, content_type, quality_label). "movie" rows count
-- Movie titles/M3UMovieRelation rows currently kept (post-prune). "series"
-- rows count distinct Series with at least one kept episode relation at
-- that quality, and the matching kept M3UEpisodeRelation count.
CREATE TABLE IF NOT EXISTS catalog_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at REAL NOT NULL,
    content_type TEXT NOT NULL,
    quality_label TEXT NOT NULL,
    title_count INTEGER NOT NULL DEFAULT 0,
    relation_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_catalog_stats_taken_at ON catalog_stats(taken_at);

-- One row per .strm file this plugin wrote, as of the last generate run
-- for that content_type. Diffing the previous manifest against the set
-- just written is how a removed/renamed relation's stale .strm gets
-- cleaned up automatically — see plugin.py's _generate_movie_strm /
-- _generate_series_strm.
CREATE TABLE IF NOT EXISTS strm_manifest (
    content_type TEXT NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (content_type, path)
);

-- One row per named action currently "in flight", so a second uwsgi worker
-- picking up a re-clicked/retried request can tell a batch is still
-- running instead of starting a duplicate one on top of it (see
-- try_acquire_lock below).
CREATE TABLE IF NOT EXISTS run_locks (
    name TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    pid INTEGER
);
"""


# Columns added after the initial schema — CREATE TABLE IF NOT EXISTS
# leaves an already-existing table untouched, so new columns need an
# explicit ALTER TABLE for anyone upgrading an existing state.sqlite3.
_MIGRATIONS = [
    ("relation_probes", "video_bitrate", "ALTER TABLE relation_probes ADD COLUMN video_bitrate INTEGER"),
    (
        "relation_probes",
        "probe_version",
        "ALTER TABLE relation_probes ADD COLUMN probe_version INTEGER NOT NULL DEFAULT 0",
    ),
]


def _migrate_relation_probes_content_type(conn):
    """One-time structural migration: relation_probes used to be keyed on
    relation_id alone (PRIMARY KEY), which only worked because it stored a
    single content type (movies). Adding series/episode probes to the same
    table needs a composite (content_type, relation_id) key instead — a
    plain ALTER TABLE can't change a SQLite primary key, so this rebuilds
    the table. Every pre-existing row predates series support, so it's
    tagged content_type='movie', which is the only type it could ever have
    held."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(relation_probes)")}
    if "content_type" in existing_cols or not existing_cols:
        return  # already migrated, or table doesn't exist yet (fresh install)

    conn.executescript(
        """
        ALTER TABLE relation_probes RENAME TO relation_probes_old;
        CREATE TABLE relation_probes (
            content_type TEXT NOT NULL DEFAULT 'movie',
            relation_id INTEGER NOT NULL,
            probed_at REAL NOT NULL,
            ok INTEGER NOT NULL,
            error TEXT,
            width INTEGER,
            height INTEGER,
            quality_label TEXT,
            video_codec TEXT,
            video_bitrate INTEGER,
            probe_version INTEGER NOT NULL DEFAULT 0,
            hdr_type TEXT,
            audio_languages TEXT,
            audio_description_languages TEXT,
            subtitle_languages TEXT,
            duration_secs REAL,
            raw_json TEXT,
            sampled_from_relation_id INTEGER,
            PRIMARY KEY (content_type, relation_id)
        );
        INSERT INTO relation_probes (
            content_type, relation_id, probed_at, ok, error, width, height,
            quality_label, video_codec, video_bitrate, probe_version, hdr_type,
            audio_languages, audio_description_languages, subtitle_languages,
            duration_secs, raw_json
        )
        SELECT
            'movie', relation_id, probed_at, ok, error, width, height,
            quality_label, video_codec, video_bitrate, probe_version, hdr_type,
            audio_languages, audio_description_languages, subtitle_languages,
            duration_secs, raw_json
        FROM relation_probes_old;
        DROP TABLE relation_probes_old;
        """
    )


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "state.sqlite3")
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            self._run_migrations(conn)
            self._compact_stored_raw_json(conn)
        self._vacuum_once()

    def _flag_set(self, conn, key):
        return conn.execute("SELECT 1 FROM plugin_state WHERE key = ?", (key,)).fetchone() is not None

    def _set_flag(self, conn, key):
        conn.execute(
            "INSERT INTO plugin_state (key, value) VALUES (?, 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key,),
        )

    def _compact_stored_raw_json(self, conn):
        """One-time: raw_json used to hold ffprobe's entire output (about
        38 KB a row, 97 of the 110 MB the file had reached) and nothing ever
        read it. Rewrites each such row as the compact summary built from
        that same output, so what a future feature might want is kept at a
        fraction of the size; a row whose JSON can't be parsed is emptied."""
        if self._flag_set(conn, "raw_json_compacted"):
            return
        rows = conn.execute(
            "SELECT content_type, relation_id, raw_json FROM relation_probes "
            "WHERE raw_json IS NOT NULL AND length(raw_json) > 4000"
        ).fetchall()
        updates = []
        for row in rows:
            try:
                compact = dumps_compact(summarize_probe(json.loads(row["raw_json"])))
            except (TypeError, ValueError):
                compact = None
            updates.append((compact, row["content_type"], row["relation_id"]))
        conn.executemany(
            "UPDATE relation_probes SET raw_json = ? WHERE content_type = ? AND relation_id = ?",
            updates,
        )
        self._set_flag(conn, "raw_json_compacted")

    def _vacuum_once(self):
        """One-time VACUUM after the compaction above, since SQLite never
        shrinks the file on its own. It needs the whole database to itself,
        so if a batch is writing at that moment it is skipped and retried at
        the next start."""
        try:
            with self._connect() as conn:
                if self._flag_set(conn, "raw_json_vacuumed"):
                    return
            raw = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            try:
                raw.execute("VACUUM")
                raw.execute("INSERT OR REPLACE INTO plugin_state (key, value) VALUES ('raw_json_vacuumed', 'true')")
            finally:
                raw.close()
        except sqlite3.OperationalError:
            pass

    def _run_migrations(self, conn):
        # Simple ADD COLUMN migrations must run first: the structural
        # content_type rebuild below copies relation_probes by full column
        # list, so the old table needs every pre-content_type column
        # (video_bitrate, probe_version) already present before it's copied.
        for table, column, ddl in _MIGRATIONS:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(ddl)
        _migrate_relation_probes_content_type(conn)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- plugin_state (small key/value: paused flag, etc.) -----------------

    def get_state(self, key, default=None):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM plugin_state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (TypeError, ValueError):
                return row["value"]

    def set_state(self, key, value):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO plugin_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )

    def is_paused(self, content_type):
        # Keyed per content_type so a movie circuit-breaker trip (or a
        # manual pause) doesn't silently pause series processing too, and
        # vice versa — the two queues are otherwise fully independent.
        return bool(self.get_state(f"paused:{content_type}", False))

    def set_paused(self, content_type, paused):
        self.set_state(f"paused:{content_type}", bool(paused))

    # --- probe_queue ---------------------------------------------------

    def enqueue(self, content_type, content_id):
        """Insert as pending if not already queued. No-op if already present
        (any status) — a caller that wants to force reprocessing should use
        requeue() instead."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO probe_queue "
                "(content_type, content_id, status, enqueued_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?) "
                "ON CONFLICT(content_type, content_id) DO NOTHING",
                (content_type, content_id, now, now),
            )

    def requeue(self, content_type, content_id):
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO probe_queue "
                "(content_type, content_id, status, attempts, enqueued_at, updated_at) "
                "VALUES (?, ?, 'pending', 0, ?, ?) "
                "ON CONFLICT(content_type, content_id) DO UPDATE SET "
                "status = 'pending', attempts = 0, last_error = NULL, updated_at = excluded.updated_at",
                (content_type, content_id, now, now),
            )

    def claim_batch(self, content_type, limit):
        """Atomically mark up to `limit` pending rows as in_progress and
        return them. Best-effort single-process locking via SQLite's own
        transaction; fine for the single-worker model this plugin runs
        under (Dispatcharr invokes plugin actions synchronously)."""
        now = time.time()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, content_id FROM probe_queue "
                "WHERE content_type = ? AND status = 'pending' "
                "ORDER BY id LIMIT ?",
                (content_type, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE probe_queue SET status = 'in_progress', updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (now, *ids),
                )
            return [r["content_id"] for r in rows]

    def mark_done(self, content_type, content_id):
        with self._connect() as conn:
            conn.execute(
                "UPDATE probe_queue SET status = 'done', updated_at = ? "
                "WHERE content_type = ? AND content_id = ?",
                (time.time(), content_type, content_id),
            )

    def mark_error(self, content_type, content_id, error):
        with self._connect() as conn:
            conn.execute(
                "UPDATE probe_queue SET status = 'error', attempts = attempts + 1, "
                "last_error = ?, updated_at = ? "
                "WHERE content_type = ? AND content_id = ?",
                (str(error)[:2000], time.time(), content_type, content_id),
            )

    def queue_counts(self, content_type):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM probe_queue "
                "WHERE content_type = ? GROUP BY status",
                (content_type,),
            ).fetchall()
            counts = {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
            for r in rows:
                counts[r["status"]] = r["n"]
            return counts

    # --- known_relations (incremental change detection) -----------------

    def get_known_relation_ids(self, content_type, content_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT relation_ids FROM known_relations "
                "WHERE content_type = ? AND content_id = ?",
                (content_type, content_id),
            ).fetchone()
            if row is None:
                return None
            return set(json.loads(row["relation_ids"]))

    def set_known_relation_ids(self, content_type, content_id, relation_ids):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO known_relations (content_type, content_id, relation_ids, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(content_type, content_id) DO UPDATE SET "
                "relation_ids = excluded.relation_ids, updated_at = excluded.updated_at",
                (content_type, content_id, json.dumps(sorted(relation_ids)), time.time()),
            )

    # --- relation_probes (cached ffprobe results) -----------------------

    def get_probe(self, content_type, relation_id):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM relation_probes WHERE content_type = ? AND relation_id = ?",
                (content_type, relation_id),
            ).fetchone()
            return dict(row) if row else None

    def delete_probe(self, content_type, relation_id):
        """Forces one specific relation to be treated as never-probed (get_probe
        will return None), without touching any other relation's cache — for a
        targeted re-probe when a provider swaps a stream's content without
        changing its stream_id (confirmed happening for real — see NOTES.md)."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM relation_probes WHERE content_type = ? AND relation_id = ?",
                (content_type, relation_id),
            )

    def get_quality_labels(self, content_type, relation_ids):
        """Bulk relation_id -> quality_label for successfully-probed rows
        (used for catalogue-composition reporting, not selection — reads
        whatever quality was last probed, regardless of probe_version)."""
        if not relation_ids:
            return {}
        with self._connect() as conn:
            result = {}
            # SQLite caps bound variables per statement; chunk defensively.
            ids = list(relation_ids)
            for i in range(0, len(ids), 900):
                chunk = ids[i:i + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"SELECT relation_id, quality_label FROM relation_probes "
                    f"WHERE content_type = ? AND ok = 1 AND relation_id IN ({placeholders})",
                    (content_type, *chunk),
                ).fetchall()
                result.update({r["relation_id"]: r["quality_label"] for r in rows})
            return result

    @staticmethod
    def _probe_payload(result):
        """What goes in the raw_json column: the compact summary of a
        successful probe (a kilobyte or so), or — so a failure stays
        diagnosable — a size-capped copy of ffprobe's raw output when a
        probe found no video stream. The column name predates the compact
        form and is kept to avoid a table rebuild."""
        summary = result.get("summary")
        if summary:
            return dumps_compact(summary)
        raw = result.get("raw")
        if raw:
            return json.dumps(raw, separators=(",", ":"), ensure_ascii=False)[:8000]
        return None

    def save_probe(self, content_type, relation_id, result, sampled_from_relation_id=None):
        """sampled_from_relation_id marks a row as copied from another
        relation's real probe (episode sampling extrapolation) rather than
        independently measured — see NOTES.md, episode sampling."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO relation_probes "
                "(content_type, relation_id, probed_at, ok, error, width, height, quality_label, "
                "video_codec, video_bitrate, probe_version, hdr_type, audio_languages, "
                "audio_description_languages, subtitle_languages, duration_secs, raw_json, "
                "sampled_from_relation_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(content_type, relation_id) DO UPDATE SET "
                "probed_at=excluded.probed_at, ok=excluded.ok, error=excluded.error, "
                "width=excluded.width, height=excluded.height, quality_label=excluded.quality_label, "
                "video_codec=excluded.video_codec, video_bitrate=excluded.video_bitrate, "
                "probe_version=excluded.probe_version, "
                "hdr_type=excluded.hdr_type, "
                "audio_languages=excluded.audio_languages, "
                "audio_description_languages=excluded.audio_description_languages, "
                "subtitle_languages=excluded.subtitle_languages, "
                "duration_secs=excluded.duration_secs, raw_json=excluded.raw_json, "
                "sampled_from_relation_id=excluded.sampled_from_relation_id",
                (
                    content_type,
                    relation_id,
                    time.time(),
                    1 if result.get("ok") else 0,
                    result.get("error"),
                    result.get("width"),
                    result.get("height"),
                    result.get("quality_label"),
                    result.get("video_codec"),
                    result.get("video_bitrate"),
                    result.get("probe_version", 0),
                    result.get("hdr_type"),
                    json.dumps(result.get("audio_languages", [])),
                    json.dumps(result.get("audio_description_languages", [])),
                    json.dumps(result.get("subtitle_languages", [])),
                    result.get("duration_secs"),
                    self._probe_payload(result),
                    sampled_from_relation_id,
                ),
            )

    # --- run_log ---------------------------------------------------------

    def start_run(self, content_type, dry_run):
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO run_log (started_at, content_type, dry_run) VALUES (?, ?, ?)",
                (time.time(), content_type, 1 if dry_run else 0),
            )
            return cur.lastrowid

    def finish_run(self, run_id, processed, errors, pruned_relations, note=""):
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_log SET finished_at = ?, processed = ?, errors = ?, "
                "pruned_relations = ?, note = ? WHERE id = ?",
                (time.time(), processed, errors, pruned_relations, note, run_id),
            )

    def last_run(self, content_type):
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM run_log WHERE content_type = ? ORDER BY id DESC LIMIT 1",
                (content_type,),
            ).fetchone()
            return dict(row) if row else None

    # --- catalog_stats (quality/language composition snapshots) --------

    def save_catalog_stats_snapshot(self, rows):
        """rows: list of dicts with content_type, quality_label,
        title_count, relation_count. All rows in one call share the same
        taken_at, so a snapshot can be queried/compared as a whole."""
        now = time.time()
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO catalog_stats "
                "(taken_at, content_type, quality_label, title_count, relation_count) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (now, r["content_type"], r["quality_label"], r["title_count"], r["relation_count"])
                    for r in rows
                ],
            )
            return now

    def get_latest_catalog_stats(self):
        with self._connect() as conn:
            latest = conn.execute("SELECT MAX(taken_at) AS t FROM catalog_stats").fetchone()["t"]
            if latest is None:
                return None, []
            rows = conn.execute(
                "SELECT content_type, quality_label, title_count, relation_count "
                "FROM catalog_stats WHERE taken_at = ? "
                "ORDER BY content_type, quality_label",
                (latest,),
            ).fetchall()
            return latest, [dict(r) for r in rows]

    def get_catalog_stats_history(self, limit=20):
        """Distinct snapshot timestamps, most recent first — for trend
        review without pulling every row of every snapshot."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT taken_at FROM catalog_stats ORDER BY taken_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [r["taken_at"] for r in rows]

    # --- strm_manifest (which .strm paths we wrote last time) ----------

    def get_strm_manifest(self, content_type):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT path FROM strm_manifest WHERE content_type = ?", (content_type,)
            ).fetchall()
            return {r["path"] for r in rows}

    def save_strm_manifest(self, content_type, paths):
        """Replaces the whole manifest for this content_type with `paths`
        (an iterable of strings), atomically."""
        with self._connect() as conn:
            conn.execute("DELETE FROM strm_manifest WHERE content_type = ?", (content_type,))
            conn.executemany(
                "INSERT INTO strm_manifest (content_type, path) VALUES (?, ?)",
                [(content_type, p) for p in paths],
            )

    # --- run_locks (stop a re-clicked/retried action piling onto a still- -
    # running one, across separate uwsgi worker processes) ---------------

    def try_acquire_lock(self, name, stale_after=3600):
        """Atomically claim a named lock. A lock older than stale_after
        seconds is treated as abandoned — e.g. Dispatcharr was restarted
        while the action that held it was still running — so a crash can
        never permanently block this action; it self-heals on its own
        instead of needing a manual reset. Race-free across processes: the
        INSERT...ON CONFLICT...WHERE runs as one statement under SQLite's
        own writer lock, so two workers racing to acquire can't both win.
        Returns (acquired, held_since) — held_since is the current
        holder's start time when acquisition fails, None otherwise."""
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO run_locks (name, started_at, pid) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET started_at = excluded.started_at, "
                "pid = excluded.pid WHERE run_locks.started_at < ?",
                (name, now, os.getpid(), now - stale_after),
            )
            row = conn.execute(
                "SELECT started_at, pid FROM run_locks WHERE name = ?", (name,)
            ).fetchone()
            acquired = row is not None and row["started_at"] == now and row["pid"] == os.getpid()
            return acquired, (None if acquired else row["started_at"])

    def release_lock(self, name):
        with self._connect() as conn:
            conn.execute("DELETE FROM run_locks WHERE name = ?", (name,))

    # --- full reset ------------------------------------------------------

    _ALL_TABLES = (
        "probe_queue", "known_relations", "relation_probes",
        "plugin_state", "run_log", "catalog_stats", "strm_manifest",
        "run_locks",
    )

    def reset_all(self):
        """Wipes every table this plugin owns: probe cache, queues, known
        relation sets, pause flags, run history, catalog stat snapshots and
        .strm tracking. The next scan re-discovers everything as new and the
        next process re-probes from scratch. Never touches Dispatcharr's own
        database — only this plugin's own sidecar state."""
        with self._connect() as conn:
            for table in self._ALL_TABLES:
                conn.execute(f"DELETE FROM {table}")
