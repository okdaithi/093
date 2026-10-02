"""Fetching behaviour: robots, retries, rate limiting and failure isolation.

Every HTTP call is intercepted by respx; nothing here touches the network.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import respx
from headliner.config import Settings, Source
from headliner.fetcher import (
    FetchError,
    RateLimiter,
    RobotsCache,
    _backoff_delay,
    _retry_after_seconds,
    build_client,
    fetch_all,
    fetch_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
FEED_URL = "https://feed.example.org/rss.xml"
ROBOTS_URL = "https://feed.example.org/robots.txt"

ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_FEED = "User-agent: *\nDisallow: /rss.xml\n"


def feed_source(name: str = "Example Wire", url: str = FEED_URL) -> Source:
    return Source(name=name, url=url, type="rss")


def run(coro: object) -> object:
    return asyncio.run(coro)  # type: ignore[arg-type]


@pytest.fixture
def feed_body() -> bytes:
    return (FIXTURES / "sample_feed.xml").read_bytes()


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


@respx.mock
def test_disallowed_path_is_skipped(settings: Settings, feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=DISALLOW_FEED))
    feed_route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    results = run(fetch_all([feed_source()], settings))

    assert len(results) == 1
    result = results[0]
    assert result.status == "skipped"
    assert result.headlines == []
    assert result.error is not None
    assert "robots.txt disallows" in result.error
    # The feed itself was never requested.
    assert not feed_route.called
    # A skip is not a failure.
    assert result.ok


@respx.mock
def test_allowed_path_is_fetched(settings: Settings, feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    results = run(fetch_all([feed_source()], settings))

    assert results[0].status == "ok"
    assert results[0].items_found == 5


@respx.mock
def test_ignore_robots_skips_the_robots_request(settings: Settings, feed_body: bytes) -> None:
    robots_route = respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=DISALLOW_FEED))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    results = run(fetch_all([feed_source()], settings, ignore_robots=True))

    assert results[0].status == "ok"
    assert not robots_route.called


@respx.mock
def test_missing_robots_txt_allows_fetching(settings: Settings, feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(404))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    assert run(fetch_all([feed_source()], settings))[0].status == "ok"


@respx.mock
def test_unreachable_robots_txt_allows_fetching(settings: Settings, feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(side_effect=httpx.ConnectError("boom"))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    assert run(fetch_all([feed_source()], settings))[0].status == "ok"


@respx.mock
def test_robots_txt_is_fetched_once_per_domain(settings: Settings, feed_body: bytes) -> None:
    robots_route = respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))
    other = "https://feed.example.org/other.xml"
    respx.get(other).mock(return_value=httpx.Response(200, content=feed_body))

    results = run(fetch_all([feed_source(), feed_source(name="Second", url=other)], settings))

    assert all(result.status == "ok" for result in results)
    assert robots_route.call_count == 1


@respx.mock
def test_robots_rules_are_agent_specific(settings: Settings, feed_body: bytes) -> None:
    rules = "User-agent: headliner-tests\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=rules))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    results = run(fetch_all([feed_source()], settings))
    assert results[0].status == "skipped"


@respx.mock
def test_robots_cache_reports_crawl_delay(settings: Settings) -> None:
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 7\nAllow: /\n")
    )

    async def check() -> float | None:
        async with build_client(settings) as client:
            return await RobotsCache(client, settings.user_agent).crawl_delay(FEED_URL)

    assert run(check()) == 7.0


# --------------------------------------------------------------------------
# retries
# --------------------------------------------------------------------------


@respx.mock
def test_retries_on_server_error_then_succeeds(settings: Settings, feed_body: bytes) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(500),
            httpx.Response(200, content=feed_body),
        ]
    )

    async def check() -> httpx.Response:
        async with build_client(settings) as client:
            return await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    response = run(check())
    assert response.status_code == 200
    assert route.call_count == 3


@respx.mock
def test_gives_up_after_three_attempts(settings: Settings) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(503))

    async def check() -> None:
        async with build_client(settings) as client:
            await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    with pytest.raises(FetchError, match="giving up after 3 attempts"):
        run(check())
    assert route.call_count == 3


@respx.mock
def test_retries_on_timeout(settings: Settings, feed_body: bytes) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200, content=feed_body)]
    )

    async def check() -> httpx.Response:
        async with build_client(settings) as client:
            return await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    assert run(check()).status_code == 200
    assert route.call_count == 2


@respx.mock
def test_does_not_retry_plain_client_errors(settings: Settings) -> None:
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(404))

    async def check() -> None:
        async with build_client(settings) as client:
            await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    with pytest.raises(FetchError, match="HTTP 404"):
        run(check())
    assert route.call_count == 1


@respx.mock
def test_retries_on_429(settings: Settings, feed_body: bytes) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, content=feed_body),
        ]
    )

    async def check() -> httpx.Response:
        async with build_client(settings) as client:
            return await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    assert run(check()).status_code == 200
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize("status", [429, 503])
def test_long_retry_after_gives_up_without_retrying(settings: Settings, status: int) -> None:
    route = respx.get(FEED_URL).mock(
        return_value=httpx.Response(status, headers={"Retry-After": "300"})
    )

    async def check() -> None:
        async with build_client(settings) as client:
            await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    with pytest.raises(FetchError, match=r"Retry-After 300s .*not retrying this run"):
        run(check())
    assert route.call_count == 1


@respx.mock
def test_retry_after_at_the_cap_is_still_honoured(settings: Settings, feed_body: bytes) -> None:
    route = respx.get(FEED_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "30"}),
            httpx.Response(200, content=feed_body),
        ]
    )

    async def check() -> httpx.Response:
        async with build_client(settings) as client:
            return await fetch_url(client, FEED_URL, timeout=5.0, sleep=False)

    assert run(check()).status_code == 200
    assert route.call_count == 2


def test_retry_after_seconds_reads_both_forms() -> None:
    delay = _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "12"}))
    assert delay == 12.0

    http_date = _retry_after_seconds(
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
    )
    # The date is in the past, so the wait clamps to zero rather than going negative.
    assert http_date == 0.0

    assert _retry_after_seconds(httpx.Response(429)) is None
    assert _retry_after_seconds(httpx.Response(429, headers={"Retry-After": "soon"})) is None


def test_backoff_prefers_retry_after_and_stays_bounded() -> None:
    assert _backoff_delay(1, 5.0) == 5.0
    assert _backoff_delay(1, 10_000.0) == 30.0
    for attempt in range(1, 6):
        assert 0.0 <= _backoff_delay(attempt, None) <= 30.0


# --------------------------------------------------------------------------
# rate limiting and concurrency
# --------------------------------------------------------------------------


def test_rate_limiter_spaces_requests_to_one_domain() -> None:
    async def check() -> float:
        limiter = RateLimiter(0.05)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await limiter.acquire("a.example")
        await limiter.acquire("a.example")
        await limiter.acquire("a.example")
        return loop.time() - start

    assert run(check()) >= 0.1


def test_rate_limiter_does_not_couple_separate_domains() -> None:
    async def check() -> float:
        limiter = RateLimiter(0.2)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await asyncio.gather(limiter.acquire("a.example"), limiter.acquire("b.example"))
        return loop.time() - start

    assert run(check()) < 0.2


def test_rate_limiter_min_gap_widens_the_delay() -> None:
    async def check() -> float:
        limiter = RateLimiter(0.0)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await limiter.acquire("a.example", min_gap=0.1)
        await limiter.acquire("a.example", min_gap=0.1)
        return loop.time() - start

    assert run(check()) >= 0.1


def test_rate_limiter_min_gap_never_shortens_the_delay() -> None:
    async def check() -> float:
        limiter = RateLimiter(0.1)
        loop = asyncio.get_running_loop()
        start = loop.time()
        await limiter.acquire("a.example", min_gap=0.01)
        await limiter.acquire("a.example", min_gap=0.01)
        return loop.time() - start

    assert run(check()) >= 0.1


@respx.mock
def test_crawl_delay_spaces_sources_on_the_same_host(settings: Settings, feed_body: bytes) -> None:
    """Crawl-delay is the gap between requests to a host, not a per-source pause.

    robotparser only accepts whole seconds, so this test waits about one second.
    """
    respx.get(ROBOTS_URL).mock(
        return_value=httpx.Response(200, text="User-agent: *\nCrawl-delay: 1\nAllow: /\n")
    )
    hits: list[float] = []

    def record(request: httpx.Request) -> httpx.Response:
        hits.append(asyncio.get_running_loop().time())
        return httpx.Response(200, content=feed_body)

    respx.get(url__regex=r"https://feed\.example\.org/(world|business)\.xml").mock(
        side_effect=record
    )
    sources = [
        feed_source("World", "https://feed.example.org/world.xml"),
        feed_source("Business", "https://feed.example.org/business.xml"),
    ]

    async def check() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        results = await fetch_all(sources, settings)
        assert all(result.status == "ok" for result in results)
        return start

    start = run(check())
    assert len(hits) == 2
    first, second = sorted(hits)
    # No up-front pause before the first request; the full delay before the second.
    assert first - start < 0.5
    assert second - first >= 1.0


def test_rate_limiter_disabled_at_zero() -> None:
    async def check() -> float:
        limiter = RateLimiter(0.0)
        loop = asyncio.get_running_loop()
        start = loop.time()
        for _ in range(5):
            await limiter.acquire("a.example")
        return loop.time() - start

    assert run(check()) < 0.05


# --------------------------------------------------------------------------
# failure isolation
# --------------------------------------------------------------------------


@respx.mock
def test_one_failing_source_does_not_abort_the_run(settings: Settings, feed_body: bytes) -> None:
    good_url = "https://good.example.org/rss.xml"
    bad_url = "https://bad.example.org/rss.xml"
    junk_url = "https://junk.example.org/rss.xml"

    for host in ("good", "bad", "junk"):
        respx.get(f"https://{host}.example.org/robots.txt").mock(
            return_value=httpx.Response(200, text=ALLOW_ALL)
        )
    respx.get(good_url).mock(return_value=httpx.Response(200, content=feed_body))
    respx.get(bad_url).mock(return_value=httpx.Response(500))
    respx.get(junk_url).mock(return_value=httpx.Response(200, content=b"not a feed"))

    sources = [
        feed_source(name="Good", url=good_url),
        feed_source(name="Bad", url=bad_url),
        feed_source(name="Junk", url=junk_url),
    ]
    results = {result.source: result for result in run(fetch_all(sources, settings))}

    assert results["Good"].status == "ok"
    assert results["Good"].items_found == 5
    assert results["Bad"].status == "error"
    assert results["Bad"].error is not None
    assert results["Junk"].status == "error"
    assert not results["Bad"].ok


@respx.mock
def test_max_items_per_source_caps_results(feed_body: bytes) -> None:
    capped = Settings(rate_limit_seconds=0.0, max_items_per_source=2, concurrency=2)
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    assert run(fetch_all([feed_source()], capped))[0].items_found == 2


@respx.mock
def test_user_agent_is_sent(settings: Settings, feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    route = respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))

    run(fetch_all([feed_source()], settings))

    assert route.calls.last.request.headers["user-agent"] == settings.user_agent


def test_fetch_all_with_no_sources_is_a_no_op(settings: Settings) -> None:
    assert run(fetch_all([], settings)) == []
