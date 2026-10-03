"""Front-page reading: extraction, HTTP outcomes, robots, safety and validation.

Every HTTP call is intercepted by respx; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from headliner import frontpage
from headliner.config import FrontPage, Settings, Source
from headliner.fetcher import RateLimiter, RobotsCache, build_client, fetch_all
from headliner.frontpage import (
    CacheEntry,
    Validation,
    check_url_safe,
    extract,
    fetch_front_page,
    follow_articles,
    is_challenge,
    is_nav_text,
    judge,
    looks_script_built,
    names_its_url,
    url_score,
)

PAGE = "https://news.example.com/"
ROBOTS = "https://news.example.com/robots.txt"
ALLOW_ALL = "User-agent: *\nAllow: /\n"


def page_source(url: str = PAGE, name: str = "Example Daily", **selectors: str | None) -> Source:
    return Source(name=name, url=url, type="front_page", front_page=FrontPage(url=url, **selectors))


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def read(source: Source, settings: Settings, **kwargs: Any) -> frontpage.FrontPageResult:
    async def go() -> frontpage.FrontPageResult:
        async with build_client(settings) as client:
            return await fetch_front_page(
                client,
                source,
                settings,
                limiter=RateLimiter(0),
                robots=kwargs.pop("robots", RobotsCache(client, settings.user_agent)),
                **kwargs,
            )

    return run(go())


# --------------------------------------------------------------------------
# Generic extraction (layers 2-5) on a realistic page
# --------------------------------------------------------------------------


@pytest.fixture
def extracted(front_page_bytes: bytes) -> frontpage.Extraction:
    return extract(front_page_bytes, page_source(), page_url=PAGE)


def test_generic_extraction_keeps_page_order(extracted: frontpage.Extraction) -> None:
    assert [h.title for h in extracted.headlines] == [
        "Harbour bridge reopens after week-long repair closure",
        "Storm hits coast, leaving thousands without power",
        "Why the budget surplus is smaller than it looks this year",
        "Dockers claim flag in a thriller at the MCG",
        "Fire in Perth suburb kills two",
        "Fire in Perth suburb kills three",
        "Live: election count updates from every electorate",
        "New species of frog found in the Kimberley",
        "Council approves new cycleway plan for city",
    ]
    assert [h.front_page_position for h in extracted.headlines] == list(range(1, 10))
    assert all(h.acquisition == ("front_page",) for h in extracted.headlines)


def test_navigation_and_chrome_are_not_headlines(extracted: frontpage.Extraction) -> None:
    urls = " ".join(h.url for h in extracted.headlines)
    titles = " ".join(h.title for h in extracted.headlines)
    # Inside <nav>, the site <header>, <footer>, a cookie banner, a share box.
    for chrome in (
        "menu-promo",
        "footer-link",
        "share-this-story",
        "cookie",
        "/subscribe",
        "/login",
        "/search",
        "/about-us",
        "/terms",
    ):
        assert chrome not in urls
    # Tag/topic indexes, section links, audio files, sign-ups, other sites, scripts.
    for junk in (
        "/topic/",
        "health-and-families",
        ".mp3",
        "newsletters",
        "twitter.com",
        "Load more",
    ):
        assert junk not in urls and junk not in titles
    assert "comments" not in titles


def test_relative_protocol_relative_and_tracking_urls_are_normalised(
    extracted: frontpage.Extraction,
) -> None:
    lead = extracted.headlines[0]
    # Relative href, tracking parameters dropped.
    assert lead.url == "https://news.example.com/news/2026/10/04/harbour-bridge-reopens"
    # Protocol-relative image source.
    assert lead.image_url == "https://img.example.com/bridge.jpg"


def test_duplicate_links_and_amp_variants_collapse_to_the_first_position(
    extracted: frontpage.Extraction,
) -> None:
    storm = [h for h in extracted.headlines if h.title.startswith("Storm")]
    bridge = [h for h in extracted.headlines if h.title.startswith("Harbour")]
    assert len(storm) == 1 and storm[0].front_page_position == 2
    assert len(bridge) == 1 and bridge[0].front_page_position == 1
    assert extracted.duplicates >= 3


def test_similar_but_distinct_headlines_are_both_kept(extracted: frontpage.Extraction) -> None:
    fires = [h.title for h in extracted.headlines if h.title.startswith("Fire in Perth")]
    assert fires == ["Fire in Perth suburb kills two", "Fire in Perth suburb kills three"]


def test_live_blog_update_links_are_one_story(extracted: frontpage.Extraction) -> None:
    live = [h for h in extracted.headlines if "election-count" in h.url]
    assert len(live) == 1
    assert live[0].is_live


def test_overlay_links_kickers_labels_and_ranks_are_handled(
    extracted: frontpage.Extraction,
) -> None:
    titles = [h.title for h in extracted.headlines]
    # Empty overlay link: the card's title element names it.
    assert "Why the budget surplus is smaller than it looks this year" in titles
    # "Sport", "Premium" labels and the teaser are not part of the headline.
    assert "Dockers claim flag in a thriller at the MCG" in titles
    # The "1" rank in a most-read list is not either.
    assert "Council approves new cycleway plan for city" in titles


def test_json_ld_and_microdata_add_dates_and_sections(extracted: frontpage.Extraction) -> None:
    by_title = {h.title: h for h in extracted.headlines}
    bridge = by_title["Harbour bridge reopens after week-long repair closure"]
    assert bridge.published_at == datetime(2026, 10, 3, 22, 30, tzinfo=UTC)
    assert bridge.section == "news"
    frog = by_title["New species of frog found in the Kimberley"]
    assert frog.section == "science"
    assert frog.published_at == datetime(2026, 10, 3, 22, 0, tzinfo=UTC)


def test_page_canonical_is_recorded(extracted: frontpage.Extraction) -> None:
    assert extracted.canonical_url == PAGE
    assert not extracted.rendering_required


def test_json_ld_alone_yields_headlines_in_list_order() -> None:
    html = """<html><head><script type="application/ld+json">
    {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": [
      {"@type": "ListItem", "position": 1, "item": {"@type": "NewsArticle",
        "headline": "First story from the structured data list",
        "url": "https://news.example.com/2026/10/04/first-story", "articleSection": "Politics"}},
      {"@type": "ListItem", "position": 2, "item": {"@type": "OpinionNewsArticle",
        "headline": "Second story from the structured data list",
        "mainEntityOfPage": {"@id": "/2026/10/04/second-story"}}}
    ]}</script></head><body><div id="root"></div></body></html>"""
    result = extract(html, page_source(), page_url=PAGE)
    assert [(h.title, h.url, h.section) for h in result.headlines] == [
        (
            "First story from the structured data list",
            "https://news.example.com/2026/10/04/first-story",
            "politics",
        ),
        (
            "Second story from the structured data list",
            "https://news.example.com/2026/10/04/second-story",
            "opinion",
        ),
    ]
    assert result.layers == {"json-ld": 2}


def test_base_href_resolves_relative_links() -> None:
    html = """<html><head><base href="https://cdn.news.example.com/en/"></head><body>
    <article><h2><a href="world/2026/10/04/talks-resume-after-ceasefire">Talks resume after
    ceasefire holds overnight</a></h2></article></body></html>"""
    result = extract(html, page_source(), page_url=PAGE)
    assert result.headlines[0].url == (
        "https://cdn.news.example.com/en/world/2026/10/04/talks-resume-after-ceasefire"
    )


def test_the_limit_keeps_the_top_of_the_page(front_page_bytes: bytes) -> None:
    result = extract(front_page_bytes, page_source(), page_url=PAGE, limit=3)
    assert [h.front_page_position for h in result.headlines] == [1, 2, 3]


def test_include_url_pattern_applies_to_front_page_links(front_page_bytes: bytes) -> None:
    source = Source(
        name="Example Daily",
        url=PAGE,
        type="front_page",
        include_url_pattern="/world/",
        front_page=FrontPage(url=PAGE),
    )
    result = extract(front_page_bytes, source, page_url=PAGE)
    assert [h.title for h in result.headlines] == [
        "Storm hits coast, leaving thousands without power"
    ]
    assert result.headlines[0].front_page_position == 1


# --------------------------------------------------------------------------
# Configured selectors (layer 1)
# --------------------------------------------------------------------------

SELECTOR_PAGE = """<html><body>
<nav><a href="/news/2026/10/04/nav-link-that-looks-like-a-story">Nav link like a story</a>
</nav>
<div class="river">
  <div class="item"><span class="sec">Business</span>
    <a class="hl" href="/a/1001">Mining company posts record profit</a>
    <span class="when" data-ts="2026-10-04T01:00:00Z">1h</span>
    <img data-src="/img/1001.jpg"></div>
  <div class="item"><span class="sec">Lifestyle</span>
    <a class="hl" href="/a/1002">Ten weekend recipes for spring picnics</a></div>
  <div class="item"><span class="sec">World</span><a class="hl" href="/a/1001">Mining company
    posts record profit</a></div>
