"""SQLite schema, dedup-on-insert, queries and the fetch log."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from headliner.models import Headline, utcnow
from headliner.store import (
    connect,
    has_fts,
    insert_headlines,
    list_headlines,
    migrate,
    record_fetch,
    search_headlines,
    source_status,
)


def make_headline(
    title: str = "A headline long enough to keep",
    url: str = "https://example.org/story",
    *,
    source: str = "Example Wire",
    published_at: datetime | None = None,
    summary: str | None = None,
) -> Headline:
    return Headline.create(
        source=source,
        title=title,
        url=url,
        published_at=published_at or datetime(2025, 3, 4, 9, 0, tzinfo=UTC),
        summary=summary,
    )


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = connect(tmp_path / "test.db")
    yield connection
    connection.close()


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "twice.db"
    first = connect(path)
    migrate(first)
    migrate(first)
    tables = {
        row["name"]
        for row in first.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {"headlines", "fetch_log"} <= tables
    assert first.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    first.close()


def test_inserting_the_same_headline_twice_yields_one_row(conn: sqlite3.Connection) -> None:
    headline = make_headline()
    assert insert_headlines(conn, [headline]) == 1
    assert insert_headlines(conn, [headline]) == 0

    total = conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0]
    assert total == 1


def test_dedup_survives_tracking_params_and_title_case(conn: sqlite3.Connection) -> None:
    first = make_headline(title="Council approves the new tram line", url="https://example.org/a")
    second = make_headline(
        title="COUNCIL APPROVES THE NEW TRAM LINE",
        url="https://example.org/a/?utm_source=newsletter",
    )
    assert first.content_hash == second.content_hash
    insert_headlines(conn, [first])
    assert insert_headlines(conn, [second]) == 0
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 1


def test_duplicates_within_one_batch_collapse(conn: sqlite3.Connection) -> None:
    headline = make_headline()
    assert insert_headlines(conn, [headline, headline, headline]) == 1


def test_insert_empty_batch_is_a_no_op(conn: sqlite3.Connection) -> None:
    assert insert_headlines(conn, []) == 0


def test_list_headlines_orders_newest_first(conn: sqlite3.Connection) -> None:
    older = make_headline(
        title="An older story worth keeping",
        url="https://example.org/old",
        published_at=datetime(2025, 3, 1, 9, 0, tzinfo=UTC),
    )
    newer = make_headline(
        title="A newer story worth keeping",
        url="https://example.org/new",
        published_at=datetime(2025, 3, 5, 9, 0, tzinfo=UTC),
    )
    insert_headlines(conn, [older, newer])
    titles = [headline.title for headline in list_headlines(conn)]
    assert titles == ["A newer story worth keeping", "An older story worth keeping"]


def test_list_headlines_filters_by_since_and_source(conn: sqlite3.Connection) -> None:
    now = utcnow()
    recent = make_headline(
        title="Something that happened recently",
        url="https://example.org/recent",
        published_at=now - timedelta(hours=1),
    )
    stale = make_headline(
        title="Something that happened last month",
        url="https://example.org/stale",
        published_at=now - timedelta(days=40),
        source="Other Wire",
    )
    insert_headlines(conn, [recent, stale])

    assert len(list_headlines(conn, since=now - timedelta(hours=24))) == 1
    assert len(list_headlines(conn, source="other wire")) == 1
    assert len(list_headlines(conn, limit=1)) == 1


def test_list_headlines_falls_back_to_fetched_at_when_undated(conn: sqlite3.Connection) -> None:
    undated = Headline.create(
        source="Example Wire",
        title="An undated story that is still recent",
        url="https://example.org/undated",
    )
    insert_headlines(conn, [undated])
    assert len(list_headlines(conn, since=utcnow() - timedelta(minutes=5))) == 1


def test_round_trip_preserves_fields(conn: sqlite3.Connection) -> None:
    original = make_headline(summary="A short summary")
    insert_headlines(conn, [original])
    stored = list_headlines(conn)[0]
    assert stored == original


def test_search_finds_by_title_and_summary(conn: sqlite3.Connection) -> None:
    insert_headlines(
        conn,
        [
            make_headline(title="Ferry service restored today", url="https://example.org/ferry"),
            make_headline(
                title="Budget surplus forecast revised",
                url="https://example.org/budget",
                summary="Treasury cites stronger ferry receipts",
            ),
            make_headline(title="Unrelated tram announcement", url="https://example.org/tram"),
        ],
    )
    titles = {headline.title for headline in search_headlines(conn, "ferry")}
    assert titles == {"Ferry service restored today", "Budget surplus forecast revised"}


def test_search_handles_punctuation_and_empty_queries(conn: sqlite3.Connection) -> None:
    insert_headlines(conn, [make_headline(title="Quarter-on-quarter growth stalls")])
    assert search_headlines(conn, '"quarter-on-quarter"')
    assert search_headlines(conn, "   ") == []


def test_search_like_fallback_matches_fts(conn: sqlite3.Connection, tmp_path: Path) -> None:
    insert_headlines(conn, [make_headline(title="Ferry service restored today")])
    assert has_fts(conn)

    # Rebuild the same data without the FTS index to exercise the LIKE path.
    plain = connect(tmp_path / "plain.db", migrate_schema=False)
    plain.executescript(
        """
        CREATE TABLE headlines (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
            summary TEXT, content_hash TEXT NOT NULL UNIQUE
        );
        """
    )
    assert not has_fts(plain)
    insert_headlines(plain, [make_headline(title="Ferry service restored today")])
    assert [h.title for h in search_headlines(plain, "ferry")] == ["Ferry service restored today"]
    plain.close()


def test_search_like_fallback_escapes_wildcards(conn: sqlite3.Connection, tmp_path: Path) -> None:
    plain = connect(tmp_path / "plain2.db", migrate_schema=False)
    plain.executescript(
        """
        CREATE TABLE headlines (
            id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
            summary TEXT, content_hash TEXT NOT NULL UNIQUE
        );
        """
    )
    insert_headlines(plain, [make_headline(title="A perfectly ordinary headline")])
    assert search_headlines(plain, "%") == []
    plain.close()


def test_fetch_log_records_runs_and_feeds_source_status(conn: sqlite3.Connection) -> None:
    started = datetime(2025, 3, 4, 9, 0, tzinfo=UTC)
    finished = datetime(2025, 3, 4, 9, 0, 5, tzinfo=UTC)
    insert_headlines(conn, [make_headline()])
    record_fetch(
        conn,
        source="Example Wire",
        started_at=started,
        finished_at=finished,
        status="ok",
        items_found=1,
        items_new=1,
    )
    record_fetch(
        conn,
        source="Broken Wire",
        started_at=started,
        finished_at=finished,
        status="error",
        items_found=0,
        items_new=0,
        error="HTTP 503",
    )

    statuses = {s.name: s for s in source_status(conn, ["Example Wire", "Broken Wire", "Unseen"])}

    assert statuses["Example Wire"].total_items == 1
    assert statuses["Example Wire"].last_success == finished
    assert statuses["Example Wire"].last_status == "ok"

    assert statuses["Broken Wire"].last_success is None
    assert statuses["Broken Wire"].last_error == "HTTP 503"

    assert statuses["Unseen"].total_items == 0
    assert statuses["Unseen"].last_status is None


def test_connect_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b" / "headlines.db"
    connection = connect(nested)
    connection.close()
    assert nested.exists()
