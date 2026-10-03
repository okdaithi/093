"""Storing front-page headlines next to feed items: one article, two ways of finding it."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from headliner.fetcher import SourceResult
from headliner.frontpage import FrontPageResult
from headliner.models import Headline, match_key, utcnow
from headliner.store import (
    SCHEMA_VERSION,
    article_history,
    connect,
    front_page_status,
    front_page_validators,
    list_headlines,
    list_title_changes,
    record_front_page,
    store_front_page,
    store_headlines,
    upgrade_plan,
)

SOURCE = "Example Daily"


def feed_item(title: str, url: str, **kwargs: object) -> Headline:
    return Headline.create(source=SOURCE, title=title, url=url, **kwargs)  # type: ignore[arg-type]


def page_item(title: str, url: str, position: int = 1, **kwargs: object) -> Headline:
    return Headline.create(
        source=SOURCE,
        title=title,
        url=url,
        acquisition=("front_page",),
        front_page_position=position,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(tmp_path / "test.db")) as connection:
        yield connection


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT url, title, acquisition, front_page_position FROM headlines ORDER BY id"
    ).fetchall()


def test_identical_url_is_one_article_found_both_ways(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [feed_item("Feed wording of the bridge story", "https://x.example/a/1")])
    stored = store_front_page(
        conn, [page_item("Bridge reopens (front-page wording)", "https://x.example/a/1", 3)]
    )
    assert (stored.new, stored.merged, stored.merged_feed) == (0, 1, 1)
    [row] = rows(conn)
    assert row["acquisition"] == "front_page,rss"
    assert row["front_page_position"] == 3
    # The feed's title stands: a front page's display headline is not a rewrite.
    assert row["title"] == "Feed wording of the bridge story"
    assert list_title_changes(conn) == []


@pytest.mark.parametrize(
    "variant",
    [
        "https://x.example/a/1?utm_source=homepage&utm_campaign=top",  # tracking parameters
        "https://x.example/a/1#comments",  # fragment
        "http://www.x.example/a/1/",  # scheme, www and trailing slash
        "https://amp.x.example/a/1/amp",  # AMP host and path
        "https://m.x.example//a/1?outputType=amp",  # mobile host, doubled slash, AMP switch
        "https://www.x.co.uk/a/1",  # the publisher's other domain (bbc.com, bbc.co.uk)
    ],
)
def test_url_variants_merge(conn: sqlite3.Connection, variant: str) -> None:
    store_headlines(conn, [feed_item("Feed wording of the bridge story", "https://x.example/a/1")])
    stored = store_front_page(conn, [page_item("Some other display wording", variant)])
    assert (stored.new, stored.merged_feed) == (0, 1)
    assert len(rows(conn)) == 1


def test_canonical_url_difference_merges_on_the_headline(conn: sqlite3.Connection) -> None:
    """The feed links the canonical URL, the page a different path with the same headline."""
    store_headlines(
        conn, [feed_item("Storm hits coast, thousands without power", "https://x.example/a/9")]
    )
    stored = store_front_page(
        conn,
        [page_item("Storm hits coast — thousands without power", "https://x.example/world/storm")],
    )
    assert stored.merged_feed == 1
    [row] = rows(conn)
    assert row["url"] == "https://x.example/a/9"


def test_similar_but_distinct_headlines_stay_apart(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [feed_item("Fire in Perth suburb kills two", "https://x.example/a/2")])
    stored = store_front_page(
        conn, [page_item("Fire in Perth suburb kills three", "https://x.example/a/3")]
    )
    assert (stored.new, stored.merged) == (1, 0)
    assert [row["acquisition"] for row in rows(conn)] == ["rss", "front_page"]


def test_other_sources_are_never_merged(conn: sqlite3.Connection) -> None:
    store_headlines(
        conn,
        [
            Headline.create(
                source="Other", title="Same words, other outlet", url="https://o.example/1"
            )
        ],
    )
    stored = store_front_page(conn, [page_item("Same words, other outlet", "https://x.example/b")])
    assert stored.new == 1


def test_old_articles_are_not_matched_on_words(conn: sqlite3.Connection) -> None:
    old = utcnow() - timedelta(days=30)
    store_headlines(
        conn, [feed_item("Council approves annual budget", "https://x.example/a/4", fetched_at=old)]
    )
    stored = store_front_page(
        conn, [page_item("Council approves annual budget", "https://x.example/a/5")]
    )
    assert stored.new == 1


def test_feed_after_front_page_takes_over_the_title(conn: sqlite3.Connection) -> None:
    store_front_page(conn, [page_item("Short display headline here", "https://x.example/a/6")])
    stored = store_headlines(
        conn, [feed_item("The full feed headline for this story", "https://x.example/a/6")]
    )
    assert (stored.new, stored.retitled) == (0, 0)
    [row] = rows(conn)
    assert (row["title"], row["acquisition"]) == (
        "The full feed headline for this story",
        "front_page,rss",
    )
    _, revisions = article_history(conn, "https://x.example/a/6") or (None, [])
    assert [revision.title for revision in revisions] == ["The full feed headline for this story"]


def test_feed_after_front_page_under_an_amp_url(conn: sqlite3.Connection) -> None:
    store_front_page(conn, [page_item("Display wording of story", "https://amp.x.example/a/7/amp")])
    stored = store_headlines(
        conn, [feed_item("Feed wording of the story", "https://x.example/a/7")]
    )
    assert stored.new == 0
    [row] = rows(conn)
    assert row["url"] == "https://x.example/a/7"
    assert row["acquisition"] == "front_page,rss"


def test_front_page_only_articles_track_their_own_rewrites(conn: sqlite3.Connection) -> None:
    store_front_page(conn, [page_item("First front-page wording", "https://x.example/a/8")])
    stored = store_front_page(
        conn, [page_item("Second front-page wording", "https://x.example/a/8", 2)]
    )
    assert stored.retitled == 1
    [change] = list_title_changes(conn)
    assert (change.old_title, change.new_title) == (
        "First front-page wording",
        "Second front-page wording",
    )
    assert rows(conn)[0]["front_page_position"] == 2


def test_feed_storage_is_unchanged_without_front_pages(conn: sqlite3.Connection) -> None:
    first = store_headlines(conn, [feed_item("A headline long enough", "https://x.example/c")])
    again = store_headlines(conn, [feed_item("A headline long enough!", "https://x.example/c")])
    assert (first.new, again.retitled) == (1, 1)
    [headline] = list_headlines(conn)
    assert headline.acquisition == ("rss",)
    assert headline.front_page_position is None


def test_provenance_comes_back_on_listed_headlines(conn: sqlite3.Connection) -> None:
    store_headlines(conn, [feed_item("Feed wording of the bridge story", "https://x.example/a/1")])
    store_front_page(
        conn,
        [page_item("Display wording", "https://x.example/a/1", 4, section="world")],
    )
    [headline] = list_headlines(conn)
    assert headline.acquisition == ("front_page", "rss")
    assert headline.front_page_position == 4
    assert headline.front_page_seen_at is not None
    assert headline.section == "world"
    data = headline.as_dict()
    assert data["acquisition"] == ["front_page", "rss"]
    assert data["front_page_position"] == 4


def test_rss_20_front_page_15_overlap_12_is_23_articles(conn: sqlite3.Connection) -> None:
    feed = [
        feed_item(f"Feed story number {n} about the news", f"https://x.example/news/{n}")
        for n in range(1, 21)
    ]
    # 12 of the 15 front-page links are feed stories, linked in varied ways.
    overlap = []
    for n in range(1, 13):
        if n % 4 == 0:
            url = f"https://x.example/news/{n}?utm_source=home"
        elif n % 4 == 1:
            url = f"https://amp.x.example/news/{n}/amp"
        elif n % 4 == 2:
            url = f"https://x.example/story-{n}"  # a different path: matched on its words
        else:
            url = f"https://x.example/news/{n}"
        title = (
            f"Feed story number {n} about the news"
            if n % 4 == 2
            else f"Display headline {n} for the page"
        )
        overlap.append((title, url))
    only_page = [
        (f"Front page exclusive {n} story", f"https://x.example/fp/{n}") for n in (1, 2, 3)
    ]
    page = [
        page_item(title, url, position)
        for position, (title, url) in enumerate([*overlap, *only_page], start=1)
    ]

    feed_stored = store_headlines(conn, feed)
    page_stored = store_front_page(conn, page)

    assert (feed_stored.new, len(page)) == (20, 15)
    assert (page_stored.new, page_stored.merged_feed) == (3, 12)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 23
    counts = dict(
        conn.execute("SELECT acquisition, COUNT(*) FROM headlines GROUP BY acquisition").fetchall()
    )
    assert counts == {"rss": 8, "front_page,rss": 12, "front_page": 3}

    # Running both again changes nothing.
    assert store_headlines(conn, feed).new == 0
    assert store_front_page(conn, page).new == 0
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 23


def test_front_page_log_status_and_validators(conn: sqlite3.Connection) -> None:
    then = datetime(2026, 10, 4, 1, tzinfo=UTC)
    good = FrontPageResult(
        source=SOURCE,
        url="https://x.example/",
        status="healthy",
        started_at=then,
        finished_at=then,
        http_status=200,
        response_ms=420,
        headlines=[page_item("Front page story number one", "https://x.example/a/1")],
        etag='"abc"',
        method="article",
    )
    record_front_page(conn, good)
    later = then + timedelta(hours=6)
    blocked = FrontPageResult(
        source=SOURCE,
        url="https://x.example/",
        status="blocked",
        started_at=later,
        finished_at=later,
        http_status=403,
        error="HTTP 403",
    )
    record_front_page(conn, blocked)

    status = front_page_status(conn, [SOURCE, "Never read"])
    assert list(status) == [SOURCE]
    assert status[SOURCE].status == "blocked"
    assert status[SOURCE].last_success == then
    assert status[SOURCE].error == "HTTP 403"
    assert front_page_validators(conn, [SOURCE]) == {SOURCE: ("https://x.example/", '"abc"', None)}


V4_HEADLINES = """
CREATE TABLE headlines (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
    published_at TEXT, fetched_at TEXT NOT NULL, summary TEXT,
    content_hash TEXT NOT NULL UNIQUE, is_live INTEGER NOT NULL DEFAULT 0
);
INSERT INTO headlines (source, title, url, fetched_at, content_hash)
VALUES ('Example Daily', 'Stored before front pages existed', 'https://x.example/1',
        '2026-10-01T00:00:00+00:00', 'h1');
