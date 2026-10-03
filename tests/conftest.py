"""Shared fixtures. Nothing here touches the network."""

from __future__ import annotations

from pathlib import Path

import pytest
from headliner.config import Settings, Source

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def dns_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch` checks DNS first; tests mock HTTP only, so every host name resolves.

    Front-page reads also check each host resolves to a public address: every
    test host gets a documentation-range public address unless a test says not.
    """
    monkeypatch.setattr("headliner.network.resolves", lambda _host: True)
    monkeypatch.setattr("headliner.frontpage._addresses", lambda _host: ["93.184.215.14"])


@pytest.fixture
def feed_bytes() -> bytes:
    return (FIXTURES / "sample_feed.xml").read_bytes()


@pytest.fixture
def listing_bytes() -> bytes:
    return (FIXTURES / "sample_listing.html").read_bytes()


@pytest.fixture
def front_page_bytes() -> bytes:
    return (FIXTURES / "front_page.html").read_bytes()


@pytest.fixture
def rss_source() -> Source:
    return Source(name="Example Wire", url="https://example.org/feed.xml", type="rss")


@pytest.fixture
def html_source() -> Source:
    return Source(
        name="Example Text",
        url="https://text.example.org/",
        type="html",
        article_selector=".topic-container li",
        title_selector="a",
        link_selector="a",
        date_selector="time",
        summary_selector="p.teaser",
    )


@pytest.fixture
def settings() -> Settings:
    # No rate limiting in tests: it would only add wall-clock time.
    return Settings(
        request_timeout=5.0,
        rate_limit_seconds=0.0,
        user_agent="headliner-tests/0.1 (+contact: tests@example.org)",
        max_items_per_source=50,
        concurrency=4,
    )
