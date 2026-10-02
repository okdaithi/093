"""Parser tests run entirely against the committed fixture files."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from headliner.config import Source
from headliner.parsers import (
    ParseError,
    find_feed_links,
    parse,
    parse_datetime,
    parse_feed,
    parse_html,
)


def test_parse_feed_extracts_and_normalises(feed_bytes: bytes, rss_source: Source) -> None:
    headlines = parse_feed(feed_bytes, rss_source)
    titles = [headline.title for headline in headlines]

    assert "Parliament passes long-delayed housing bill" in titles
    # Sub-10-character titles and link-less items are dropped.
    assert "Too short" not in titles
    assert not any("no link at all" in title for title in titles)
    assert len(headlines) == 5


def test_parse_feed_strips_tracking_params_and_unescapes(
    feed_bytes: bytes, rss_source: Source
) -> None:
    by_title = {headline.title: headline for headline in parse_feed(feed_bytes, rss_source)}

    housing = by_title["Parliament passes long-delayed housing bill"]
    assert housing.url == "https://example.org/news/housing-bill"
    assert housing.summary == (
        "The bill clears its final stage after three years of wrangling & two collapsed votes."
    )

    entities = by_title['"Double-escaped" entities & extra spacing']
    assert entities.url == "https://example.org/news/entities"


def test_parse_feed_resolves_relative_links(feed_bytes: bytes, rss_source: Source) -> None:
    urls = {headline.url for headline in parse_feed(feed_bytes, rss_source)}
    assert "https://example.org/news/relative-story" in urls


def test_parse_feed_dates_are_utc_aware_or_none(feed_bytes: bytes, rss_source: Source) -> None:
    by_title = {headline.title: headline for headline in parse_feed(feed_bytes, rss_source)}

    storm = by_title["Storm warning issued for the western seaboard"]
    assert storm.published_at == datetime(2025, 3, 4, 7, 5, tzinfo=UTC)

    undated = by_title["Undated story that still has a usable link"]
    assert undated.published_at is None

    for headline in by_title.values():
        assert headline.fetched_at.tzinfo is UTC
        assert headline.published_at is None or headline.published_at.tzinfo is UTC


def test_parse_feed_honours_limit(feed_bytes: bytes, rss_source: Source) -> None:
    assert len(parse_feed(feed_bytes, rss_source, limit=2)) == 2


def test_parse_feed_raises_on_unparseable_input(rss_source: Source) -> None:
    with pytest.raises(ParseError):
        parse_feed(b"this is not a feed at all", rss_source)


def test_parse_html_extracts_and_normalises(listing_bytes: bytes, html_source: Source) -> None:
    headlines = parse_html(listing_bytes, html_source)
    titles = [headline.title for headline in headlines]

    assert titles == [
        "Council approves new tram line after decade of debate",
        "Ferry service restored following engine repairs",
        "Budget surplus forecast revised upward again",
    ]
    # The footer list sits outside article_selector.
    assert not any("Terms and conditions" in title for title in titles)
    # Anchor-only hrefs, short titles and anchor-less items are all dropped.
    assert not any("Anchor-only" in title for title in titles)


def test_parse_html_resolves_relative_links(listing_bytes: bytes, html_source: Source) -> None:
    urls = [headline.url for headline in parse_html(listing_bytes, html_source)]
    assert urls[0] == "https://text.example.org/story/1234/council-approves-new-tram-line"
    assert urls[1] == "https://example.org/story/5678/ferry-service-restored"


def test_parse_html_deduplicates_repeated_links(listing_bytes: bytes, html_source: Source) -> None:
    urls = [headline.url for headline in parse_html(listing_bytes, html_source)]
    assert len(urls) == len(set(urls))


def test_parse_html_reads_dates_and_summaries(listing_bytes: bytes, html_source: Source) -> None:
    headlines = parse_html(listing_bytes, html_source)
    assert headlines[0].published_at == datetime(2025, 3, 4, 9, 0, tzinfo=UTC)
    assert headlines[1].published_at == datetime(2025, 3, 4, 6, 30, tzinfo=UTC)
    assert headlines[0].summary == "Construction is due to begin in the autumn."


def test_parse_html_without_optional_selectors(listing_bytes: bytes) -> None:
    minimal = Source(
        name="Minimal",
        url="https://text.example.org/",
        type="html",
        article_selector=".topic-container li",
        title_selector="a",
        link_selector="a",
    )
    headlines = parse_html(listing_bytes, minimal)
    assert len(headlines) == 3
    assert all(headline.published_at is None for headline in headlines)
    assert all(headline.summary is None for headline in headlines)


def test_parse_html_honours_limit(listing_bytes: bytes, html_source: Source) -> None:
    assert len(parse_html(listing_bytes, html_source, limit=1)) == 1


def test_parse_html_requires_selectors() -> None:
    broken = Source(name="Broken", url="https://example.org/", type="html")
    with pytest.raises(ParseError, match="missing required selectors"):
        parse_html(b"<html></html>", broken)


def test_parse_dispatches_on_type(
    feed_bytes: bytes,
    listing_bytes: bytes,
    rss_source: Source,
    html_source: Source,
) -> None:
    assert parse(feed_bytes, rss_source)
    assert parse(listing_bytes, html_source)

    unknown = Source(name="Odd", url="https://example.org/", type="rss")
    object.__setattr__(unknown, "type", "gopher")
    with pytest.raises(ParseError, match="unsupported source type"):
        parse(b"", unknown)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-03-04T09:00:00+00:00", datetime(2025, 3, 4, 9, 0, tzinfo=UTC)),
        ("2025-03-04T09:00:00Z", datetime(2025, 3, 4, 9, 0, tzinfo=UTC)),
        ("Tue, 04 Mar 2025 09:00:00 GMT", datetime(2025, 3, 4, 9, 0, tzinfo=UTC)),
        ("4 March 2025", datetime(2025, 3, 4, 0, 0, tzinfo=UTC)),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_datetime(raw: str | None, expected: datetime | None) -> None:
    assert parse_datetime(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2025-03-04T09:00:00", datetime(2025, 3, 4, 9, 0, tzinfo=UTC)),
        ("2025-03-04", datetime(2025, 3, 4, 0, 0, tzinfo=UTC)),
        ("2025-03-04T09:00:00+08:00", datetime(2025, 3, 4, 1, 0, tzinfo=UTC)),
    ],
)
def test_parse_datetime_treats_naive_iso_as_utc_regardless_of_host_tz(
    raw: str, expected: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A naive timestamp must not be read as the server's local time."""
    monkeypatch.setenv("TZ", "Australia/Perth")
    time.tzset()
    try:
        assert parse_datetime(raw) == expected
    finally:
        monkeypatch.undo()
        time.tzset()


