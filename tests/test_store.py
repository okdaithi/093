"""SQLite schema, dedup-on-insert, queries and the fetch log."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from headliner import store
from headliner.models import Headline, utcnow
from headliner.store import (
    SCHEMA_VERSION,
    connect,
    count_title_changes,
    has_fts,
    insert_headlines,
    list_headlines,
    list_title_changes,
    migrate,
    record_fetch,
    search_headlines,
    source_status,
    store_headlines,
    upgrade_plan,
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


def without_fts5(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `migrate` behave as on an SQLite build that lacks FTS5."""
    monkeypatch.setattr(
        store, "_FTS_SCHEMA", "CREATE VIRTUAL TABLE headlines_fts USING no_such_module(title);"
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


def test_search_like_fallback_matches_fts(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_headlines(conn, [make_headline(title="Ferry service restored today")])
    assert has_fts(conn)

    # Rebuild the same data without the FTS index to exercise the LIKE path.
    without_fts5(monkeypatch)
    plain = connect(tmp_path / "plain.db")
    assert not has_fts(plain)
    insert_headlines(plain, [make_headline(title="Ferry service restored today")])
    assert [h.title for h in search_headlines(plain, "ferry")] == ["Ferry service restored today"]
    plain.close()


def test_search_like_fallback_escapes_wildcards(
    conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    without_fts5(monkeypatch)
    plain = connect(tmp_path / "plain2.db")
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


# --------------------------------------------------------------------------
# title history
# --------------------------------------------------------------------------

STORY_URL = "https://example.org/news/quarantine-centre"
T0 = datetime(2025, 3, 4, 9, 0, tzinfo=UTC)


def seen(title: str, at: datetime, url: str = STORY_URL, source: str = "Example Wire") -> Headline:
    return Headline.create(source=source, title=title, url=url, published_at=T0, fetched_at=at)


def revision_titles(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT title FROM headline_revisions ORDER BY seen_at, id").fetchall()
    return [row[0] for row in rows]


def test_retitled_article_keeps_one_row_and_records_history(conn: sqlite3.Connection) -> None:
    first = store_headlines(conn, [seen("Quarantine centre to become a prison", T0)])
    second = store_headlines(
        conn, [seen("Is this white elephant about to become a prison?", T0 + timedelta(hours=6))]
    )

    assert (first.new, first.retitled) == (1, 0)
    assert (second.new, second.retitled) == (0, 1)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 1
    [current] = list_headlines(conn)
    assert current.title == "Is this white elephant about to become a prison?"
    assert revision_titles(conn) == [
        "Quarantine centre to become a prison",
        "Is this white elephant about to become a prison?",
    ]


def test_case_only_change_is_not_a_retitle(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [seen("Council approves the new tram line", T0)])
    result = store_headlines(conn, [seen("COUNCIL APPROVES THE NEW TRAM LINE", T0)])
    assert (result.new, result.retitled) == (0, 0)
    assert len(revision_titles(conn)) == 1


def test_title_flipping_back_updates_current_but_not_history(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [seen("First wording of the story", T0)])
    store_headlines(conn, [seen("Second wording of the story", T0 + timedelta(hours=1))])
    back = store_headlines(conn, [seen("First wording of the story", T0 + timedelta(hours=2))])

    assert back.retitled == 0
    assert list_headlines(conn)[0].title == "First wording of the story"
    assert revision_titles(conn) == ["First wording of the story", "Second wording of the story"]


def test_search_finds_the_current_title(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [seen("Ferry cancelled by storm", T0)])
    store_headlines(conn, [seen("Ferry service restored after storm", T0 + timedelta(hours=1))])
    assert [h.title for h in search_headlines(conn, "restored")] == [
        "Ferry service restored after storm"
    ]
    assert search_headlines(conn, "cancelled") == []


def test_list_title_changes_reports_old_and_new(conn: sqlite3.Connection) -> None:
    later = T0 + timedelta(hours=6)
    store_headlines(conn, [seen("Original headline wording", T0)])
    store_headlines(conn, [seen("Rewritten headline wording", later)])
    other = "https://example.org/other"
    store_headlines(conn, [seen("Untouched other headline", T0, url=other, source="Other")])

    [change] = list_title_changes(conn)
    assert change.source == "Example Wire"
    assert change.url == STORY_URL
    assert change.changed_at == later
    assert (change.old_title, change.new_title) == (
        "Original headline wording",
        "Rewritten headline wording",
    )
    assert list_title_changes(conn, since=later + timedelta(minutes=1)) == []
    assert list_title_changes(conn, source="other") == []
    assert len(list_title_changes(conn, source="EXAMPLE WIRE")) == 1


def test_fresh_database_is_at_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"
    connection = connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    indexes = {row[1] for row in connection.execute("PRAGMA index_list(headlines)").fetchall()}
    assert "idx_headlines_url" in indexes
    connection.close()
    assert not list(tmp_path.glob("*.bak"))


# --------------------------------------------------------------------------
# upgrade from schema 0 (dedup on url + title)
# --------------------------------------------------------------------------

_V0_SCHEMA = """
CREATE TABLE headlines (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL,
    url TEXT NOT NULL, published_at TEXT, fetched_at TEXT NOT NULL,
    summary TEXT, content_hash TEXT NOT NULL UNIQUE
);
CREATE TABLE fetch_log (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL, status TEXT NOT NULL,
    items_found INTEGER NOT NULL DEFAULT 0, items_new INTEGER NOT NULL DEFAULT 0, error TEXT
);
"""


def make_v0_db(path: Path, headlines: list[Headline]) -> None:
    """A database as the pre-history release left it: one row per url + title."""
    with closing(sqlite3.connect(path)) as legacy:
        legacy.executescript(_V0_SCHEMA)
        legacy.executescript(store._FTS_SCHEMA)
        legacy.executemany(
            "INSERT INTO headlines (source, title, url, published_at, fetched_at, summary,"
            " content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    h.source,
                    h.title,
                    h.url,
                    h.published_at.isoformat() if h.published_at else None,
                    h.fetched_at.isoformat(),
                    h.summary,
                    h.content_hash,
                )
                for h in headlines
            ],
        )
        legacy.commit()


V0_ROWS = [
    seen("Quarantine centre to become a prison", T0),
    seen("Unrelated story that never changed", T0, url="https://example.org/other"),
    seen("Is this white elephant about to become a prison?", T0 + timedelta(hours=6)),
    seen("White elephant centre will become WA's newest prison", T0 + timedelta(hours=12)),
]


def test_upgrade_plan_describes_v0_without_changing_it(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_v0_db(path, V0_ROWS)
    with closing(connect(path, migrate_schema=False)) as legacy:
        plan = upgrade_plan(legacy)
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
    assert plan.needed
    assert (plan.headlines, plan.merged_urls, plan.rows_merged) == (4, 1, 2)


def test_upgrade_from_v0_merges_rows_into_history(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_v0_db(path, V0_ROWS)

    upgraded = connect(path)

    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert upgraded.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 2
    story = upgraded.execute(
        "SELECT id, title, fetched_at FROM headlines WHERE url = ?", (STORY_URL,)
    ).fetchone()
    # The earliest row survives, carrying the latest title.
    assert story["title"] == "White elephant centre will become WA's newest prison"
    assert story["fetched_at"] == T0.isoformat()
    assert len(revision_titles(upgraded)) == 4
    assert [c.new_title for c in list_title_changes(upgraded)] == [
        "White elephant centre will become WA's newest prison",
        "Is this white elephant about to become a prison?",
    ]
    # The FTS index followed the deletes and the title update.
    assert [h.title for h in search_headlines(upgraded, "newest")] == [story["title"]]
    assert search_headlines(upgraded, "quarantine") == []
    # fetch_log gained the new counter, and a URL is now unique.
    store_headlines(upgraded, [seen("Another rewrite of the prison story", T0 + timedelta(days=1))])
    record_fetch(
        upgraded,
        source="Example Wire",
        started_at=T0,
        finished_at=T0,
        status="ok",
        items_found=1,
        items_new=0,
        items_changed=1,
    )
    with pytest.raises(sqlite3.IntegrityError):
        upgraded.execute(
            "INSERT INTO headlines (source, title, url, fetched_at, content_hash)"
            " VALUES ('x', 'y', ?, 'z', 'unique-hash')",
            (STORY_URL,),
        )
    upgraded.close()

    backup = tmp_path / "legacy.db.pre-v1.bak"
    with closing(sqlite3.connect(backup)) as original:
        assert original.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 4
        assert original.execute("PRAGMA user_version").fetchone()[0] == 0


def test_upgrade_is_applied_once(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_v0_db(path, V0_ROWS)
    connect(path).close()
    backup = tmp_path / "legacy.db.pre-v1.bak"
    stamp = backup.stat().st_mtime_ns

    again = connect(path)
    migrate(again)
    assert again.execute("SELECT COUNT(*) FROM headline_revisions").fetchone()[0] == 4
    assert not upgrade_plan(again).needed
    again.close()
    assert backup.stat().st_mtime_ns == stamp


def test_upgrade_without_duplicates_takes_no_backup(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    make_v0_db(path, V0_ROWS[:2])
    upgraded = connect(path)
    assert revision_titles(upgraded) == [V0_ROWS[0].title, V0_ROWS[1].title]
    upgraded.close()
    assert not list(tmp_path.glob("*.bak"))


# --------------------------------------------------------------------------
# live blogs
# --------------------------------------------------------------------------

LIVE_URL = "https://example.org/world/live/2026/oct/02/storm"


def test_live_retitles_are_counted_separately(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [seen("Storm live: winds reach 120km/h on the coast", T0, url=LIVE_URL)])
    store_headlines(conn, [seen("Ordinary first headline here", T0)])
    result = store_headlines(
        conn,
        [
            seen("Storm live: power out for 40,000 homes", T0 + timedelta(hours=1), url=LIVE_URL),
            seen("Ordinary second headline here", T0 + timedelta(hours=1)),
        ],
    )
    assert (result.retitled, result.retitled_live) == (2, 1)
    flags = dict(conn.execute("SELECT url, is_live FROM headlines").fetchall())
    assert flags == {LIVE_URL: 1, STORY_URL: 0}


def test_live_flag_sticks_when_the_title_stops_saying_live(conn: sqlite3.Connection) -> None:
    url = "https://example.org/story-123"
    store_headlines(conn, [seen("Election live: counting under way", T0, url=url)])
    store_headlines(conn, [seen("Labor wins second term", T0 + timedelta(hours=3), url=url)])
    [current] = list_headlines(conn)
    assert current.title == "Labor wins second term"
    assert current.is_live


def test_source_pattern_flags_a_known_article_without_a_new_title(
    conn: sqlite3.Connection,
) -> None:
    url = "https://example.org/as-it-happened/budget"
    store_headlines(conn, [seen("Budget night: every announcement", T0, url=url)])
    assert not list_headlines(conn)[0].is_live
    flagged = Headline.create(
        source="Example Wire",
        title="Budget night: every announcement",
        url=url,
        fetched_at=T0 + timedelta(hours=1),
        live_url_pattern=re.compile("/as-it-happened/"),
    )
    result = store_headlines(conn, [flagged])
    assert (result.new, result.retitled) == (0, 0)
    assert list_headlines(conn)[0].is_live


def seed_live_and_plain(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [seen("Storm live: winds reach 120km/h", T0, url=LIVE_URL)])
    store_headlines(conn, [seen("Storm live: power cut", T0 + timedelta(hours=1), url=LIVE_URL)])
    store_headlines(
        conn, [seen("Storm live: clean-up begins", T0 + timedelta(hours=2), url=LIVE_URL)]
    )
    store_headlines(conn, [seen("Original headline wording", T0)])
    store_headlines(conn, [seen("Rewritten headline wording", T0 + timedelta(hours=1))])


def test_changes_can_exclude_include_or_isolate_live_blogs(conn: sqlite3.Connection) -> None:
    seed_live_and_plain(conn)

    excluded = list_title_changes(conn, live="exclude")
    assert [c.new_title for c in excluded] == ["Rewritten headline wording"]
    assert len(list_title_changes(conn, live="include")) == 3

    timeline = list_title_changes(conn, live="only", oldest_first=True)
    assert [(c.old_title, c.new_title) for c in timeline] == [
        (None, "Storm live: winds reach 120km/h"),
        ("Storm live: winds reach 120km/h", "Storm live: power cut"),
        ("Storm live: power cut", "Storm live: clean-up begins"),
    ]
    assert all(c.is_live for c in timeline)
    assert count_title_changes(conn, live="only") == 3
    assert count_title_changes(conn, live="exclude") == 1


def test_upgrade_from_v1_flags_live_blogs(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    v1 = connect(path)
    # A URL that only an earlier title marks as live, and one marked by its path.
    store_headlines(v1, [seen("Budget live: treasurer rises to speak", T0)])
    store_headlines(v1, [seen("Budget delivers tax cuts", T0 + timedelta(hours=2))])
    store_headlines(v1, [seen("Storm batters the coast tonight", T0, url=LIVE_URL)])
    store_headlines(v1, [seen("Unrelated ordinary story", T0, url="https://example.org/o")])
    v1.execute("ALTER TABLE headlines DROP COLUMN is_live")
    v1.execute("PRAGMA user_version = 1")
    v1.commit()
    v1.close()

    with closing(connect(path, migrate_schema=False)) as before:
        plan = upgrade_plan(before)
    assert (plan.from_version, plan.live_articles, plan.rows_merged) == (1, 2, 0)

    upgraded = connect(path)
    assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    flags = dict(upgraded.execute("SELECT url, is_live FROM headlines").fetchall())
    assert flags == {STORY_URL: 1, LIVE_URL: 1, "https://example.org/o": 0}
    upgraded.close()
    assert not list(tmp_path.glob("*.bak"))
