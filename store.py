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


class Store:
    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "state.sqlite3")
        with self._connect() as conn:
            conn.executescript(SCHEMA)

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

    def requeue_errors(self, content_type):
        """Put every errored title back in the queue. Returns how many."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE probe_queue SET status = 'pending', attempts = 0, last_error = NULL, "
                "updated_at = ? WHERE content_type = ? AND status = 'error'",
                (time.time(), content_type),
            ).rowcount

    def mark_waiting(self, content_type, content_id):
        """Set a title aside until vod-probe has measured all its relations."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE probe_queue SET status = 'waiting', updated_at = ? "
                "WHERE content_type = ? AND content_id = ?",
                (time.time(), content_type, content_id),
            )

    def requeue_waiting(self, content_type):
        """Give every waiting title another chance. Returns how many."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE probe_queue SET status = 'pending', updated_at = ? "
                "WHERE content_type = ? AND status = 'waiting'",
                (time.time(), content_type),
            ).rowcount

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
            counts = {"pending": 0, "in_progress": 0, "waiting": 0, "done": 0, "error": 0}
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

    def changed_content_ids(self, content_type, current_by_content):
        """Ids from `current_by_content` ({content_id: set of relation ids})
        that are new or whose relation set differs from the stored one, read
        with a single query."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_id, relation_ids FROM known_relations WHERE content_type = ?",
                (content_type,),
            ).fetchall()
        known = {row["content_id"]: set(json.loads(row["relation_ids"])) for row in rows}
        return [
            content_id
            for content_id, relation_ids in current_by_content.items()
            if known.get(content_id) != relation_ids
        ]

    def set_known_relation_ids(self, content_type, content_id, relation_ids):
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO known_relations (content_type, content_id, relation_ids, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(content_type, content_id) DO UPDATE SET "
                "relation_ids = excluded.relation_ids, updated_at = excluded.updated_at",
                (content_type, content_id, json.dumps(sorted(relation_ids)), time.time()),
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

    def lock_held_since(self, name, stale_after=3600):
        """Start (or last renewal) time of a live lock, or None when it is
        free or has gone stale."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT started_at FROM run_locks WHERE name = ?", (name,)
            ).fetchone()
        if row is None or row["started_at"] < time.time() - stale_after:
            return None
        return row["started_at"]

    def renew_lock(self, name):
        """Restart a held lock's staleness clock. A long run calls this
        between batches so that only one that has really died goes stale."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE run_locks SET started_at = ? WHERE name = ?", (time.time(), name)
            )

    def requeue_in_progress(self, content_type):
        """Put back titles a run claimed but never finished (Dispatcharr was
        restarted mid-batch). Only call while holding the lock that keeps
        another run from working the same queue. Returns how many."""
        with self._connect() as conn:
            return conn.execute(
                "UPDATE probe_queue SET status = 'pending', updated_at = ? "
                "WHERE content_type = ? AND status = 'in_progress'",
                (time.time(), content_type),
            ).rowcount

    # --- orphan cleanup (rows for titles/relations Dispatcharr deleted) ----

    @staticmethod
    def _chunks(ids, size=900):
        # SQLite caps bound variables per statement.
        ids = list(ids)
        for i in range(0, len(ids), size):
            yield ids[i:i + size]

    def stored_content_ids(self, content_type):
        """Every movie/series id the queue or the known-relations table still
        holds for this content_type."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT content_id FROM probe_queue WHERE content_type = ? "
                "UNION SELECT content_id FROM known_relations WHERE content_type = ?",
                (content_type, content_type),
            ).fetchall()
            return {r["content_id"] for r in rows}

    def delete_content_rows(self, content_type, content_ids):
        """Drops the queue and known-relations rows of the given titles.
        Returns (queue rows deleted, known-relations rows deleted)."""
        queue = known = 0
        with self._connect() as conn:
            for chunk in self._chunks(content_ids):
                marks = ",".join("?" for _ in chunk)
                queue += conn.execute(
                    f"DELETE FROM probe_queue WHERE content_type = ? AND content_id IN ({marks})",
                    (content_type, *chunk),
                ).rowcount
                known += conn.execute(
                    f"DELETE FROM known_relations WHERE content_type = ? AND content_id IN ({marks})",
                    (content_type, *chunk),
                ).rowcount
        return queue, known

    # --- full reset ------------------------------------------------------

    _ALL_TABLES = (
        "probe_queue", "known_relations",
        "plugin_state", "run_log", "catalog_stats", "strm_manifest",
        "run_locks",
    )

    def reset_all(self):
        """Wipes every table this plugin owns: queues, known
        relation sets, pause flags, run history, catalog stat snapshots and
        .strm tracking. The next scan re-discovers everything as new and the
        next process decides every title again. Never touches Dispatcharr's own
        database — only this plugin's own sidecar state."""
        with self._connect() as conn:
            for table in self._ALL_TABLES:
                conn.execute(f"DELETE FROM {table}")