</div></body></html>"""


def test_configured_selectors_are_used_first() -> None:
    source = page_source(
        article_selector=".river .item",
        title_selector="a.hl",
        link_selector="a.hl",
        section_selector=".sec",
        published_selector=".when",
        image_selector="img",
    )
    result = extract(SELECTOR_PAGE, source, page_url=PAGE)
    assert [(h.title, h.url, h.section) for h in result.headlines] == [
        ("Mining company posts record profit", "https://news.example.com/a/1001", "business"),
        ("Ten weekend recipes for spring picnics", "https://news.example.com/a/1002", "other"),
    ]
    assert result.headlines[0].image_url == "https://news.example.com/img/1001.jpg"
    assert result.layers == {"selectors": 2}
    assert result.duplicates == 1
    assert result.note is None


def test_selectors_that_match_nothing_fall_back_to_generic_extraction(
    front_page_bytes: bytes,
) -> None:
    source = page_source(article_selector="div.no-such-thing")
    result = extract(front_page_bytes, source, page_url=PAGE)
    assert result.headlines
    assert result.note == "configured selectors matched nothing; used generic extraction"
    assert "selectors" not in result.layers


# --------------------------------------------------------------------------
# Heuristics, JavaScript-built pages and bot challenges
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "article"),
    [
        ("https://x.example/news/2026/10/04/some-story-here", True),
        ("https://x.example/world/europe/c8zxl62yzxzxo", True),
        ("https://x.example/a/12345678", True),
        ("https://x.example/news/storm-hits-the-coast-today.html", True),
        ("https://x.example/category/national/a-long-enough-story-slug-for-an-article", True),
        ("https://x.example/", False),
        ("https://x.example/sport", False),
        ("https://x.example/news/world", False),
        ("https://x.example/topic/climate-change-and-the-reef", False),
        ("https://x.example/news/topics/c2vdnvdg6xxt", False),
        ("https://x.example/category/features/when-they-opened", False),
        ("https://x.example/author/jane-citizen-reporter", False),
        ("https://x.example/audio/2026/10/04/bulletin.mp3", False),
        ("https://x.example/bbcnewsignup2", False),
    ],
)
def test_url_score_separates_articles_from_index_pages(url: str, article: bool) -> None:
    assert (url_score(url) > 0) is article


@pytest.mark.parametrize(
    "text",
    [
        "Home",
        "SUBSCRIBE",
        "Log in",
        "Read more",
        "View all",
        "12 comments",
        "Listen · 3:34",
        "Sign up for our newsletter",
        "Privacy policy",
        "Weather",
        "Sport",
    ],
)
def test_navigation_words_are_not_headlines(text: str) -> None:
    assert is_nav_text(text)


def test_a_headline_mentioning_sport_is_not_navigation() -> None:
    assert not is_nav_text("Sport minister resigns over grants scandal")


def test_section_links_that_spell_out_their_url() -> None:
    assert names_its_url("Health & Families", "https://x.example/life-style/health-and-families")
    assert not names_its_url(
        "Artist puts heart into her work for charity",
        "https://x.example/category/features/artist-puts-heart-into-her-work-for-charity",
    )


def test_static_html_is_not_flagged_for_rendering(front_page_bytes: bytes) -> None:
    assert not looks_script_built(front_page_bytes.decode())


def test_script_shell_is_detected_rather_than_silently_empty() -> None:
    shell = (
        "<html><head><script src='/a.js'></script><script src='/b.js'></script>"
        '<script>window.__STATE__={}</script></head><body><div id="root"></div>'
        "<noscript>You need to enable JavaScript to run this app.</noscript></body></html>"
    )
    result = extract(shell, page_source(), page_url=PAGE)
    assert result.headlines == []
    assert result.rendering_required


def test_bot_challenge_pages_are_recognised() -> None:
    assert is_challenge("<html><head><title>Just a moment...</title></head></html>")
    assert is_challenge('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>')
    assert not is_challenge("<html><head><title>Example Daily</title></head></html>")


# --------------------------------------------------------------------------
# HTTP outcomes
# --------------------------------------------------------------------------


@pytest.fixture
def allow_robots() -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text=ALLOW_ALL))


@respx.mock
def test_200_is_healthy_and_records_timing_and_validators(
    allow_robots: None, settings: Settings, front_page_bytes: bytes
) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            content=front_page_bytes,
            headers={"content-type": "text/html; charset=utf-8", "etag": '"v1"'},
        )
    )
    result = read(page_source(), settings)
    assert result.status == "healthy" and result.ok
    assert result.http_status == 200
    assert result.final_url == PAGE
    assert result.response_ms is not None and result.response_ms >= 0
    assert result.etag == '"v1"'
    assert len(result.headlines) == 9
    assert result.method and "article" in result.method


@respx.mock
@pytest.mark.parametrize("code", [301, 302])
def test_redirects_are_followed_and_the_final_url_recorded(
    allow_robots: None, settings: Settings, front_page_bytes: bytes, code: int
) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(code, headers={"location": "https://news.example.com/au/"})
    )
    respx.get("https://news.example.com/au/").mock(
        return_value=httpx.Response(200, content=front_page_bytes)
    )
    result = read(page_source(), settings)
    assert result.status == "healthy"
    assert result.final_url == "https://news.example.com/au/"


@respx.mock
def test_304_uses_the_previous_validators(allow_robots: None, settings: Settings) -> None:
    route = respx.get(PAGE).mock(return_value=httpx.Response(304))
    cache = CacheEntry(PAGE, etag='"v1"', last_modified="Sat, 03 Oct 2026 10:00:00 GMT")
    result = read(page_source(), settings, cache=cache)
    assert result.status == "not_modified" and result.ok
    assert result.http_status == 304
    assert result.headlines == []
    sent = route.calls.last.request.headers
    assert sent["if-none-match"] == '"v1"'
    assert sent["if-modified-since"] == "Sat, 03 Oct 2026 10:00:00 GMT"
    assert result.etag == '"v1"'


@respx.mock
@pytest.mark.parametrize(
    ("code", "status", "error"),
    [
        (403, "blocked", "HTTP 403"),
        (429, "blocked", "HTTP 429"),
        (404, "http_error", "HTTP 404"),
        (410, "http_error", "HTTP 410"),
    ],
)
def test_http_failures_become_structured_states(
    allow_robots: None, settings: Settings, code: int, status: str, error: str
) -> None:
    route = respx.get(PAGE).mock(return_value=httpx.Response(code))
    result = read(page_source(), settings)
    assert (result.status, result.error, result.http_status) == (status, error, code)
    # Never retried: a block or a missing page will not change in seconds.
    assert route.call_count == 1


@respx.mock
def test_500_is_retried_once_then_reported(allow_robots: None, settings: Settings) -> None:
    route = respx.get(PAGE).mock(return_value=httpx.Response(500))
    result = read(page_source(), settings)
    assert result.status == "http_error" and result.error == "HTTP 500"
    assert route.call_count == 2


@respx.mock
def test_cloudflare_challenge_is_blocked_not_bypassed(
    allow_robots: None, settings: Settings
) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            503,
            text="<html><head><title>Just a moment...</title></head></html>",
            headers={"cf-mitigated": "challenge", "server": "cloudflare"},
        )
    )
    result = read(page_source(), settings)
    assert result.status == "blocked"
    assert "challenge" in (result.error or "")


@respx.mock
def test_timeout_is_reported(allow_robots: None, settings: Settings) -> None:
    respx.get(PAGE).mock(side_effect=httpx.ReadTimeout("slow"))
    result = read(page_source(), settings)
    assert result.status == "timeout"


@respx.mock
def test_connection_failure_is_a_network_error(allow_robots: None, settings: Settings) -> None:
    respx.get(PAGE).mock(side_effect=httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"))
    result = read(page_source(), settings)
    assert result.status == "network_error"
    assert (result.error or "").startswith("TLS error")


@respx.mock
def test_non_html_and_empty_pages(allow_robots: None, settings: Settings) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200, json={"a": 1}, headers={"content-type": "application/json"}
        )
    )
    assert read(page_source(), settings).status == "parse_error"

    respx.get(PAGE).mock(
        return_value=httpx.Response(200, html="<html><body><p>Nothing to see.</p></body></html>")
    )
    assert read(page_source(), settings).status == "no_headlines"


@respx.mock
def test_script_built_page_is_reported_as_rendering_required(
    allow_robots: None, settings: Settings
) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(
            200,
            html='<html><body><div id="app"></div><script src="/a.js"></script>'
            '<script src="/b.js"></script><script src="/c.js"></script></body></html>',
        )
    )
    result = read(page_source(), settings)
    assert result.status == "rendering_required"
    assert result.rendering_required


@respx.mock
def test_oversized_page_is_abandoned(
    allow_robots: None, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(frontpage, "MAX_BODY_BYTES", 100)
    respx.get(PAGE).mock(return_value=httpx.Response(200, content=b"x" * 500))
    assert read(page_source(), settings).status == "parse_error"


# --------------------------------------------------------------------------
# robots.txt and request safety
# --------------------------------------------------------------------------


@respx.mock
def test_robots_allowed_page_is_read(settings: Settings, front_page_bytes: bytes) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /x\n"))
    respx.get(PAGE).mock(return_value=httpx.Response(200, content=front_page_bytes))
    assert read(page_source(), settings).status == "healthy"


@respx.mock
def test_robots_disallowed_page_is_never_requested(settings: Settings) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n"))
    page = respx.get(PAGE).mock(return_value=httpx.Response(200))
    result = read(page_source(), settings)
    assert result.status == "robots_denied"
    assert not page.called


@respx.mock
def test_robots_of_a_redirect_target_is_obeyed(allow_robots: None, settings: Settings) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(302, headers={"location": "https://edition.example.org/"})
    )
    respx.get("https://edition.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /\n")
    )
    target = respx.get("https://edition.example.org/").mock(return_value=httpx.Response(200))
    result = read(page_source(), settings)
    assert result.status == "robots_denied"
    assert not target.called


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://10.1.2.3/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.66.115.121:8000/",
        "file:///etc/passwd",
        "ftp://news.example.com/",
    ],
)
def test_private_and_non_http_urls_are_refused(url: str) -> None:
    with pytest.raises(frontpage.UnsafeURLError):
        run(check_url_safe(url))


def test_a_host_resolving_to_a_private_address_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(frontpage, "_addresses", lambda _host: ["192.168.0.78"])
    with pytest.raises(frontpage.UnsafeURLError, match="non-public"):
        run(check_url_safe("https://intranet.example.com/"))


@respx.mock
def test_a_redirect_into_the_local_network_is_refused(
    allow_robots: None, settings: Settings
) -> None:
    respx.get(PAGE).mock(
        return_value=httpx.Response(302, headers={"location": "http://192.168.0.1/"})
    )
    local = respx.get("http://192.168.0.1/").mock(return_value=httpx.Response(200))
    result = read(page_source(), settings)
    assert result.status == "unsafe_url"
    assert not local.called


# --------------------------------------------------------------------------
# Failure isolation and the feed
# --------------------------------------------------------------------------

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Feed story number one for the test</title>
<link>https://{host}/news/2026/10/04/feed-story-one</link></item>
</channel></rss>"""


