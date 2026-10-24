"""Explicit SQLite persistence for the source-item PoC.

No ORM is used here intentionally. The tables mirror the limited durable model:
sources, source items, their associations, shared visit state, refresh runs, and
model usage. Article HTML, extracted bodies, and model prompts never enter this
module.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import FeedEntry
from .util import as_utc_text, parse_utc, utc_now


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK(length(trim(name)) > 0),
    feed_url TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    extract_articles INTEGER NOT NULL DEFAULT 1 CHECK(extract_articles IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1,
    etag TEXT,
    last_modified TEXT,
    last_success_at TEXT,
    last_error TEXT,
    last_error_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sequence_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    next_discovery_sequence INTEGER NOT NULL
);

INSERT OR IGNORE INTO sequence_state(id, next_discovery_sequence) VALUES (1, 1);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    normalized_url TEXT NOT NULL UNIQUE,
    headline TEXT NOT NULL,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    discovery_sequence INTEGER NOT NULL UNIQUE,
    feed_excerpt TEXT NOT NULL DEFAULT '',
    metadata_fingerprint TEXT NOT NULL,
    summary_status TEXT NOT NULL DEFAULT 'pending'
        CHECK(summary_status IN ('pending', 'processing', 'ready', 'unavailable')),
    summary_text TEXT,
    summary_basis TEXT CHECK(summary_basis IN ('article', 'feed_excerpt')),
    summary_reason TEXT,
    summary_input_fingerprint TEXT,
    supporting_excerpts TEXT NOT NULL DEFAULT '[]',
    summary_invalid_attempts INTEGER NOT NULL DEFAULT 0,
    summary_updated_at TEXT
);

CREATE INDEX IF NOT EXISTS items_chronological
    ON items(published_at DESC, discovered_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS items_discovery_sequence ON items(discovery_sequence);
CREATE INDEX IF NOT EXISTS items_summary_candidates ON items(summary_status, published_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS item_sources (
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    source_revision INTEGER NOT NULL DEFAULT 1,
    source_feed_url TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(item_id, source_id)
);
CREATE INDEX IF NOT EXISTS item_sources_source_item ON item_sources(source_id, item_id);

CREATE TABLE IF NOT EXISTS visit_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO visit_state(id, acknowledged_sequence, updated_at) VALUES (1, 0, '1970-01-01T00:00:00Z');

CREATE TABLE IF NOT EXISTS refresh_runs (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    phase TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    source_total INTEGER NOT NULL DEFAULT 0,
    source_completed INTEGER NOT NULL DEFAULT 0,
    items_inserted INTEGER NOT NULL DEFAULT 0,
    items_updated INTEGER NOT NULL DEFAULT 0,
    summaries_completed INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE INDEX IF NOT EXISTS refresh_runs_recent ON refresh_runs(id DESC);

CREATE TABLE IF NOT EXISTS refresh_source_results (
    refresh_run_id INTEGER NOT NULL REFERENCES refresh_runs(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'not_modified', 'failed')),
    item_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY(refresh_run_id, source_id)
);

CREATE TABLE IF NOT EXISTS model_state (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    utc_day TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_request_at TEXT,
    cooldown_until TEXT
);
INSERT OR IGNORE INTO model_state(id, utc_day, attempt_count) VALUES (1, '1970-01-01', 0);

CREATE TABLE IF NOT EXISTS model_attempts (
    id INTEGER PRIMARY KEY,
    refresh_run_id INTEGER REFERENCES refresh_runs(id) ON DELETE SET NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    input_fingerprint TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    completed_at TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('started', 'ready', 'insufficient', 'invalid', 'failed', 'quota_exhausted')),
    failure_reason TEXT,
    retry_after_at TEXT
);
CREATE INDEX IF NOT EXISTS model_attempts_item_fingerprint
    ON model_attempts(item_id, input_fingerprint, id DESC);
"""


