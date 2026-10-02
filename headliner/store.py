"""SQLite persistence: versioned schema, one row per article URL, title history."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from headliner.models import (
    Headline,
    compute_hash,
    is_minor_change,
    looks_live,
    normalise_url,
    to_utc,
    utcnow,
)

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH: Final = Path("headlines.db")

# Stored in `PRAGMA user_version`. 0 is the original layout (dedup on URL and
# title); 1 keys `headlines` on URL and keeps titles in `headline_revisions`;
# 2 adds `headlines.is_live` for live blogs; 3 re-normalises stored URLs (BBC
# `at_*` tracking parameters) and indexes `headline_revisions` for search; 4
# stores each revision's previous title and whether the change was minor, so
# rewrite queries are plain filters, and each fetch's newest item date.
SCHEMA_VERSION: Final = 4

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS headlines (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    published_at  TEXT,
    fetched_at    TEXT    NOT NULL,
    summary       TEXT,
    content_hash  TEXT    NOT NULL UNIQUE,
    -- 1 once any version of the article looked like a live blog; never reset.
    is_live       INTEGER NOT NULL DEFAULT 0
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
    items_changed INTEGER NOT NULL DEFAULT 0,
    -- Newest publication date in the feed at this run; NULL when undated.
    newest_item  TEXT
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
    -- The title of the revision before this one (by seen_at, id); NULL for the first.
    prev_title    TEXT,
    -- 1 when only case, punctuation or spacing differ from prev_title (`title_key`).
    is_minor      INTEGER NOT NULL DEFAULT 0,
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

# Every title and summary an article has carried, for `search --history`.
_REVISIONS_FTS_SCHEMA: Final = """
CREATE VIRTUAL TABLE IF NOT EXISTS headline_revisions_fts USING fts5(
    title,
    summary,
    content='headline_revisions',
    content_rowid='id',
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS headline_revisions_fts_insert AFTER INSERT ON headline_revisions
BEGIN
    INSERT INTO headline_revisions_fts(rowid, title, summary)
    VALUES (new.id, new.title, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS headline_revisions_fts_delete AFTER DELETE ON headline_revisions
BEGIN
    INSERT INTO headline_revisions_fts(headline_revisions_fts, rowid, title, summary)
    VALUES ('delete', old.id, old.title, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS headline_revisions_fts_update AFTER UPDATE ON headline_revisions
BEGIN
    INSERT INTO headline_revisions_fts(headline_revisions_fts, rowid, title, summary)
    VALUES ('delete', old.id, old.title, old.summary);
    INSERT INTO headline_revisions_fts(rowid, title, summary)
    VALUES (new.id, new.title, new.summary);
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
    # Newest publication date seen in the feed at the last successful run.
    newest_item: datetime | None = None


@dataclass(frozen=True, slots=True)
class InsertResult:
    """What one `store_headlines` call changed."""

    new: int
    retitled: int
    retitled_live: int = 0
    # Rewrites that only changed case, punctuation or spacing (see `title_key`).
    retitled_minor: int = 0


@dataclass(frozen=True, slots=True)
class TitleChange:
    """One observed rewrite of an article's headline."""

    source: str
    url: str
    changed_at: datetime | None
    # None for an article's first headline, which only `live="only"` returns.
    old_title: str | None
    new_title: str
    is_live: bool = False
    # Only case, punctuation or spacing changed (see `title_key`).
    is_minor: bool = False


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One article found by `search_history`."""

    headline: Headline
    # The earlier title whose version matched, or None when the current one did.
    matched_title: str | None


@dataclass(frozen=True, slots=True)
class UpgradePlan:
    """What `migrate` would do to an existing database, for `--dry-run`."""

    from_version: int
    to_version: int
    headlines: int
    merged_urls: int
    rows_merged: int
    live_articles: int = 0
    urls_normalised: int = 0
    rows_folded: int = 0
    revisions_linked: int = 0

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
    live_articles = len(_live_urls(conn)) if version < 2 else 0
    urls_normalised = rows_folded = 0
    if version < 3:
        targets, groups = _normalisation_plan(conn)
        urls_normalised = sum(1 for _, (old, new) in targets.items() if old != new)
        rows_folded = sum(len({targets[i][0] for i in ids}) - 1 for ids in groups.values())
    revisions_linked = 0
    if version < 4 and _table_exists(conn, "headline_revisions"):
        revisions_linked = int(
            conn.execute("SELECT COUNT(*) FROM headline_revisions").fetchone()[0]
        )
    return UpgradePlan(
        version,
        SCHEMA_VERSION,
        int(total),
        int(merged_urls),
        rows_merged,
        live_articles,
        urls_normalised,
        rows_folded,
        revisions_linked,
    )


def _normalisation_plan(
    conn: sqlite3.Connection,
) -> tuple[dict[int, tuple[str, str]], dict[str, list[int]]]:
    """Each headline's (stored, re-normalised) URL, and URLs that now collide.

    Collisions map the new URL to its row ids, oldest first. On a version-0
    database (several rows per URL) these are counted after the v1 merge.
    """
    targets: dict[int, tuple[str, str]] = {}
    by_new: dict[str, set[str]] = {}  # distinct stored URLs per new URL
    ids_by_new: dict[str, list[int]] = {}
    for row in conn.execute("SELECT id, url FROM headlines ORDER BY id").fetchall():
        new_url = normalise_url(row[1])
        targets[row[0]] = (row[1], new_url)
        by_new.setdefault(new_url, set()).add(row[1])
        ids_by_new.setdefault(new_url, []).append(row[0])
    groups = {url: ids_by_new[url] for url, olds in by_new.items() if len(olds) > 1}
    return targets, groups


def _live_urls(conn: sqlite3.Connection) -> set[str]:
    """URLs whose current or any earlier title looks like a live blog (built-in rules)."""
    titles: dict[str, list[str]] = {}
    for row in conn.execute("SELECT url, title FROM headlines").fetchall():
        titles.setdefault(row[0], []).append(row[1])
    if _table_exists(conn, "headline_revisions"):
        for row in conn.execute(
            "SELECT h.url, r.title FROM headline_revisions r JOIN headlines h"
            " ON h.id = r.headline_id"
        ).fetchall():
            titles.setdefault(row[0], []).append(row[1])
    return {url for url, seen in titles.items() if any(looks_live(url, t) for t in seen)}


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


def _upgrade_to_v2(conn: sqlite3.Connection) -> None:
    """Add `headlines.is_live` and flag existing live blogs from their URLs and titles.

    Only the built-in rules apply here; a source's own `live_url_pattern`
    flags its articles the next time they are fetched.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(headlines)").fetchall()}
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "is_live" not in columns:
            conn.execute("ALTER TABLE headlines ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0")
        live = _live_urls(conn)
        conn.executemany("UPDATE headlines SET is_live = 1 WHERE url = ?", [(u,) for u in live])
        conn.execute("PRAGMA user_version = 2")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()
    if live:
        logger.info("upgrading database: flagged %d live blog(s)", len(live))


def _upgrade_to_v3(conn: sqlite3.Connection) -> None:
    """Re-normalise stored URLs, folding rows that now share one, and index history.

    URL normalisation now strips more tracking parameters (`at_*`), so a
    stored URL can change and two rows can turn out to be one article. Each
    such group keeps its oldest row, takes over the others' title history and
    keeps the most recently seen title. Hashes depend on the URL, so they are
    recomputed for every row and revision.
    """
    targets, groups = _normalisation_plan(conn)
    changed = sum(1 for old, new in targets.values() if old != new)
    if changed:
        backup = _backup(conn, "pre-v3")
        logger.warning(
            "upgrading database: re-normalising %d URL(s), folding %d row(s) (backup: %s)",
            changed,
            sum(len(ids) - 1 for ids in groups.values()),
            backup,
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # `migrate` has just created the history index empty. Fill it before
        # touching any revision: its update/delete triggers assume every row
        # is already indexed, and removing an unindexed row corrupts FTS5.
        if _table_exists(conn, "headline_revisions_fts"):
            conn.execute(
                "INSERT INTO headline_revisions_fts(headline_revisions_fts) VALUES ('rebuild')"
            )
        keepers = set()
        for ids in groups.values():
            _fold_rows(conn, keeper=ids[0], others=ids[1:])
            keepers.add(ids[0])
        for headline_id, (old_url, new_url) in targets.items():
            # Keepers always need rehashing: they hold moved revisions and may
            # have taken another row's title.
            if (old_url == new_url and headline_id not in keepers) or not _row_exists(
                conn, headline_id
            ):
                continue
            title = conn.execute(
                "SELECT title FROM headlines WHERE id = ?", (headline_id,)
            ).fetchone()[0]
            conn.execute(
                "UPDATE headlines SET url = ?, content_hash = ? WHERE id = ?",
                (new_url, compute_hash(new_url, title), headline_id),
            )
            _rehash_revisions(conn, headline_id, new_url)
        conn.execute("PRAGMA user_version = 3")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, definition in columns.items():
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _upgrade_to_v4(conn: sqlite3.Connection) -> None:
    """Store each revision's previous title and minor flag; add `fetch_log.newest_item`.

    Before v4 these were derived on every query with a window function over
    the whole history plus a Python call per row. Additive and derived from
    data already stored, so no backup is taken.
    """
    count = int(conn.execute("SELECT COUNT(*) FROM headline_revisions").fetchone()[0])
    if count:
        logger.info("upgrading database: recording previous titles for %d revision(s)", count)
    conn.execute("BEGIN IMMEDIATE")
    try:
        _add_missing_columns(
            conn,
            "headline_revisions",
            {"prev_title": "TEXT", "is_minor": "INTEGER NOT NULL DEFAULT 0"},
        )
        _add_missing_columns(conn, "fetch_log", {"newest_item": "TEXT"})
        ids = [row[0] for row in conn.execute("SELECT id FROM headlines").fetchall()]
        for headline_id in ids:
            _link_revisions(conn, headline_id)
        conn.execute("PRAGMA user_version = 4")
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def _link_revisions(conn: sqlite3.Connection, headline_id: int) -> None:
    """Set `prev_title` and `is_minor` on every revision of one article."""
    previous: str | None = None
    updates = []
    for rev_id, title, stored_prev, stored_minor in conn.execute(
        "SELECT id, title, prev_title, is_minor FROM headline_revisions"
        " WHERE headline_id = ? ORDER BY seen_at, id",
        (headline_id,),
    ).fetchall():
        minor = int(previous is not None and is_minor_change(previous, title))
        if stored_prev != previous or stored_minor != minor:
            updates.append((previous, minor, rev_id))
        previous = title
    if updates:
        conn.executemany(
            "UPDATE headline_revisions SET prev_title = ?, is_minor = ? WHERE id = ?", updates
        )


def _row_exists(conn: sqlite3.Connection, headline_id: int) -> bool:
    return (
        conn.execute("SELECT 1 FROM headlines WHERE id = ?", (headline_id,)).fetchone() is not None
    )


def _rehash_revisions(conn: sqlite3.Connection, headline_id: int, url: str) -> None:
    """Recompute revision hashes for a new URL, dropping revisions that now repeat."""
    seen: set[str] = set()
    for rev_id, title, old_hash in conn.execute(
        "SELECT id, title, content_hash FROM headline_revisions WHERE headline_id = ?"
        " ORDER BY seen_at, id",
        (headline_id,),
    ).fetchall():
        new_hash = compute_hash(url, title)
        if new_hash in seen:
            conn.execute("DELETE FROM headline_revisions WHERE id = ?", (rev_id,))
            continue
        seen.add(new_hash)
        if new_hash != old_hash:
            conn.execute(
                "UPDATE headline_revisions SET content_hash = ? WHERE id = ?", (new_hash, rev_id)
            )


def _fold_rows(conn: sqlite3.Connection, *, keeper: int, others: list[int]) -> None:
    """Merge `others` into `keeper`: one article seen under several stored URLs."""
    marks = ",".join("?" * len(others))
    # Hashes are about to be recomputed, so park the moved revisions on unique
    # placeholder hashes; `_rehash_revisions` then drops true repeats.
    conn.execute(
        f"""
        UPDATE headline_revisions
        SET headline_id = ?, content_hash = 'fold:' || id
        WHERE headline_id IN ({marks})
        """,
        (keeper, *others),
    )
    group = (keeper, *others)
    all_marks = ",".join("?" * len(group))
    first_seen, published, live = conn.execute(
        f"""
        SELECT MIN(fetched_at), MIN(published_at), MAX(is_live)
        FROM headlines WHERE id IN ({all_marks})
        """,
        group,
    ).fetchone()
    conn.execute(f"DELETE FROM headlines WHERE id IN ({marks})", others)
    latest = conn.execute(
        "SELECT title, summary FROM headline_revisions WHERE headline_id = ?"
        " ORDER BY seen_at DESC, id DESC LIMIT 1",
        (keeper,),
    ).fetchone()
    conn.execute(
        """
        UPDATE headlines
        SET fetched_at = ?, published_at = COALESCE(published_at, ?), is_live = ?,
            title = COALESCE(?, title), summary = COALESCE(?, summary)
        WHERE id = ?
        """,
        (
            first_seen,
            published,
            live,
            latest[0] if latest else None,
            latest[1] if latest else None,
            keeper,
        ),
    )


def migrate(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and apply pending upgrades. Safe on every start."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    try:
        conn.executescript(_FTS_SCHEMA)
        conn.executescript(_REVISIONS_FTS_SCHEMA)
    except sqlite3.OperationalError as exc:
        logger.debug("FTS5 unavailable (%s); search will use LIKE", exc)
    conn.commit()
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 1:
        _upgrade_to_v1(conn)
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 2:
        _upgrade_to_v2(conn)
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 3:
        _upgrade_to_v3(conn)
    if int(conn.execute("PRAGMA user_version").fetchone()[0]) < 4:
        _upgrade_to_v4(conn)


def _prepare(conn: sqlite3.Connection) -> None:
    """Row access by name."""
    conn.row_factory = sqlite3.Row


def connect(
    path: Path | str = DEFAULT_DB_PATH, *, migrate_schema: bool = True
) -> sqlite3.Connection:
    """Open (and by default migrate) the database at `path`."""
    db_path = Path(path)
    if db_path.parent and str(db_path.parent) not in {"", "."}:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    _prepare(conn)
    if migrate_schema:
        migrate(conn)
    return conn


def connect_readonly(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open an existing database for reading only: no schema creation or upgrade.

    Raises `sqlite3.OperationalError` when the file does not exist.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30.0, check_same_thread=False)
    _prepare(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    """The database's `PRAGMA user_version`."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


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


def _add_revision(
    conn: sqlite3.Connection, headline_id: int, headline: Headline, *, first: bool = False
) -> bool:
    """Record `headline`'s title for this article; False if it was seen before.

    Keeps `prev_title`/`is_minor` right for the whole article, even when the
    new revision is not the latest by time (a feed reporting an older version).
    """
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
    added = cursor.rowcount == 1
    if added and not first:
        _link_revisions(conn, headline_id)
    return added


def store_headlines(conn: sqlite3.Connection, headlines: Iterable[Headline]) -> InsertResult:
    """Store headlines, one row per article URL, keeping every distinct title.

    A URL not seen before is a new row. A known URL under a different title
    becomes the row's current title and, the first time that title is seen
    for the article, a new revision. Titles that only differ in case or
    whitespace are the same title (see `compute_hash`). An article stays a
    live blog once any version of it looked like one.
    """
    new = retitled = retitled_live = retitled_minor = 0
    with conn:
        for headline in headlines:
            existing = conn.execute(
                "SELECT id, title, content_hash, is_live FROM headlines WHERE url = ?",
                (headline.url,),
            ).fetchone()
            if existing is None:
                cursor = conn.execute(
                    """
                    INSERT INTO headlines
                        (source, title, url, published_at, fetched_at, summary, content_hash,
                         is_live)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        headline.source,
                        headline.title,
                        headline.url,
                        _iso(headline.published_at),
                        _iso(headline.fetched_at),
                        headline.summary,
                        headline.content_hash,
                        int(headline.is_live),
                    ),
                )
                headline_id = cursor.lastrowid
                assert headline_id is not None
                _add_revision(conn, headline_id, headline, first=True)
                new += 1
                continue

            is_live = bool(existing["is_live"]) or headline.is_live
            if existing["content_hash"] == headline.content_hash:
                if is_live and not existing["is_live"]:
                    conn.execute("UPDATE headlines SET is_live = 1 WHERE id = ?", (existing["id"],))
                continue
            # A title flipping back to an earlier one updates the current
            # title but adds no revision, so feeds that alternate between
            # two wordings do not grow the history every run.
            if _add_revision(conn, existing["id"], headline):
                retitled += 1
                retitled_live += int(is_live)
                retitled_minor += int(is_minor_change(existing["title"], headline.title))
            conn.execute(
                """
                UPDATE headlines SET title = ?, summary = ?, content_hash = ?, is_live = ?
                WHERE id = ?
                """,
                (
                    headline.title,
                    headline.summary,
                    headline.content_hash,
                    int(is_live),
                    existing["id"],
                ),
            )
    return InsertResult(
        new=new, retitled=retitled, retitled_live=retitled_live, retitled_minor=retitled_minor
    )


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
    newest_item: datetime | None = None,
) -> None:
    """Append one row to `fetch_log`."""
    with conn:
        conn.execute(
            """
            INSERT INTO fetch_log
                (source, started_at, finished_at, status, items_found, items_new,
                 items_changed, error, newest_item)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                _iso(newest_item),
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
        is_live=bool(row["is_live"]),
    )


_SELECT_COLUMNS: Final = (
    "source, title, url, published_at, fetched_at, summary, content_hash, is_live"
)
# COALESCE so items without a publication date still sort by when we saw them.
_ORDER_BY: Final = "ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC"


def _in_sources(column: str, sources: Sequence[str] | None) -> tuple[str | None, list[Any]]:
    """A `column IN (...)` clause for a set of source names (case-insensitive).

    None means no filter; an empty list matches nothing (e.g. a tag whose
    sources have all been removed).
    """
    if sources is None:
        return None, []
    if not sources:
        return "0", []
    marks = ",".join("?" * len(sources))
    return f"{column} COLLATE NOCASE IN ({marks})", list(sources)


def list_headlines(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    sources: Sequence[str] | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Headline]:
    """Most recent headlines first, optionally filtered by age and source(s)."""
    clauses: list[str] = []
    params: list[Any] = []
    in_clause, in_params = _in_sources("source", sources)
    if in_clause:
        clauses.append(in_clause)
        params.extend(in_params)
    if since is not None:
        clauses.append("COALESCE(published_at, fetched_at) >= ?")
        params.append(_iso(since))
    if source:
        clauses.append("source = ? COLLATE NOCASE")
        params.append(source)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.extend([max(1, limit), max(0, offset)])
    with closing(
        conn.execute(
            f"SELECT {_SELECT_COLUMNS}, id FROM headlines {where} {_ORDER_BY} LIMIT ? OFFSET ?",
            params,
        )
    ) as cursor:
        return [_row_to_headline(row) for row in cursor.fetchall()]


LiveFilter = Literal["exclude", "include", "only"]


def _changes_query(
    *,
    since: datetime | None,
    source: str | None,
    live: LiveFilter,
    sources: Sequence[str] | None = None,
    minor: bool = True,
) -> tuple[str, list[Any]]:
    """FROM/WHERE for `list_title_changes` and `count_title_changes`."""
    # A live blog's first headline is part of its timeline, so `only` keeps it.
    clauses = [] if live == "only" else ["r.prev_title IS NOT NULL"]
    if not minor:
        clauses.append("r.is_minor = 0")
    params: list[Any] = []
    if live == "exclude":
        clauses.append("h.is_live = 0")
    elif live == "only":
        clauses.append("h.is_live = 1")
    if since is not None:
        clauses.append("r.seen_at >= ?")
        params.append(_iso(since))
    if source:
        clauses.append("h.source = ? COLLATE NOCASE")
        params.append(source)
    in_clause, in_params = _in_sources("h.source", sources)
    if in_clause:
        clauses.append(in_clause)
        params.extend(in_params)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        FROM headline_revisions r
        JOIN headlines h ON h.id = r.headline_id
        {where}
    """
    return sql, params


def list_title_changes(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    sources: Sequence[str] | None = None,
    limit: int = 50,
    live: LiveFilter = "include",
    oldest_first: bool = False,
    offset: int = 0,
    minor: bool = True,
) -> list[TitleChange]:
    """Headline rewrites, most recent first: each revision after an article's first.

    `live` keeps, drops or isolates live blogs. With `"only"` the result is
    each live blog's timeline, its first headline included (`old_title` None).
    `minor=False` drops rewrites that only changed case, punctuation or spacing.
    """
    sql, params = _changes_query(
        since=since, source=source, live=live, sources=sources, minor=minor
    )
    order = "ASC" if oldest_first else "DESC"
    with closing(
        conn.execute(
            f"""
            SELECT h.source, h.url, h.is_live, r.seen_at, r.prev_title, r.title, r.is_minor
            {sql}
            ORDER BY r.seen_at {order}, r.id {order}
            LIMIT ? OFFSET ?
            """,
            [*params, max(1, limit), max(0, offset)],
        )
    ) as cursor:
        return [
            TitleChange(
                source=row["source"],
                url=row["url"],
                changed_at=_parse_iso(row["seen_at"]),
                old_title=row["prev_title"],
                new_title=row["title"],
                is_live=bool(row["is_live"]),
                is_minor=bool(row["is_minor"]),
            )
            for row in cursor.fetchall()
        ]


def count_title_changes(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    sources: Sequence[str] | None = None,
    live: LiveFilter = "include",
    minor: bool = True,
) -> int:
    """How many rows `list_title_changes` would return without a limit."""
    sql, params = _changes_query(
        since=since, source=source, live=live, sources=sources, minor=minor
    )
    return int(conn.execute(f"SELECT COUNT(*) {sql}", params).fetchone()[0])


def hidden_changes(
    conn: sqlite3.Connection,
    *,
    since: datetime | None = None,
    source: str | None = None,
    sources: Sequence[str] | None = None,
    live: LiveFilter = "exclude",
    minor: bool = False,
) -> tuple[int, int]:
    """How many rewrites the `live` and `minor` filters hide: (live blogs, minor).

    Each count is measured with the other filter as given, so the two add up
    to what showing everything would add. A live blog's first headline is not
    a rewrite, so it never counts.
    """
    scope: dict[str, Any] = {"since": since, "source": source, "sources": sources}
    hidden_live = (
        count_title_changes(conn, **scope, live="include", minor=minor)
        - count_title_changes(conn, **scope, live="exclude", minor=minor)
        if live == "exclude"
        else 0
    )
    hidden_minor = (
        count_title_changes(conn, **scope, live=live, minor=True)
        - count_title_changes(conn, **scope, live=live, minor=False)
        if not minor
        else 0
    )
    return hidden_live, hidden_minor


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
    sources: Sequence[str] | None = None,
) -> list[Headline]:
    """Full-text search over titles and summaries, falling back to LIKE."""
    term = query.strip()
    if not term:
        return []
    in_clause, in_params = _in_sources("h.source", sources)
    and_sources = f"AND {in_clause}" if in_clause else ""

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
                        WHERE headlines_fts MATCH ? {and_sources}
                        ORDER BY bm25(headlines_fts), COALESCE(h.published_at, h.fetched_at) DESC
                        LIMIT ?
                        """,
                        (match, *in_params, max(1, limit)),
                    )
                ) as cursor:
                    return [_row_to_headline(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError as exc:
                logger.debug("FTS query failed (%s); falling back to LIKE", exc)

    pattern = f"%{_escape_like(term)}%"
    with closing(
        conn.execute(
            f"""
            SELECT {_SELECT_COLUMNS}, id FROM headlines h
            WHERE (title LIKE ? ESCAPE '\\' OR COALESCE(summary, '') LIKE ? ESCAPE '\\')
            {and_sources}
            {_ORDER_BY}
            LIMIT ?
            """,
            (pattern, pattern, *in_params, max(1, limit)),
        )
    ) as cursor:
        return [_row_to_headline(row) for row in cursor.fetchall()]


def search_history(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 50,
    sources: Sequence[str] | None = None,
) -> list[SearchHit]:
    """Search every title and summary an article has carried; one hit per article.

    Articles rank by their best-matching version. A hit carries the matched
    earlier title when that version was not the current one.
    """
    term = query.strip()
    if not term:
        return []

    columns = ", ".join(f"h.{c}" for c in _SELECT_COLUMNS.split(", "))
    in_clause, in_params = _in_sources("h.source", sources)
    and_sources = f"AND {in_clause}" if in_clause else ""
    # The version reported per article: the current one whenever it matches,
    # otherwise the best-ranked earlier one. Articles rank by their best score.
    best_per_article = """
        ROW_NUMBER() OVER (
            PARTITION BY r.headline_id
            ORDER BY r.content_hash = h.content_hash DESC, m.score, r.seen_at DESC, r.id DESC
        ) AS rn,
        MIN(m.score) OVER (PARTITION BY r.headline_id) AS best
    """
    if _table_exists(conn, "headline_revisions_fts"):
        match = _fts_query(term)
        if match:
            try:
                with closing(
                    conn.execute(
                        f"""
                        WITH m AS (
                            SELECT rowid AS rid, bm25(headline_revisions_fts) AS score
                            FROM headline_revisions_fts
                            WHERE headline_revisions_fts MATCH ?
                        ),
                        ranked AS (
                            SELECT r.headline_id, r.title AS matched, {best_per_article}
                            FROM m
                            JOIN headline_revisions r ON r.id = m.rid
                            JOIN headlines h ON h.id = r.headline_id
                        )
                        SELECT {columns}, ranked.matched
                        FROM ranked JOIN headlines h ON h.id = ranked.headline_id
                        WHERE ranked.rn = 1 {and_sources}
                        ORDER BY ranked.best, COALESCE(h.published_at, h.fetched_at) DESC
                        LIMIT ?
                        """,
                        (match, *in_params, max(1, limit)),
                    )
                ) as cursor:
                    return [_row_to_hit(row) for row in cursor.fetchall()]
            except sqlite3.OperationalError as exc:
                logger.debug("FTS history query failed (%s); falling back to LIKE", exc)

    pattern = f"%{_escape_like(term)}%"
    with closing(
        conn.execute(
            f"""
            WITH m AS (
                SELECT id AS rid, 0 AS score FROM headline_revisions
                WHERE title LIKE ? ESCAPE '\\' OR COALESCE(summary, '') LIKE ? ESCAPE '\\'
            ),
            ranked AS (
                SELECT r.headline_id, r.title AS matched, {best_per_article}
                FROM m
                JOIN headline_revisions r ON r.id = m.rid
                JOIN headlines h ON h.id = r.headline_id
            )
            SELECT {columns}, ranked.matched
            FROM ranked JOIN headlines h ON h.id = ranked.headline_id
            WHERE ranked.rn = 1 {and_sources}
            ORDER BY COALESCE(h.published_at, h.fetched_at) DESC, h.id DESC
            LIMIT ?
            """,
            (pattern, pattern, *in_params, max(1, limit)),
        )
    ) as cursor:
        return [_row_to_hit(row) for row in cursor.fetchall()]


def _row_to_hit(row: sqlite3.Row) -> SearchHit:
    headline = _row_to_headline(row)
    matched = row["matched"]
    return SearchHit(
        headline=headline, matched_title=None if matched == headline.title else matched
    )


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
            SELECT finished_at, newest_item FROM fetch_log
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
                newest_item=_parse_iso(success_row["newest_item"]) if success_row else None,
                last_status=last_row["status"] if last_row else None,
                last_error=last_row["error"] if last_row else None,
            )
        )
    return statuses


@dataclass(frozen=True, slots=True)
class Revision:
    """One title an article has carried, with when it was first seen."""

    title: str
    summary: str | None
    seen_at: datetime | None


def article_history(conn: sqlite3.Connection, url: str) -> tuple[Headline, list[Revision]] | None:
    """An article by its stored URL and every title it has carried, oldest first."""
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS}, id FROM headlines WHERE url = ?", (url,)
    ).fetchone()
    if row is None:
        return None
    with closing(
        conn.execute(
            """
            SELECT title, summary, seen_at FROM headline_revisions
            WHERE headline_id = ? ORDER BY seen_at, id
            """,
            (row["id"],),
        )
    ) as cursor:
        revisions = [
            Revision(title=rev["title"], summary=rev["summary"], seen_at=_parse_iso(rev["seen_at"]))
            for rev in cursor.fetchall()
        ]
    return _row_to_headline(row), revisions


def revision_counts(conn: sqlite3.Connection, urls: Sequence[str]) -> dict[str, int]:
    """How many distinct titles each of `urls` has carried (1 = never rewritten)."""
    if not urls:
        return {}
    marks = ",".join("?" * len(urls))
    with closing(
        conn.execute(
            f"""
            SELECT h.url, COUNT(r.id) AS titles
            FROM headlines h JOIN headline_revisions r ON r.headline_id = h.id
            WHERE h.url IN ({marks})
            GROUP BY h.id
            """,
            list(urls),
        )
    ) as cursor:
        return {row["url"]: int(row["titles"]) for row in cursor.fetchall()}


@dataclass(frozen=True, slots=True)
class RunSummary:
    """One `fetch` run, rebuilt from its `fetch_log` rows."""

    started_at: datetime
    finished_at: datetime
    ok: int
    skipped: int
    failed: int
    found: int
    new: int
    changed: int
    failed_sources: tuple[str, ...] = ()


# fetch_log has no run id. A run's sources start within seconds of each other;
# scheduled runs are hours apart, so a quiet gap this long separates two runs.
RUN_GAP: Final = timedelta(minutes=10)


def recent_runs(conn: sqlite3.Connection, *, limit: int = 12) -> list[RunSummary]:
    """The most recent fetch runs, newest first."""
    # Enough rows for `limit` runs of every configured source, generously.
    with closing(
        conn.execute(
            """
            SELECT source, started_at, finished_at, status, items_found, items_new,
                   items_changed
            FROM fetch_log ORDER BY started_at DESC, id DESC LIMIT ?
            """,
            (max(1, limit) * 200,),
        )
    ) as cursor:
        rows = cursor.fetchall()

    groups: list[list[sqlite3.Row]] = []
    previous: datetime | None = None
    for row in rows:
        started = _parse_iso(row["started_at"])
        if started is None:
            continue
        if previous is None or previous - started > RUN_GAP:
            if len(groups) == limit:
                break
            groups.append([])
        groups[-1].append(row)
        previous = started

    runs: list[RunSummary] = []
    for group in groups:
        starts = [_parse_iso(row["started_at"]) for row in group]
        ends = [_parse_iso(row["finished_at"]) for row in group]
        runs.append(
            RunSummary(
                started_at=min(value for value in starts if value),
                finished_at=max(value for value in ends if value),
                ok=sum(1 for row in group if row["status"] == "ok"),
                skipped=sum(1 for row in group if row["status"] == "skipped"),
                failed=sum(1 for row in group if row["status"] == "error"),
                found=sum(int(row["items_found"]) for row in group),
                new=sum(int(row["items_new"]) for row in group),
                changed=sum(int(row["items_changed"]) for row in group),
                failed_sources=tuple(
                    sorted({row["source"] for row in group if row["status"] == "error"})
                ),
            )
        )
    return runs


@dataclass(frozen=True, slots=True)
class Totals:
    """Database-wide counts for the web viewer's header."""

    articles: int
    # As the Rewrites page counts them by default: no live blogs, no minor changes.
    rewrites: int
    live: int
    last_fetch: datetime | None


def totals(conn: sqlite3.Connection) -> Totals:
    """Article, rewrite and live-blog counts plus the latest fetch time."""
    articles, live = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(is_live), 0) FROM headlines"
    ).fetchone()
    last = conn.execute("SELECT MAX(finished_at) FROM fetch_log").fetchone()[0]
    return Totals(
        articles=int(articles),
        rewrites=count_title_changes(conn, live="exclude", minor=False),
        live=int(live),
        last_fetch=_parse_iso(last),
    )


# --- Trends ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FirstSeen:
    """When an article was first stored, for per-day counts."""

    source: str
    fetched_at: datetime
    is_live: bool


def first_seen(conn: sqlite3.Connection, *, since: datetime) -> list[FirstSeen]:
    """Every article first stored since `since`, oldest first."""
    with closing(
        conn.execute(
            "SELECT source, fetched_at, is_live FROM headlines WHERE fetched_at >= ?"
            " ORDER BY fetched_at",
            (_iso(since),),
        )
    ) as cursor:
        rows = []
        for row in cursor.fetchall():
            when = _parse_iso(row["fetched_at"])
            if when is not None:
                rows.append(FirstSeen(row["source"], when, bool(row["is_live"])))
        return rows


@dataclass(frozen=True, slots=True)
class RewriteStat:
    """How often one outlet rewrites its headlines, live blogs excluded."""

    source: str
    articles: int
    rewritten: int
    # From first seen to the first rewrite that changed words, per rewritten article.
    delays: tuple[timedelta, ...]

    @property
    def share(self) -> float:
        return self.rewritten / self.articles if self.articles else 0.0

    @property
    def median_delay(self) -> timedelta | None:
        if not self.delays:
            return None
        ordered = sorted(self.delays)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2


def rewrite_stats(conn: sqlite3.Connection, *, since: datetime) -> list[RewriteStat]:
    """Per source, of articles first seen since `since`: how many were reworded, and how soon.

    Minor rewrites (case, punctuation, spacing) do not count.
    """
    with closing(
        conn.execute(
            """
            SELECT h.id, h.source, r.seen_at, r.is_minor
            FROM headlines h JOIN headline_revisions r ON r.headline_id = h.id
            WHERE h.fetched_at >= ? AND h.is_live = 0
            ORDER BY h.id, r.seen_at, r.id
            """,
            (_iso(since),),
        )
    ) as cursor:
        rows = cursor.fetchall()

    articles: dict[str, int] = {}
    rewritten: dict[str, int] = {}
    delays: dict[str, list[timedelta]] = {}
    current = None
    first_at: datetime | None = None
    done = False
    for row in rows:
        source = row["source"]
        if row["id"] != current:
            current, done = row["id"], False
            first_at = _parse_iso(row["seen_at"])
            articles[source] = articles.get(source, 0) + 1
            continue
        if done or row["is_minor"]:
            continue
        done = True
        rewritten[source] = rewritten.get(source, 0) + 1
        seen = _parse_iso(row["seen_at"])
        if seen is not None and first_at is not None:
            delays.setdefault(source, []).append(seen - first_at)
    return [
        RewriteStat(
            source=source,
            articles=count,
            rewritten=rewritten.get(source, 0),
            delays=tuple(delays.get(source, [])),
        )
        for source, count in articles.items()
    ]


@dataclass(frozen=True, slots=True)
class Turnover:
    """One source's fetch history: reliability and how much of its feed is new each run."""

    source: str
    ok: int
    failed: int
    skipped: int
    found: int
    new: int
    # Successful runs where every item in the feed was new: stories that came
    # and went between those runs were probably missed.
    all_new: int

    @property
    def share_new(self) -> float:
        return self.new / self.found if self.found else 0.0


def feed_turnover(conn: sqlite3.Connection, *, since: datetime) -> list[Turnover]:
    """Per source, over fetch runs since `since`; each source's first-ever run is left out
    (everything is new then)."""
    with closing(
        conn.execute(
            """
            SELECT source, status, items_found, items_new FROM fetch_log
            WHERE started_at >= ?
              AND id NOT IN (SELECT MIN(id) FROM fetch_log GROUP BY source COLLATE NOCASE)
            """,
            (_iso(since),),
        )
    ) as cursor:
        rows = cursor.fetchall()
    stats: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = stats.setdefault(
            row["source"],
            {"ok": 0, "failed": 0, "skipped": 0, "found": 0, "new": 0, "all_new": 0},
        )
        status = row["status"]
        if status == "ok":
            entry["ok"] += 1
            found, new = int(row["items_found"]), int(row["items_new"])
            entry["found"] += found
            entry["new"] += new
            entry["all_new"] += int(found > 0 and new >= found)
        elif status == "error":
            entry["failed"] += 1
        else:
            entry["skipped"] += 1
    return [Turnover(source=source, **entry) for source, entry in stats.items()]