def two_path_source(host: str) -> Source:
    return Source(
        name=host,
        url=f"https://{host}/feed.xml",
        type="rss",
        front_page=FrontPage(url=f"https://{host}/"),
    )


@respx.mock
def test_one_bad_front_page_never_fails_the_feed_or_the_run(
    settings: Settings, front_page_bytes: bytes
) -> None:
    hosts = ["a.example.com", "b.example.com", "c.example.com", "d.example.com"]
    for host in hosts:
        respx.get(f"https://{host}/robots.txt").mock(return_value=httpx.Response(404))
        respx.get(f"https://{host}/feed.xml").mock(
            return_value=httpx.Response(200, text=FEED.format(host=host))
        )
    respx.get("https://a.example.com/").mock(
        return_value=httpx.Response(200, content=front_page_bytes)
    )
    respx.get("https://b.example.com/").mock(return_value=httpx.Response(403))
    respx.get("https://c.example.com/").mock(side_effect=httpx.ConnectTimeout("slow"))
    respx.get("https://d.example.com/").mock(side_effect=RuntimeError("parser exploded"))

    results = run(fetch_all([two_path_source(host) for host in hosts], settings))

    assert [r.status for r in results] == ["ok", "ok", "ok", "ok"]
    assert all(r.items_found == 1 for r in results)
    pages = [r.front_page for r in results]
    assert [p.status if p else None for p in pages] == [
        "healthy",
        "blocked",
        "timeout",
        "network_error",
    ]