def test_shipped_html_source_selectors_work_against_the_fixture(listing_bytes: bytes) -> None:
    """The selectors in the committed sources.yaml must parse markup of this shape.

    The live site cannot be reached from the test suite, so this pins the
    selector strings against a fixture built to mirror that page's structure.
    """
    from headliner.config import load_config

    config = load_config(Path(__file__).resolve().parents[1] / "sources.yaml")
    html_sources = [source for source in config.sources if source.type == "html"]
    assert html_sources, "sources.yaml should ship at least one html source"

    for source in html_sources:
        headlines = parse_html(listing_bytes, source)
        assert headlines, f"{source.name}: selectors matched nothing"
        assert all(headline.url.startswith("http") for headline in headlines)


SECTIONED_FEED = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Radio</title>
<item><title>Win a car in our big giveaway today</title><link>https://r.example/win/car</link></item>
<item><title>Storm warning issued for the west coast</title><link>https://r.example/news/storm</link></item>
<item><title>Breakfast show guest list announced</title><link>https://r.example/shows/guests</link></item>
<item><title>Rail fares to rise from next month</title><link>https://r.example/news/fares</link></item>
<item><title>Council approves new housing plan</title><link>https://r.example/news/housing</link></item>
</channel></rss>"""


def test_include_url_pattern_filters_before_the_limit() -> None:
    source = Source(
        name="Radio", url="https://r.example/feed/", type="rss", include_url_pattern="/news/"
    )
    titles = [h.title for h in parse(SECTIONED_FEED, source, limit=2)]
    # The limit counts matching items only: two news stories, not one.
    assert titles == [
        "Storm warning issued for the west coast",
        "Rail fares to rise from next month",
    ]


def test_find_feed_links_reads_alternate_links_only() -> None:
    page = b"""<html><head>
    <link rel="alternate" type="application/rss+xml" href="/feed/">
    <link rel="stylesheet alternate" type="text/css" href="/style.css">
    <link rel="alternate" type="application/atom+xml; charset=utf-8" href="https://cdn.example/atom">
    <link rel="alternate" type="application/rss+xml" href="/feed/">
    <link rel="alternate" hreflang="ga" href="/ga/">
    </head></html>"""
    assert find_feed_links(page, "https://site.example/news/") == [
        "https://site.example/feed/",
        "https://cdn.example/atom",
    ]