class EnabledSourceLimitError(ValueError):
    """Raised atomically when an operation would exceed the configured cap."""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            self._ensure_source_revision_columns(conn)
            recovered_at = as_utc_text()
            # A process can exit after claiming an item and before writing its
            # result. The source-item remains readable and is safe to retry on
            # a later access rather than being stuck in an invisible state.
            conn.execute(
                """
                UPDATE items
                SET summary_status = 'pending', summary_reason = 'Summary pending: recovery after restart.'
                WHERE summary_status = 'processing'
                """
            )
            conn.execute(
                """
                UPDATE refresh_runs
                SET status = 'failed', phase = 'complete', completed_at = ?,
                    message = 'Refresh interrupted by application restart.'
                WHERE status = 'running'
                """,
                (recovered_at,),
            )
            conn.execute(
                """
                UPDATE model_attempts
                SET outcome = 'failed', completed_at = ?,
                    failure_reason = 'Model attempt interrupted by application restart.'
                WHERE outcome = 'started'
                """,
                (recovered_at,),
            )
        # Restrict newly-created database files on multi-user VM installations.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _ensure_source_revision_columns(conn: sqlite3.Connection) -> None:
        """Small forward migration for data created by an earlier PoC build."""

        source_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sources)")}
        if "revision" not in source_columns:
            conn.execute("ALTER TABLE sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        association_columns = {row["name"] for row in conn.execute("PRAGMA table_info(item_sources)")}
        if "source_revision" not in association_columns:
            conn.execute("ALTER TABLE item_sources ADD COLUMN source_revision INTEGER NOT NULL DEFAULT 1")
        if "source_feed_url" not in association_columns:
            conn.execute("ALTER TABLE item_sources ADD COLUMN source_feed_url TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()

    # Source configuration -------------------------------------------------

    def list_sources(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sources"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY lower(name), id"
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql)]

    def get_source(self, source_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        return dict(row) if row else None

    def count_enabled_sources(self, *, excluding_id: int | None = None) -> int:
        sql = "SELECT count(*) FROM sources WHERE enabled = 1"
        params: tuple[object, ...] = ()
        if excluding_id is not None:
            sql += " AND id != ?"
            params = (excluding_id,)
        with self.connection() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def create_source(
        self,
        name: str,
        feed_url: str,
        enabled: bool,
        extract_articles: bool,
        *,
        maximum_enabled: int | None = None,
    ) -> int:
        now = as_utc_text()
        with self.write() as conn:
            if enabled and maximum_enabled is not None:
                current = int(conn.execute("SELECT count(*) FROM sources WHERE enabled = 1").fetchone()[0])
                if current >= maximum_enabled:
                    raise EnabledSourceLimitError("Enabled source limit reached")
            cursor = conn.execute(
                """
                INSERT INTO sources(name, feed_url, enabled, extract_articles, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, feed_url, int(enabled), int(extract_articles), now, now),
            )
            return int(cursor.lastrowid)

    def update_source(
        self,
        source_id: int,
        name: str,
        feed_url: str,
        enabled: bool,
        extract_articles: bool,
        *,
        maximum_enabled: int | None = None,
    ) -> bool:
        now = as_utc_text()
        with self.write() as conn:
            old = conn.execute("SELECT feed_url, enabled FROM sources WHERE id = ?", (source_id,)).fetchone()
            if not old:
                return False
            if enabled and not old["enabled"] and maximum_enabled is not None:
                current = int(
                    conn.execute(
                        "SELECT count(*) FROM sources WHERE enabled = 1 AND id != ?", (source_id,)
                    ).fetchone()[0]
                )
                if current >= maximum_enabled:
                    raise EnabledSourceLimitError("Enabled source limit reached")
            feed_changed = old["feed_url"] != feed_url
            if feed_changed:
                cursor = conn.execute(
                    """
                    UPDATE sources
                    SET name = ?, feed_url = ?, enabled = ?, extract_articles = ?,
                        etag = NULL, last_modified = NULL, last_success_at = NULL,
                        last_error = NULL, last_error_at = NULL,
                        revision = revision + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (name, feed_url, int(enabled), int(extract_articles), now, source_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE sources
                    SET name = ?, enabled = ?, extract_articles = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, int(enabled), int(extract_articles), now, source_id),
                )
            return cursor.rowcount == 1

    def mark_source_success(
        self,
        source_id: int,
        *,
        etag: str | None,
        last_modified: str | None,
        now: str | None = None,
        expected_feed_url: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        checked_at = now or as_utc_text()
        clauses = ["id = ?"]
        params: list[object] = [checked_at, etag, last_modified, checked_at, source_id]
        if expected_feed_url is not None:
            clauses.append("feed_url = ?")
            params.append(expected_feed_url)
        if expected_revision is not None:
            clauses.append("revision = ?")
            params.append(expected_revision)
        with self.write() as conn:
            cursor = conn.execute(
                f"""
                UPDATE sources
                SET last_success_at = ?, last_error = NULL, last_error_at = NULL,
                    etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified), updated_at = ?
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            return cursor.rowcount == 1

    def mark_source_failure(
        self,
        source_id: int,
        error: str,
        *,
        now: str | None = None,
        expected_feed_url: str | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        failed_at = now or as_utc_text()
        clauses = ["id = ?"]
        params: list[object] = [error, failed_at, failed_at, source_id]
        if expected_feed_url is not None:
            clauses.append("feed_url = ?")
            params.append(expected_feed_url)
        if expected_revision is not None:
            clauses.append("revision = ?")
            params.append(expected_revision)
        with self.write() as conn:
            cursor = conn.execute(
                f"UPDATE sources SET last_error = ?, last_error_at = ?, updated_at = ? WHERE {' AND '.join(clauses)}",
                params,
            )
            return cursor.rowcount == 1

    # Visit state and chronological view ----------------------------------

    def acknowledged_sequence(self) -> int:
        with self.connection() as conn:
            return int(conn.execute("SELECT acknowledged_sequence FROM visit_state WHERE id = 1").fetchone()[0])

    def acknowledge_through(self, sequence: int) -> None:
        if sequence <= 0:
            return
        now = as_utc_text()
        with self.write() as conn:
            conn.execute(
                """
                UPDATE visit_state
                SET acknowledged_sequence = CASE WHEN acknowledged_sequence < ? THEN ? ELSE acknowledged_sequence END,
                    updated_at = ?
                WHERE id = 1
                """,
                (sequence, sequence, now),
            )

    def current_max_discovery_sequence(self) -> int:
        with self.connection() as conn:
            return int(conn.execute("SELECT COALESCE(MAX(discovery_sequence), 0) FROM items").fetchone()[0])

    def query_updates(
        self,
        *,
        cutoff: str,
        source_id: int | None,
        search: str | None,
        acknowledged_sequence: int,
    ) -> list[dict[str, Any]]:
        clauses = ["COALESCE(i.published_at, i.discovered_at) >= ?"]
        params: list[object] = [cutoff]
        if source_id is not None:
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM item_sources chosen
                    JOIN sources selected_source ON selected_source.id = chosen.source_id
                    WHERE chosen.item_id = i.id AND chosen.source_id = ?
                      AND selected_source.enabled = 1
                      AND chosen.source_revision = selected_source.revision
                )"""
            )
            params.append(source_id)
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            clauses.append("(i.headline LIKE ? ESCAPE '\\' OR i.url LIKE ? ESCAPE '\\')")
            params.extend([f"%{escaped}%", f"%{escaped}%"])
        where = " AND ".join(clauses)
        # Only enabled associations participate in the default public view.
        # A selected disabled source cannot bypass that rule.
        sql = f"""
            SELECT i.*, source_names.names AS source_names,
                   CASE WHEN i.published_at IS NULL THEN 1 ELSE 0 END AS timestamp_is_discovered
            FROM items i
            JOIN (
                SELECT item_sources.item_id AS item_id,
                       group_concat(sources.name, ' · ') AS names
                FROM item_sources
                JOIN sources ON sources.id = item_sources.source_id
                WHERE sources.enabled = 1 AND item_sources.source_revision = sources.revision
                GROUP BY item_sources.item_id
            ) AS source_names ON source_names.item_id = i.id
            WHERE {where}
            ORDER BY COALESCE(i.published_at, i.discovered_at) DESC, i.id DESC
        """
        with self.connection() as conn:
            rows = [dict(row) for row in conn.execute(sql, params)]
        for row in rows:
            row["is_new"] = row["discovery_sequence"] > acknowledged_sequence
            row["supporting_excerpts"] = _json_list(row.get("supporting_excerpts"))
        return rows

    # Feed ingestion -------------------------------------------------------

    def ingest_entries(
        self,
        entries: Sequence[FeedEntry],
        *,
        seen_at: str | None = None,
        expected_source_id: int | None = None,
        expected_feed_url: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, int | bool]:
        """Insert/update metadata in one short transaction.

        Feed and article network work always occurs before this method. New
        records become visible as soon as this transaction commits, before any
        extraction or model call starts.
        """

        now = seen_at or as_utc_text()
        inserted = 0
        updated = 0
        max_sequence = 0
        with self.write() as conn:
            source_versions: dict[int, tuple[int, str]] = {}
            if expected_source_id is not None:
                source_row = conn.execute(
                    "SELECT revision, feed_url FROM sources WHERE id = ?", (expected_source_id,)
                ).fetchone()
                if (
                    source_row is None
                    or (expected_feed_url is not None and source_row["feed_url"] != expected_feed_url)
                    or (expected_revision is not None and int(source_row["revision"]) != expected_revision)
                ):
                    return {"inserted": 0, "updated": 0, "max_sequence": 0, "discarded": True}
                source_versions[expected_source_id] = (int(source_row["revision"]), str(source_row["feed_url"]))
            for entry in entries:
                if entry.source_id not in source_versions:
                    source_row = conn.execute(
                        "SELECT revision, feed_url FROM sources WHERE id = ?", (entry.source_id,)
                    ).fetchone()
                    if source_row is None:
                        continue
                    source_versions[entry.source_id] = (int(source_row["revision"]), str(source_row["feed_url"]))
                row = conn.execute(
                    "SELECT id, metadata_fingerprint, discovery_sequence FROM items WHERE normalized_url = ?",
                    (entry.normalized_url,),
                ).fetchone()
                if row is None:
                    sequence = int(
                        conn.execute("SELECT next_discovery_sequence FROM sequence_state WHERE id = 1").fetchone()[0]
                    )
                    conn.execute(
                        "UPDATE sequence_state SET next_discovery_sequence = ? WHERE id = 1", (sequence + 1,)
                    )
                    cursor = conn.execute(
                        """
                        INSERT INTO items(
                            url, normalized_url, headline, published_at, discovered_at, discovery_sequence,
                            feed_excerpt, metadata_fingerprint, summary_status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                        """,
                        (
                            entry.url,
                            entry.normalized_url,
                            entry.headline,
                            entry.published_at,
                            now,
                            sequence,
                            entry.excerpt,
                            entry.metadata_fingerprint,
                        ),
                    )
                    item_id = int(cursor.lastrowid)
                    inserted += 1
                    max_sequence = max(max_sequence, sequence)
                else:
                    item_id = int(row["id"])
                    max_sequence = max(max_sequence, int(row["discovery_sequence"]))
                    if row["metadata_fingerprint"] != entry.metadata_fingerprint:
                        # A summary is inseparable from its feed metadata/input
                        # version. Never continue displaying it after a change.
                        conn.execute(
                            """
                            UPDATE items
                            SET url = ?, headline = ?, published_at = ?, feed_excerpt = ?,
                                metadata_fingerprint = ?, summary_status = 'pending', summary_text = NULL,
                                summary_basis = NULL, summary_reason = NULL, summary_input_fingerprint = NULL,
                                supporting_excerpts = '[]', summary_invalid_attempts = 0,
                                summary_updated_at = NULL
                            WHERE id = ?
                            """,
                            (
                                entry.url,
                                entry.headline,
                                entry.published_at,
                                entry.excerpt,
                                entry.metadata_fingerprint,
                                item_id,
                            ),
                        )
                        updated += 1
                source_revision, source_feed_url = source_versions[entry.source_id]
                conn.execute(
                    """
                    INSERT INTO item_sources(
                        item_id, source_id, source_revision, source_feed_url, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_id, source_id) DO UPDATE SET
                        source_revision = excluded.source_revision,
                        source_feed_url = excluded.source_feed_url,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (item_id, entry.source_id, source_revision, source_feed_url, now, now),
                )
        return {"inserted": inserted, "updated": updated, "max_sequence": max_sequence, "discarded": False}

    def summary_candidates(self, limit: int) -> list[dict[str, Any]]:
        """Newest currently eligible source items, independent of summary state UI."""

        sql = """
            SELECT i.*, EXISTS(
                SELECT 1
                FROM item_sources assoc
                JOIN sources source ON source.id = assoc.source_id
                WHERE assoc.item_id = i.id AND source.enabled = 1
                  AND assoc.source_revision = source.revision AND source.extract_articles = 1
            ) AS may_extract,
            group_concat(DISTINCT source_names.name) AS source_names
            FROM items i
            JOIN item_sources enabled_assoc ON enabled_assoc.item_id = i.id
            JOIN sources enabled_source ON enabled_source.id = enabled_assoc.source_id
                AND enabled_source.enabled = 1 AND enabled_assoc.source_revision = enabled_source.revision
            LEFT JOIN item_sources source_assoc ON source_assoc.item_id = i.id
            LEFT JOIN sources source_names ON source_names.id = source_assoc.source_id
                AND source_names.enabled = 1 AND source_assoc.source_revision = source_names.revision
            WHERE i.summary_status = 'pending'
            GROUP BY i.id
            ORDER BY COALESCE(i.published_at, i.discovered_at) DESC, i.id DESC
            LIMIT ?
        """
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, (limit,))]

    def mark_summary_processing(self, item_id: int, metadata_fingerprint: str) -> bool:
        with self.write() as conn:
            cursor = conn.execute(
                """
                UPDATE items SET summary_status = 'processing', summary_reason = NULL
                WHERE id = ? AND metadata_fingerprint = ? AND summary_status = 'pending'
                """,
                (item_id, metadata_fingerprint),
            )
            return cursor.rowcount == 1

    def set_summary_ready(
        self,
        *,
        item_id: int,
        metadata_fingerprint: str,
        input_fingerprint: str,
        summary: str,
        basis: str,
        evidence: list[dict[str, object]],
    ) -> bool:
        now = as_utc_text()
        # Persist only exact, short evidence quotes—not the extracted body or
        # model prompt that produced them.
        excerpts = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
        with self.write() as conn:
            cursor = conn.execute(
                """
                UPDATE items
                SET summary_status = 'ready', summary_text = ?, summary_basis = ?, summary_reason = NULL,
                    summary_input_fingerprint = ?, supporting_excerpts = ?, summary_invalid_attempts = 0,
                    summary_updated_at = ?
                WHERE id = ? AND metadata_fingerprint = ?
                """,
                (summary, basis, input_fingerprint, excerpts, now, item_id, metadata_fingerprint),
            )
            return cursor.rowcount == 1

    def set_summary_unavailable(
        self,
        *,
        item_id: int,
        metadata_fingerprint: str,
        reason: str,
        input_fingerprint: str | None = None,
    ) -> bool:
        now = as_utc_text()
        with self.write() as conn:
            cursor = conn.execute(
                """
                UPDATE items
                SET summary_status = 'unavailable', summary_text = NULL, summary_basis = NULL,
                    summary_reason = ?, summary_input_fingerprint = COALESCE(?, summary_input_fingerprint),
                    supporting_excerpts = '[]', summary_updated_at = ?
                WHERE id = ? AND metadata_fingerprint = ?
                """,
                (reason, input_fingerprint, now, item_id, metadata_fingerprint),
            )
            return cursor.rowcount == 1

    def return_summary_to_pending(
        self, *, item_id: int, metadata_fingerprint: str, reason: str | None = None
    ) -> None:
        with self.write() as conn:
            conn.execute(
                """
                UPDATE items SET summary_status = 'pending', summary_reason = ?
                WHERE id = ? AND metadata_fingerprint = ?
                """,
                (reason, item_id, metadata_fingerprint),
            )

    def record_invalid_summary(
        self,
        *,
        item_id: int,
        metadata_fingerprint: str,
        input_fingerprint: str,
        reason: str,
    ) -> int:
        """Count invalid model output per exact input version and return it.

        ``model_attempts.input_fingerprint`` is the durable scope rather than
        feed metadata alone: a newly extracted body or a changed excerpt is a
        genuinely new input and earns fresh validation attempts.
        """

        with self.write() as conn:
            row = conn.execute(
                "SELECT id FROM items WHERE id = ? AND metadata_fingerprint = ?",
                (item_id, metadata_fingerprint),
            ).fetchone()
            if not row:
                return 0
            attempts = int(
                conn.execute(
                    """
                    SELECT count(*) FROM model_attempts
                    WHERE item_id = ? AND input_fingerprint = ? AND outcome = 'invalid'
                    """,
                    (item_id, input_fingerprint),
                ).fetchone()[0]
            )
            status = "unavailable" if attempts >= 2 else "pending"
            conn.execute(
                """
                UPDATE items
                SET summary_invalid_attempts = ?, summary_status = ?, summary_text = NULL,
                    summary_basis = NULL, summary_reason = ?, summary_input_fingerprint = ?,
                    supporting_excerpts = '[]', summary_updated_at = ?
                WHERE id = ? AND metadata_fingerprint = ?
                """,
                (attempts, status, reason, input_fingerprint, as_utc_text(), item_id, metadata_fingerprint),
            )
            return attempts

    # Refresh runs ---------------------------------------------------------

    def create_refresh_run(self, source_total: int) -> int:
        now = as_utc_text()
        with self.write() as conn:
            cursor = conn.execute(
                """
                INSERT INTO refresh_runs(status, phase, started_at, source_total)
                VALUES ('running', 'fetching', ?, ?)
                """,
                (now, source_total),
            )
            return int(cursor.lastrowid)

    def update_refresh_run(
        self,
        run_id: int,
        *,
        phase: str | None = None,
        source_completed_increment: int = 0,
        items_inserted_increment: int = 0,
        items_updated_increment: int = 0,
        summaries_completed_increment: int = 0,
        message: str | None = None,
    ) -> None:
        updates: list[str] = []
        params: list[object] = []
        if phase is not None:
            updates.append("phase = ?")
            params.append(phase)
        if source_completed_increment:
            updates.append("source_completed = source_completed + ?")
            params.append(source_completed_increment)
        if items_inserted_increment:
            updates.append("items_inserted = items_inserted + ?")
            params.append(items_inserted_increment)
        if items_updated_increment:
            updates.append("items_updated = items_updated + ?")
            params.append(items_updated_increment)
        if summaries_completed_increment:
            updates.append("summaries_completed = summaries_completed + ?")
            params.append(summaries_completed_increment)
        if message is not None:
            updates.append("message = ?")
            params.append(message)
        if not updates:
            return
        params.append(run_id)
        with self.write() as conn:
            conn.execute(f"UPDATE refresh_runs SET {', '.join(updates)} WHERE id = ?", params)

    def finish_refresh_run(self, run_id: int, *, failed: bool = False, message: str | None = None) -> None:
        with self.write() as conn:
            conn.execute(
                """
                UPDATE refresh_runs
                SET status = ?, phase = 'complete', completed_at = ?, message = COALESCE(?, message)
                WHERE id = ?
                """,
                ("failed" if failed else "completed", as_utc_text(), message, run_id),
            )

    def record_refresh_source_result(
        self,
        run_id: int,
        source_id: int,
        outcome: str,
        *,
        item_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self.write() as conn:
            conn.execute(
                """
                INSERT INTO refresh_source_results(refresh_run_id, source_id, outcome, item_count, error, checked_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(refresh_run_id, source_id) DO UPDATE SET
                    outcome = excluded.outcome, item_count = excluded.item_count,
                    error = excluded.error, checked_at = excluded.checked_at
                """,
                (run_id, source_id, outcome, item_count, error, as_utc_text()),
            )

    def latest_refresh_run(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    # Model limits and attempts -------------------------------------------

    def reserve_model_attempt(
        self,
        *,
        refresh_run_id: int,
        item_id: int,
        input_fingerprint: str,
        daily_limit: int,
        minimum_interval_seconds: int,
    ) -> tuple[int | None, str | None]:
        """Atomically reserve one paid/free-provider request slot.

        The returned reason is one of ``daily_limit`` or ``cooldown``. Both
        counters and cooldown timestamps live in SQLite, so a restart cannot
        accidentally burst the provider account.
        """

        now = utc_now()
        now_text = as_utc_text(now)
        today = now.date().isoformat()
        with self.write() as conn:
            state = conn.execute("SELECT * FROM model_state WHERE id = 1").fetchone()
            count = int(state["attempt_count"])
            last_request = parse_utc(state["last_request_at"])
            cooldown = parse_utc(state["cooldown_until"])
            if state["utc_day"] != today:
                count = 0
                conn.execute(
                    """
                    UPDATE model_state
                    SET utc_day = ?, attempt_count = 0
                    WHERE id = 1
                    """,
                    (today,),
                )
            if count >= daily_limit:
                return None, "daily_limit"
            if cooldown and cooldown > now:
                return None, "cooldown"
            # This branch protects older databases/state where cooldown was
            # absent but a recent request time existed.
            if last_request and (now - last_request).total_seconds() < minimum_interval_seconds:
                return None, "cooldown"
            cooldown_text = as_utc_text(now + timedelta(seconds=minimum_interval_seconds))
            conn.execute(
                """
                UPDATE model_state
                SET utc_day = ?, attempt_count = ?, last_request_at = ?, cooldown_until = ?
                WHERE id = 1
                """,
                (today, count + 1, now_text, cooldown_text),
            )
            cursor = conn.execute(
                """
                INSERT INTO model_attempts(refresh_run_id, item_id, input_fingerprint, attempted_at, outcome)
                VALUES (?, ?, ?, ?, 'started')
                """,
                (refresh_run_id, item_id, input_fingerprint, now_text),
            )
            return int(cursor.lastrowid), None

    def complete_model_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        failure_reason: str | None = None,
        retry_after_at: str | None = None,
    ) -> None:
        with self.write() as conn:
            conn.execute(
                """
                UPDATE model_attempts
                SET outcome = ?, completed_at = ?, failure_reason = ?, retry_after_at = ?
                WHERE id = ?
                """,
                (outcome, as_utc_text(), failure_reason, retry_after_at, attempt_id),
            )

    def set_model_cooldown(self, until: str) -> None:
        with self.write() as conn:
            row = conn.execute("SELECT cooldown_until FROM model_state WHERE id = 1").fetchone()
            existing = parse_utc(row["cooldown_until"] if row else None)
            requested = parse_utc(until)
            # Never let a short Retry-After weaken the already-reserved 65s
            # pacing interval, and keep the farther provider deadline.
            target = max((value for value in (existing, requested) if value is not None), default=None)
            if target is not None:
                conn.execute("UPDATE model_state SET cooldown_until = ? WHERE id = 1", (as_utc_text(target),))

    def model_usage(self) -> dict[str, Any]:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM model_state WHERE id = 1").fetchone()
        return dict(row)


def _json_list(value: object) -> list[object]:
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError):
        return []
    return decoded if isinstance(decoded, list) else []