@respx.mock
def test_front_pages_can_be_switched_off(settings: Settings) -> None:
    respx.get("https://a.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://a.example.com/feed.xml").mock(
        return_value=httpx.Response(200, text=FEED.format(host="a.example.com"))
    )
    page = respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    results = run(fetch_all([two_path_source("a.example.com")], settings, front_pages=False))
    assert results[0].status == "ok" and results[0].front_page is None
    assert not page.called


@respx.mock
def test_feeds_can_be_switched_off(settings: Settings, front_page_bytes: bytes) -> None:
    respx.get("https://a.example.com/robots.txt").mock(return_value=httpx.Response(404))
    feed = respx.get("https://a.example.com/feed.xml").mock(return_value=httpx.Response(200))
    respx.get("https://a.example.com/").mock(
        return_value=httpx.Response(200, content=front_page_bytes)
    )
    results = run(fetch_all([two_path_source("a.example.com")], settings, feeds=False))
    assert not feed.called
    assert results[0].feed_ran is False
    assert results[0].front_page is not None and results[0].front_page.ok


@respx.mock
def test_a_front_page_only_source_reports_through_its_page(
    settings: Settings, front_page_bytes: bytes
) -> None:
    respx.get(ROBOTS).mock(return_value=httpx.Response(404))
    respx.get(PAGE).mock(return_value=httpx.Response(200, content=front_page_bytes))
    ok = run(fetch_all([page_source()], settings))[0]
    assert (ok.status, ok.items_found, ok.headlines) == ("ok", 9, [])
    assert ok.front_page is not None and len(ok.front_page.headlines) == 9

    respx.get(PAGE).mock(return_value=httpx.Response(403))
    failed = run(fetch_all([page_source()], settings))[0]
    assert failed.status == "error"
    assert failed.error == "front page blocked: HTTP 403"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

