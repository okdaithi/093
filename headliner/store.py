"""SQLite persistence: idempotent schema, dedup-on-insert, and queries."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from headliner.models import Headline, to_utc, utcnow

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH: Final = Path("headlines.db")

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS headlines (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    published_at  TEXT,
    fetched_at    TEXT    NOT NULL,
    summary       TEXT,
    content_hash  TEXT    NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_headlines_source       ON headlines(source);
CREATE INDEX IF NOT EXISTS idx_headlines_published_at ON headlines(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_headlines_fetched_at   ON headlines(fetched_at DESC);

CREATE TABLE IF NOT EXISTS fetch_log (
    id           INTEGER PRIMARY KEY,
    source       TEXT    NOT NULL,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT    NOT NULL,
    status       TEXT    NOT NULL,
    items_found  INTEGER NOT NULL DEFAULT 0,
    items_new    INTEGER NOT NULL DEFAULT 0,
    error        TEXT
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_source ON fetch_log(source, finished_at DESC);
"""

# FTS5 is compiled in on most builds but not all, so the index is optional and
# every search path has a LIKE fallback.
_FTS_SCHEMA: Final = """
CREATE VIRTUAL TABLE IF NOT EXISTS headlines_fts USING fts5(
    title,
    summary,
    content='headlines',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS headlines_fts_insert AFTER INSERT ON headlines BEGIN
    INSERT INTO headlines_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS headlines_fts_delete AFTER DELETE ON headlines BEGIN
    INSERT INTO headlines_fts(headlines_fts, rowid, title, summary)
    VALUES ('delete', old.id, old.title, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS headlines_fts_update AFTER UPDATE ON headlines BEGIN
    INSERT INTO headlines_fts(headlines_fts, rowid, title, summary)
    VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO headlines_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
"""


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Per-source rollup shown by `headliner sources`."""

    name: str
    total_items: int
    last_success: datetime | None
    last_status: str | None
    last_error: str | None


def _iso(value: datetime | None) -> str | None:
    aware = to_utc(value)
    return aware.isoformat() if aware else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return to_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


def has_fts(conn: sqlite3.Connection) -> bool:
    """True when the FTS5 index exists in this database."""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='headlines_fts'"
    ).fetchone()
    return row is not None


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema if absent. Safe to run on every start."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    try:
        conn.executescript(_FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        logger.debug("FTS5 unavailable (%s); search will use LIKE", exc)
    conn.commit()


def connect(
    path: Path | str = DEFAULT_DB_PATH, *, migrate_schema: bool = True
) -> sqlite3.Connection:
    """Open (and by default migrate) the database at `path`."""
    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in {"", "."}:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    if migrate_schema:
        migrate(conn)
    return conn


@contextmanager
def open_db(
    path: Path | str = DEFAULT_DB_PATH, *, migrate_schema: bool = True
) -> Iterator[sqlite3.Connection]:
    """Context manager around `connect` that always closes the handle."""
    conn = connect(path, migrate_schema=migrate_schema)
    try:
        yield conn
    finally:
        conn.close()


def insert_headlines(conn: sqlite3.Connection, headlines: Iterable[Headline]) -> int:
    """Insert headlines, ignoring ones already stored. Returns the new-row count."""
    rows = [
        (
            headline.source,
            headline.title,
            headline.url,
            _iso(headline.published_at),
            _iso(headline.fetched_at),
            headline.summary,
            headline.content_hash,
        )
        for headline in headlines
    ]
    if not rows:
        return 0

    # `total_changes` would also count the rows the FTS triggers write, so the
    # new-row count comes from the table itself.
    before = conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
    with conn:
        conn.executemany(
            """
            INSERT INTO headlines
                (source, title, url, published_at, fetched_at, summary, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(content_hash) DO NOTHING
            """,
            rows,
        )
    after = conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
    return int(after) - int(before)


def record_fetch(
    conn: sqlite3.Connection,
    *,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    items_found: int,
    items_new: int,
    error: str | None = None,
) -> None:
    """Append one row to `fetch_log`."""
    with conn:
        conn.execute(
            """
            INSERT INTO fetch_log
                (source, started_at, finished_at, status, items_found, items_new, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                _iso(started_at),
                _iso(finished_at),
                status,
                items_found,
                items_new,
                error,
            ),
        )