PRAGMA user_version = 4;
"""


def test_upgrade_from_v4_adds_provenance(tmp_path: Path) -> None:
    path = tmp_path / "v4.db"
    with closing(sqlite3.connect(path)) as raw:
        raw.executescript(V4_HEADLINES)

    with closing(connect(path, migrate_schema=False)) as old:
        assert upgrade_plan(old).from_version == 4

    with closing(connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 5
        [headline] = list_headlines(conn)
        assert headline.acquisition == ("rss",)
        assert headline.front_page_position is None
        assert conn.execute("SELECT COUNT(*) FROM front_page_log").fetchone()[0] == 0


def test_match_key_ignores_presentation_only() -> None:
    base = match_key("https://www.x.example/news/a-story")
    for same in (
        "http://x.example/news/a-story/",
        "https://amp.x.example/news/a-story/amp",
        "https://m.x.example/news//a-story?utm_source=x",
        "https://x.example/news/a-story.amp",
        "https://x.example/news/a-story?amp=1",
    ):
        assert match_key(same) == base
    assert match_key("https://x.example/news/a-story?id=2") != base
    assert match_key("https://x.example/news/b-story") != base


def test_source_result_defaults_keep_feed_behaviour() -> None:
    now = utcnow()
    result = SourceResult(source=SOURCE, status="ok", started_at=now, finished_at=now)
    assert result.feed_ran and result.front_page is None
