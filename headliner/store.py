"""SQLite persistence: versioned schema, one row per article URL, title history."""

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

# Stored in `PRAGMA user_version`. 0 is the original layout (dedup on URL and
# title); 1 keys `headlines` on URL and keeps titles in `headline_revisions`.
SCHEMA_VERSION: Final = 1

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
    error        TEXT,
    items_changed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_fetch_log_source ON fetch_log(source, finished_at DESC);

-- Every distinct title an article has carried, first-seen time included.
-- `headlines.title` is always the most recently observed one.
CREATE TABLE IF NOT EXISTS headline_revisions (
    id            INTEGER PRIMARY KEY,
    headline_id   INTEGER NOT NULL REFERENCES headlines(id) ON DELETE CASCADE,
    title         TEXT    NOT NULL,
    summary       TEXT,
    content_hash  TEXT    NOT NULL,
    seen_at       TEXT    NOT NULL,
    UNIQUE (headline_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_revisions_seen_at ON headline_revisions(seen_at DESC);
"""

# The schema-1 upgrade: fold rows that share a URL into the earliest one,
# keep each row's title as a revision, and surface the latest title. Runs in
# one transaction; see `_upgrade_to_v1`.
_UPGRADE_V1_STATEMENTS: Final = (
    """
    CREATE TEMP TABLE _keepers AS
    SELECT url, MIN(id) AS keeper_id FROM headlines GROUP BY url
    """,
    """
    INSERT OR IGNORE INTO headline_revisions
        (headline_id, title, summary, content_hash, seen_at)
    SELECT k.keeper_id, h.title, h.summary, h.content_hash, h.fetched_at
    FROM headlines h JOIN _keepers k ON k.url = h.url
    ORDER BY h.fetched_at, h.id
    """,
    """
    CREATE TEMP TABLE _latest AS
    SELECT keeper_id, title, summary, content_hash FROM (
        SELECT k.keeper_id, h.title, h.summary, h.content_hash,
               ROW_NUMBER() OVER (
                   PARTITION BY h.url ORDER BY h.fetched_at DESC, h.id DESC
               ) AS rn
        FROM headlines h JOIN _keepers k ON k.url = h.url
    ) WHERE rn = 1
    """,
    "DELETE FROM headlines WHERE id NOT IN (SELECT keeper_id FROM _keepers)",
    """
    UPDATE headlines
    SET title = l.title, summary = l.summary, content_hash = l.content_hash
    FROM _latest l
    WHERE headlines.id = l.keeper_id AND headlines.content_hash <> l.content_hash
    """,
    "DROP TABLE _keepers",
    "DROP TABLE _latest",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_headlines_url ON headlines(url)",
)

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


@dataclass(frozen=True, slots=True)
class InsertResult:
    """What one `store_headlines` call changed."""

    new: int
    retitled: int


@dataclass(frozen=True, slots=True)
class TitleChange:
    """One observed rewrite of an article's headline."""

    source: str
    url: str
    changed_at: datetime | None
    old_title: str
    new_title: str


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """What `migrate` would do to an existing database, for `--dry-run`."""

    from_version: int
    to_version: int
    headlines: int
    merged_urls: int
    rows_merged: int

    @property
    def needed(self) -> bool:
        return self.from_version < self.to_version


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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _db_file(conn: sqlite3.Connection) -> Path | None:
    """The on-disk path of the main database, or None for in-memory ones."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main" and row[2]:
            return Path(row[2])
    return None


def upgrade_plan(conn: sqlite3.Connection) -> UpgradePlan:
    """Describe the pending schema upgrade without changing anything."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if not _table_exists(conn, "headlines"):
        return UpgradePlan(version, SCHEMA_VERSION, 0, 0, 0)
    total, distinct = conn.execute("SELECT COUNT(*), COUNT(DISTINCT url) FROM headlines").fetchone()
    merged_urls = 0
    if version < 1:
        merged_urls = conn.execute(
            "SELECT COUNT(*) FROM (SELECT url FROM headlines GROUP BY url HAVING COUNT(*) > 1)"
        ).fetchone()[0]
    rows_merged = int(total) - int(distinct) if version < 1 else 0
    return UpgradePlan(version, SCHEMA_VERSION, int(total), int(merged_urls), rows_merged)


def _backup(conn: sqlite3.Connection, suffix: str) -> Path | None:
    """Copy the live database next to itself before a destructive upgrade."""
    source = _db_file(conn)
    if source is None:
        return None
    target = source.with_name(f"{source.name}.{suffix}.bak")
    with closing(sqlite3.connect(target)) as copy:
        conn.backup(copy)
    return target


def _upgrade_to_v1(conn: sqlite3.Connection) -> None:
    """Key `headlines` on URL and move title history into `headline_revisions`."""
    plan = upgrade_plan(conn)
    if plan.rows_merged:
        backup = _backup(conn, "pre-v1")
        logger.warning(
            "upgrading database: merging %d row(s) across %d URL(s) into title history"
            " (backup: %s)",
            plan.rows_merged,
            plan.merged_urls,
            backup,
        )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(fetch_log)").fetchall()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "items_changed" not in columns:
            conn.execute(
                "ALTER TABLE fetch_log ADD COLUMN items_changed INTEGER NOT NULL DEFAULT 0"
            )
        for statement in _UPGRADE_V1_STATEMENTS:
            conn.execute(statement)
        conn.execute("PRAGMA user_version = 1")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and apply pending upgrades. Safe on every start."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    try:
        conn.executescript(_FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        logger.debug("FTS5 unavailable (%s); search will use LIKE", exc)
    conn.commit()
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 1:
        _upgrade_to_v1(conn)


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


def _add_revision(conn: sqlite3.Connection, headline_id: int, headline: Headline) -> bool:
    """Record `headline`'s title for this article; False if it was seen before."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO headline_revisions
            (headline_id, title, summary, content_hash, seen_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            headline_id,
            headline.title,
            headline.summary,
            headline.content_hash,
            _iso(headline.fetched_at),
        ),
    )
    return cursor.rowcount == 1


def store_headlines(conn: sqlite3.Connection, headlines: Iterable[Headline]) -> InsertResult:
    """Store headlines, one row per article URL, keeping every distinct title.

    A URL not seen before is a new row. A known URL under a different title
    becomes the row's current title and, the first time that title is seen
    for the article, a new revision. Titles that only differ in case or
    whitespace are the same title (see `compute_hash`).
    """
    new = retitled = 0
    with conn:
        for headline in headlines:
            existing = conn.execute(
                "SELECT id, content_hash FROM headlines WHERE url = ?", (headline.url,)
            ).fetchone()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO headlines
                        (source, title, url, published_at, fetched_at, summary, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        headline.source,
                        headline.title,
                        headline.url,
                        _iso(headline.published_at),
                        _iso(headline.fetched_at),
                        headline.summary,
                        headline.content_hash,
                    ),
                )
                headline_id = cursor.lastrowid
                assert headline_id is not None
                _add_revision(conn, headline_id, headline)
                new += 1
                continue

            if existing["content_hash"] == headline.content_hash:
                continue
            # A title flipping back to an earlier one updates the current
            # title but adds no revision, so feeds that alternate between
            # two wordings do not grow the history every run.
            if _add_revision(conn, existing["id"], headline):
                retitled += 1
            conn.execute(
                "UPDATE headlines SET title = ?, summary = ?, content_hash = ? WHERE id = ?",
                (headline.title, headline.summary, headline.content_hash, existing["id"]),
            )
    return InsertResult(new=new, retitled=retitled)


def insert_headlines(conn: sqlite3.Connection, headlines: Iterable[Headline]) -> int:
    """`store_headlines`, returning only the number of new articles."""
    return store_headlines(conn, headlines).new


def record_fetch(
    conn: sqlite3.Connection,
    *,
    source: str,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    items_found: int,
    items_new: int,
    items_changed: int = 0,
    error: str | None = None,
) -> None:
    """Append one row to `fetch_log`."""
    with conn:
        conn.execute(
            """
            INSERT INTO fetch_log
                (source, started_at, finished_at, status, items_found, items_new,
                 items_changed, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                _iso(started_at),
                _iso(finished_at),
                status,
                items_found,
                items_new,
                items_changed,
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


def list_title_changes(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    limit: int = 50,
) -> list[TitleChange]:
    """Headline rewrites, most recent first: each revision after an article's first."""
    clauses = ["r.old_title IS NOT NULL"]
    params: list[Any] = []
    if since is not None:
        clauses.append("r.seen_at >= ?")
        params.append(_iso(since))
    if source:
        clauses.append("h.source = ? COLLATE NOCASE")
        params.append(source)
    params.append(max(1, limit))
    with closing(
        conn.execute(
            f"""
            SELECT h.source, h.url, r.seen_at, r.old_title, r.title
            FROM (
                SELECT id, headline_id, title, seen_at,
                       LAG(title) OVER (
                           PARTITION BY headline_id ORDER BY seen_at, id
                       ) AS old_title
                FROM headline_revisions
            ) r
            JOIN headlines h ON h.id = r.headline_id
            WHERE {" AND ".join(clauses)}
            ORDER BY r.seen_at DESC, r.id DESC
            LIMIT ?
            """,
            params,
        )
    ) as cursor:
        return [
            TitleChange(
                source=row["source"],
                url=row["url"],
                changed_at=_parse_iso(row["seen_at"]),
                old_title=row["old_title"],
                new_title=row["title"],
            )
            for row in cursor.fetchall()
        ]


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