ARTICLE = '<html><head><link rel="canonical" href="{url}"></head><body><h1>Story</h1></body></html>'


@respx.mock
def test_validation_follows_articles_and_passes_a_good_page(
    allow_robots: None, settings: Settings, front_page_bytes: bytes
) -> None:
    respx.get(PAGE).mock(return_value=httpx.Response(200, content=front_page_bytes))
    respx.get(url__startswith="https://news.example.com/").mock(
        side_effect=lambda request: httpx.Response(200, html=ARTICLE.format(url=request.url))
    )

    async def go() -> Validation:
        async with build_client(settings) as client:
            limiter, robots = RateLimiter(0), RobotsCache(client, settings.user_agent)
            result = await fetch_front_page(
                client, page_source(), settings, limiter=limiter, robots=robots
            )
            validation = Validation(result)
            await follow_articles(
                client,
                result.headlines,
                settings,
                limiter=limiter,
                robots=robots,
                count=2,
                validation=validation,
            )
            judge(validation, follow=2)
            return validation

    validation = run(go())
    assert validation.valid, validation.reasons
    report = validation.as_dict()
    assert report["unique_headline_count"] == 9
    assert report["headline_count"] >= 12
    assert report["valid_article_url_count"] == 9
    assert report["accessible_article_count"] == 2
    assert report["canonical_matches"] == 2
    assert 0 < report["duplicate_rate"] < 0.5
    assert report["mean_headline_length"] > 20
    assert report["front_page_response_time"] is not None