def _row_to_headline(row: sqlite3.Row) -> Headline:
    return Headline(
        source=row["source"],
        title=row["title"],
        url=row["url"],
        published_at=_parse_iso(row["published_at"]),
        fetched_at=_parse_iso(row["fetched_at"]) or utcnow(),
        summary=row["summary"],
        content_hash=row["content_hash"],
    )


_SELECT_COLUMNS: Final = "source, title, url, published_at, fetched_at, summary, content_hash"
# COALESCE so items without a publication date still sort by when we saw them.
_ORDER_BY: Final = "ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC"


def list_headlines(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[Headline]:
    """Most recent headlines first, optionally filtered by age and source."""
    clauses: list[str] = []
    params: list[Any] = []
    if since is not None:
        clauses.append("COALESCE(published_at, fetched_at) >= ?")
        params.append(_iso(since))
    if source:
        clauses.append("source = ? COLLATE NOCASE")
        params.append(source)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, limit))
    with closing(
        conn.execute(
            f"SELECT {_SELECT_COLUMNS}, id FROM headlines {where} {_ORDER_BY} LIMIT ?",
            params,
        )
    ) as cursor:
        return [_row_to_headline(row) for row in cursor.fetchall()]


def _escape_like(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(query: str) -> str:
    """Quote each token so user punctuation cannot break FTS5 syntax."""
    tokens = [token for token in query.replace('"', " ").split() if token]
    return " ".join(f'"{token}"*' for token in tokens)


def search_headlines(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 50,
) -> list[Headline]:
    """Full-text search over titles and summaries, falling back to LIKE."""
    term = query.strip()
    if not term:
        return []

    if has_fts(conn):
        match = _fts_query(term)
        if match:
            try:
                with closing(
                    conn.execute(
                        f"""
                        SELECT {", ".join(f"h.{c}" for c in _SELECT_COLUMNS.split(", "))}
                        FROM headlines_fts f
                        JOIN headlines h ON h.id = f.rowid
                        WHERE headlines_fts MATCH ?
                        ORDER BY bm25(headlines_fts), COALESCE(h.published_at, h.fetched_at) DESC
                        LIMIT ?
                        """,
                        (match, max(1, limit)),
                    )
                ) as cursor:
                    return [_row_to_headline(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError as exc:
                logger.debug("FTS query failed (%s); falling back to LIKE", exc)

    pattern = f"%{_escape_like(term)}%"
    with closing(
        conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}, id FROM headlines
            WHERE title LIKE ? ESCAPE '\\' OR COALESCE(summary, '') LIKE ? ESCAPE '\\'
            {_ORDER_BY}
            LIMIT ?
            """,
            (pattern, pattern, max(1, limit)),
        )
    ) as cursor:
        return [_row_to_headline(row) for row in cursor.fetchall()]


def source_status(conn: sqlite3.Connection, names: Iterable[str]) -> list[SourceStatus]:
    """Stored item counts and last-fetch state for each named source."""
    statuses: list[SourceStatus] = []
    for name in names:
        count_row = conn.execute(
            "SELECT COUNT(*) AS total FROM headlines WHERE source = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
        success_row = conn.execute(
            """
            SELECT finished_at FROM fetch_log
            WHERE source = ? COLLATE NOCASE AND status = 'ok'
            ORDER BY finished_at DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        last_row = conn.execute(
            """
            SELECT status, error FROM fetch_log
            WHERE source = ? COLLATE NOCASE
            ORDER BY finished_at DESC LIMIT 1
            """,
            (name,),
        ).fetchone()
        statuses.append(
            SourceStatus(
                name=name,
                total_items=int(count_row["total"]) if count_row else 0,
                last_success=_parse_iso(success_row["finished_at"]) if success_row else None,
                last_status=last_row["status"] if last_row else None,
                last_error=last_row["error"] if last_row else None,
            )
        )
    return statuses
