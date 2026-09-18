"""Normalisation, hashing and `Headline` construction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from headliner.models import (
    Headline,
    InvalidHeadlineError,
    clean_text,
    compute_hash,
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