def test_validation_rejects_thin_or_failed_pages(settings: Settings) -> None:
    now = datetime(2026, 10, 4, tzinfo=UTC)
    thin = frontpage.FrontPageResult(
        source="x", url=PAGE, status="healthy", started_at=now, finished_at=now, found=2
    )
    validation = Validation(thin)
    judge(validation, follow=0)
    assert not validation.valid
    assert validation.reasons == ["only 0 headline(s); want at least 5"]

    blocked = frontpage.FrontPageResult(
        source="x", url=PAGE, status="blocked", started_at=now, finished_at=now, error="HTTP 403"
    )
    validation = Validation(blocked)
    judge(validation, follow=2)
    assert validation.reasons == ["blocked: HTTP 403"]


def test_health_states() -> None:
    now = datetime(2026, 10, 4, 12, tzinfo=UTC)
    page = FrontPage(url=PAGE)
    assert frontpage.health(None, None, None, now) is None
    assert frontpage.health(FrontPage(url=PAGE, enabled=False), None, None, now) == "disabled"
    assert frontpage.health(page, None, None, now) == "never read"
    assert frontpage.health(page, "healthy", now, now) == "healthy"
    assert frontpage.health(page, "not_modified", now, now) == "healthy"
    assert frontpage.health(page, "healthy", datetime(2026, 10, 3, tzinfo=UTC), now) == "stale"
    assert frontpage.health(page, "blocked", None, now) == "blocked"
