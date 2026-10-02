"""Normalisation, hashing and `Headline` construction."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from headliner.models import (
    Headline,
    InvalidHeadlineError,
    clean_text,
    compute_hash,
    looks_live,
    normalise_url,
    to_utc,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Hello   world  ", "Hello world"),
        ("Line\nbreak\tand\ttabs", "Line break and tabs"),
        ("Café Royale", "Café Royale"),
        ("&amp;quot;Quoted&amp;quot;", '"Quoted"'),
        ("<b>Bold</b> headline", "Bold headline"),
        ("zero​width", "zerowidth"),
        (None, ""),
        ("", ""),
    ],
)
def test_clean_text(raw: str | None, expected: str) -> None:
    assert clean_text(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.ORG/News/", "https://example.org/News"),
        ("https://example.org:443/news", "https://example.org/news"),
        ("https://example.org/news#section", "https://example.org/news"),
        (
            "https://example.org/news?utm_source=rss&utm_medium=feed&id=7",
            "https://example.org/news?id=7",
        ),
        ("https://example.org/", "https://example.org/"),
        ("https://example.org/a?b=2&a=1", "https://example.org/a?a=1&b=2"),
    ],
)
def test_normalise_url(raw: str, expected: str) -> None:
    assert normalise_url(raw) == expected


def test_hash_is_stable_across_tracking_params_and_case() -> None:
    a = compute_hash("https://example.org/story?utm_source=rss", "Big News Today")
    b = compute_hash("https://EXAMPLE.org/story/", "big news today")
    assert a == b


def test_hash_differs_for_different_titles() -> None:
    a = compute_hash("https://example.org/story", "Big News Today")
    b = compute_hash("https://example.org/story", "Different News Today")
    assert a != b


def test_to_utc_converts_and_assumes_utc_for_naive() -> None:
    aware = datetime(2025, 3, 4, 9, 0, tzinfo=timezone(timedelta(hours=11)))
    assert to_utc(aware) == datetime(2025, 3, 3, 22, 0, tzinfo=UTC)
    naive = datetime(2025, 3, 4, 9, 0)
    assert to_utc(naive) == datetime(2025, 3, 4, 9, 0, tzinfo=UTC)
    assert to_utc(None) is None


def test_create_normalises_every_field() -> None:
    headline = Headline.create(
        source="Example Wire",
        title="  A   perfectly  fine &amp;amp; usable headline ",
        url="https://EXAMPLE.org/story/?utm_campaign=x#frag",
        published_at=datetime(2025, 3, 4, 9, 0),
        summary="<p>Some   summary</p>",
    )
    assert headline.title == "A perfectly fine & usable headline"
    assert headline.url == "https://example.org/story"
    assert headline.summary == "Some summary"
    assert headline.published_at == datetime(2025, 3, 4, 9, 0, tzinfo=UTC)
    assert headline.fetched_at.tzinfo is not None
    assert len(headline.content_hash) == 64


def test_create_defaults_summary_to_none_when_blank() -> None:
    headline = Headline.create(
        source="Example Wire",
        title="A title long enough to survive",
        url="https://example.org/x",
        summary="   ",
    )
    assert headline.summary is None


@pytest.mark.parametrize(
    ("title", "url"),
    [
        ("Too short", "https://example.org/a"),
        ("", "https://example.org/a"),
        ("A title long enough to survive", ""),
        ("A title long enough to survive", "ftp://example.org/a"),
    ],
)
def test_create_rejects_unusable_rows(title: str, url: str) -> None:
    with pytest.raises(InvalidHeadlineError):
        Headline.create(source="Example Wire", title=title, url=url)


def test_as_dict_is_json_ready() -> None:
    headline = Headline.create(
        source="Example Wire",
        title="A title long enough to survive",
        url="https://example.org/x",
        published_at=datetime(2025, 3, 4, 9, 0, tzinfo=UTC),
    )
    payload = headline.as_dict()
    assert payload["published_at"] == "2025-03-04T09:00:00+00:00"
    assert payload["summary"] is None


# Real examples from the shipped sources.
@pytest.mark.parametrize(
    ("url", "title"),
    [
        (
            "https://www.theguardian.com/australia-news/live/2026/oct/02/labor-albanese",
            "Australia news live: Israeli embassy condemns flydubai claims",
        ),
        (
            "https://www.theguardian.com/world/live/2026/oct/02/france-schools-protests",
            "400 French schools closed on Friday as protests escalate",
        ),
        (
            "https://www.aljazeera.com/news/liveblog/2026/10/2/iran-war-live-us-moves",
            "Iran war live: US moves 2,000 Marines to Middle East",
        ),
        (
            "https://www.france24.com/en/europe/20261002-live-russia-hits-kyiv",
            "Live: Russia hits Kyiv's Southern Bridge again",
        ),
        ("https://www.bbc.co.uk/news/live/c1234567", "Storm Amy: Latest as wind warnings issued"),
        ("https://example.org/story", "Election live updates: polls close in the west"),
    ],
)
def test_looks_live_recognises_live_blogs(url: str, title: str) -> None:
    assert looks_live(url, title)


@pytest.mark.parametrize(
    ("url", "title"),
    [
        (
            "https://www.abc.net.au/news/2026-10-02/how-to-protect-your-garden-and-live-in-harmony",
            "How to protect your garden and live in harmony with possums",
        ),
        ("https://example.org/delivery/2026/oct/02/story", "Courier firm to deliver on Sundays"),
        ("https://example.org/story", "Band to play live at the festival tonight"),
        ("https://example.org/lives/2026/obituary", "A life lived for music"),
    ],
)
def test_looks_live_ignores_ordinary_stories(url: str, title: str) -> None:
    assert not looks_live(url, title)


def test_looks_live_uses_a_source_pattern() -> None:
    pattern = re.compile(r"/as-it-happened/", re.IGNORECASE)
    url = "https://example.org/as-it-happened/budget-night"
    assert not looks_live(url, "Budget night: every announcement")
    assert looks_live(url, "Budget night: every announcement", pattern)


def test_headline_create_sets_is_live() -> None:
    live = Headline.create(
        source="X", title="Iran war live: latest updates", url="https://example.org/a"
    )
    plain = Headline.create(source="X", title="An ordinary headline here", url="https://e.org/b")
    assert live.is_live and live.as_dict()["is_live"] is True
    assert not plain.is_live


def test_normalise_url_strips_bbc_at_internet_params() -> None:
    raw = "https://www.bbc.co.uk/news/articles/c5kg0gwwpyx8o?at_campaign=rss&at_medium=RSS"
    assert normalise_url(raw) == "https://www.bbc.co.uk/news/articles/c5kg0gwwpyx8o"
    assert normalise_url("https://e.org/a?id=3&utm_anything=x&AT_Link_ID=9") == (
        "https://e.org/a?id=3"
    )


def test_normalise_url_keeps_lookalike_params() -> None:
    assert normalise_url("https://e.org/a?attribute=1&at=2") == "https://e.org/a?at=2&attribute=1"


def test_bbc_campaign_change_no_longer_changes_the_hash() -> None:
    base = "https://www.bbc.co.uk/news/articles/c5kg0gwwpyx8o"
    title = "A headline long enough to keep"
    assert compute_hash(f"{base}?at_campaign=rss&at_medium=RSS", title) == compute_hash(
        f"{base}?at_campaign=newsletter", title
    )
