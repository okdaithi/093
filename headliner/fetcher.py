"""Async HTTP: robots.txt gating, per-domain rate limiting and retries."""

from __future__ import annotations

import asyncio
import logging
import random
import urllib.robotparser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import urljoin, urlsplit

import httpx

from headliner import frontpage
from headliner.config import Settings, Source
from headliner.models import Headline, utcnow
from headliner.parsers import ParseError, parse

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 3
BACKOFF_BASE_SECONDS: Final = 1.0
BACKOFF_CAP_SECONDS: Final = 30.0
RETRY_STATUSES: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
ROBOTS_TIMEOUT_SECONDS: Final = 10.0


class FetchError(RuntimeError):
    """A source could not be fetched after every retry was spent."""


class RobotsDisallowedError(FetchError):
    """robots.txt forbids the configured URL for our user agent."""


@dataclass(slots=True)
class SourceResult:
    """Outcome of processing one source. Never raises upward — see `status`."""

    source: str
    status: str
    started_at: datetime
    finished_at: datetime
    items_found: int = 0
    items_new: int = 0
    items_changed: int = 0
    items_changed_live: int = 0
    items_changed_minor: int = 0
    error: str | None = None
    headlines: list[Headline] = field(default_factory=list)
    # The source's front page this run, when it has one and it was read.
    front_page: frontpage.FrontPageResult | None = None
    # False when this run did not read the feed (`--front-pages-only`): then
    # nothing goes to fetch_log for it.
    feed_ran: bool = True

    @property
    def ok(self) -> bool:
        """True when the source did not fail (fetched or deliberately skipped)."""
        return self.status in {"ok", "skipped"}


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse a `Retry-After` header in either delay-seconds or HTTP-date form."""
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    raw = raw.strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    # An HTTP-date is always GMT; parsedate_to_datetime returns naive for "-0000".
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - utcnow()).total_seconds())


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with full jitter; `Retry-After` wins when present."""
    if retry_after is not None:
        return min(retry_after, BACKOFF_CAP_SECONDS)
    ceiling = min(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), BACKOFF_CAP_SECONDS)
    # Jitter spreads retries across clients; it does not need to be cryptographic.
    return random.uniform(0.0, ceiling)


class RateLimiter:
    """Enforces a minimum gap between requests to the same domain."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = max(0.0, delay_seconds)
        self._locks: dict[str, asyncio.Lock] = {}
        self._holds: dict[str, asyncio.Lock] = {}
        self._last_request: dict[str, float] = {}

    @asynccontextmanager
    async def hold(self, domain: str) -> AsyncIterator[None]:
        """One request to `domain` at a time: hold this around the whole request."""
        async with self._holds.setdefault(domain, asyncio.Lock()):
            yield

    async def acquire(self, domain: str, *, min_gap: float | None = None) -> None:
        """Sleep, if needed, until this domain may be hit again.

        `min_gap` (e.g. a robots.txt `Crawl-delay`) widens the gap for this
        request when it is longer than the configured delay.
        """
        gap = max(self._delay, min_gap or 0.0)
        if gap <= 0:
            return
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            last = self._last_request.get(domain)
            if last is not None:
                wait = gap - (loop.time() - last)
                if wait > 0:
                    logger.debug("rate limit: sleeping %.2fs before hitting %s", wait, domain)
                    await asyncio.sleep(wait)
            self._last_request[domain] = loop.time()


class RobotsCache:
    """Fetches and caches one robots.txt per domain."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _load(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """Return a parser for the URL's origin, or None when robots is unavailable."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._cache:
            return self._cache[origin]

        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin in self._cache:
                return self._cache[origin]

            robots_url = urljoin(origin, "/robots.txt")
            parser: urllib.robotparser.RobotFileParser | None = None
            try:
                response = await self._client.get(
                    robots_url,
                    timeout=ROBOTS_TIMEOUT_SECONDS,
                    follow_redirects=True,
                )
            except httpx.HTTPError as exc:
                logger.debug("robots.txt unreachable at %s (%s); allowing", robots_url, exc)
            else:
                if response.status_code == 200:
                    parser = urllib.robotparser.RobotFileParser()
                    parser.set_url(robots_url)
                    parser.parse(response.text.splitlines())
                elif 400 <= response.status_code < 500:
                    # RFC 9309: 4xx means unrestricted.
                    logger.debug("robots.txt %s at %s; allowing", response.status_code, robots_url)
                else:
                    # 5xx should mean "disallow all", but that would silently
                    # zero out a run on a flaky origin, so log loudly and allow.
                    logger.warning(
                        "robots.txt returned %s at %s; proceeding without rules",
                        response.status_code,
                        robots_url,
                    )
            self._cache[origin] = parser
            return parser

    async def can_fetch(self, url: str) -> bool:
        """True when our user agent may fetch `url`."""
        parser = await self._load(url)
        if parser is None:
            return True
        return bool(parser.can_fetch(self._user_agent, url))

    async def crawl_delay(self, url: str) -> float | None:
        """Any `Crawl-delay` the origin declares for our user agent."""
        parser = await self._load(url)
        if parser is None:
            return None
        try:
            value = parser.crawl_delay(self._user_agent)
        except (AttributeError, ValueError):
            return None
        return float(value) if value is not None else None


async def fetch_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    attempts: int = MAX_ATTEMPTS,
    sleep: bool = True,
) -> httpx.Response:
    """GET `url`, retrying 429/5xx/timeouts with jittered exponential backoff.

    Raises `FetchError` once every attempt is spent, or immediately on a
    non-retryable 4xx.
    """
    last_error: str = "no attempt was made"
    for attempt in range(1, attempts + 1):
        try:
            response = await client.get(url, timeout=timeout, follow_redirects=True)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            logger.debug("attempt %d/%d for %s failed: %s", attempt, attempts, url, last_error)
        else:
            if response.status_code < 400:
                return response
            last_error = f"HTTP {response.status_code}"
            if response.status_code not in RETRY_STATUSES:
                raise FetchError(f"{url}: {last_error}")
            retry_after = _retry_after_seconds(response)
            if retry_after is not None and retry_after > BACKOFF_CAP_SECONDS:
                # Retrying sooner than the server asked is impolite; leave this
                # source for the next scheduled run instead.
                raise FetchError(
                    f"{url}: {last_error} with Retry-After {retry_after:.0f}s "
                    f"(over the {BACKOFF_CAP_SECONDS:.0f}s cap); not retrying this run"
                )
            delay = _backoff_delay(attempt, retry_after)
            logger.debug(
                "attempt %d/%d for %s got %s; retrying in %.2fs",
                attempt,
                attempts,
                url,
                last_error,
                delay,
            )
            if attempt < attempts and sleep and delay > 0:
                await asyncio.sleep(delay)
            continue

        if attempt < attempts:
            delay = _backoff_delay(attempt, None)
            if sleep and delay > 0:
                await asyncio.sleep(delay)

    raise FetchError(f"{url}: giving up after {attempts} attempts ({last_error})")


async def _fetch_feed(
    client: httpx.AsyncClient,
    source: Source,
    settings: Settings,
    *,
    limiter: RateLimiter,
    robots: RobotsCache | None,
    started_at: datetime,
) -> SourceResult:
    """Fetch and parse one source's feed (or listing page)."""

    def result(
        status: str, *, error: str | None = None, items: list[Headline] | None = None
    ) -> SourceResult:
        return SourceResult(
            source=source.name,
            status=status,
            started_at=started_at,
            finished_at=utcnow(),
            items_found=len(items or []),
            error=error,
            headlines=items or [],
        )

    try:
        if robots is not None:
            if not await robots.can_fetch(source.url):
                message = f"robots.txt disallows {source.url}"
                logger.info("%s: skipped - %s", source.name, message)
                return result("skipped", error=message)
            crawl_delay = await robots.crawl_delay(source.url)
            if crawl_delay is not None and crawl_delay > settings.rate_limit_seconds:
                logger.debug("%s: honouring robots Crawl-delay of %.1fs", source.name, crawl_delay)
        else:
            crawl_delay = None

        # The Crawl-delay is the gap between requests to the domain, so it
        # goes through the limiter rather than being slept up front. The hold
        # keeps this from overlapping a front-page read on the same domain.
        async with limiter.hold(source.domain):
            await limiter.acquire(source.domain, min_gap=crawl_delay)
            logger.debug("%s: fetching %s", source.name, source.url)
            response = await fetch_url(client, source.url, timeout=settings.request_timeout)
        headlines = parse(
            response.content,
            source,
            fetched_at=utcnow(),
            limit=settings.max_items_per_source,
        )
    except (FetchError, ParseError) as exc:
        logger.warning("%s: %s", source.name, exc)
        return result("error", error=str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad source must not abort the run
        logger.warning("%s: unexpected %s: %s", source.name, type(exc).__name__, exc)
        return result("error", error=f"{type(exc).__name__}: {exc}")

    logger.info("%s: %d headline(s)", source.name, len(headlines))
    return result("ok", items=headlines)


def _from_front_page(
    source: Source, page: frontpage.FrontPageResult | None, started_at: datetime
) -> SourceResult:
    """The run's record for a source with no feed, from its front-page read."""
    if page is None:
        return SourceResult(
            source=source.name,
            status="skipped",
            started_at=started_at,
            finished_at=utcnow(),
            feed_ran=False,
        )
    if page.ok:
        status, error = "ok", None
    elif page.status == frontpage.ROBOTS_DENIED:
        status, error = "skipped", page.error
    else:
        status, error = "error", f"front page {page.status}: {page.error or 'no detail'}"
    return SourceResult(
        source=source.name,
        status=status,
        started_at=started_at,
        finished_at=page.finished_at,
        items_found=len(page.headlines),
        error=error,
        front_page=page,
    )


async def fetch_source(
    client: httpx.AsyncClient,
    source: Source,
    settings: Settings,
    *,
    limiter: RateLimiter,
    robots: RobotsCache | None,
    semaphore: asyncio.Semaphore,
    feeds: bool = True,
    front_pages: bool = True,
    front_page_cache: frontpage.CacheEntry | None = None,
) -> SourceResult:
    """Fetch and parse one source. Failures become a `SourceResult`, not an exception.

    The feed comes first and is unaffected by the front page: when the source
    has a front page too, it is read afterwards and its outcome, good or bad,
    is attached as `front_page`.
    """
    started_at = utcnow()
    async with semaphore:
        feed: SourceResult | None = None
        if feeds and source.has_feed:
            feed = await _fetch_feed(
                client, source, settings, limiter=limiter, robots=robots, started_at=started_at
            )
        page: frontpage.FrontPageResult | None = None
        if front_pages and source.front_page_url:
            try:
                page = await frontpage.fetch_front_page(
                    client,
                    source,
                    settings,
                    limiter=limiter,
                    robots=robots,
                    cache=front_page_cache,
                )
            except Exception as exc:  # noqa: BLE001 - never let the front page sink the feed
                logger.warning(
                    'source="%s" method="front_page" status="error" error="%s: %s"',
                    source.name,
                    type(exc).__name__,
                    exc,
                )
    if feed is None:
        if source.has_feed:  # feeds were switched off for this run
            return SourceResult(
                source=source.name,
                status="skipped",
                started_at=started_at,
                finished_at=utcnow(),
                front_page=page,
                feed_ran=False,
            )
        return _from_front_page(source, page, started_at)
    feed.front_page = page
    return feed


def build_client(settings: Settings) -> httpx.AsyncClient:
    """An `AsyncClient` carrying the configured descriptive User-Agent."""
    return httpx.AsyncClient(
        headers={
            "User-Agent": settings.user_agent,
            "Accept": (
                "application/rss+xml, application/atom+xml, application/xml;q=0.9, "
                "text/html;q=0.8, */*;q=0.5"
            ),
            "Accept-Language": "en",
        },
        timeout=settings.request_timeout,
        follow_redirects=True,
    )


async def fetch_all(
    sources: list[Source],
    settings: Settings,
    *,
    ignore_robots: bool = False,
    client: httpx.AsyncClient | None = None,
    feeds: bool = True,
    front_pages: bool = True,
    front_page_cache: dict[str, frontpage.CacheEntry] | None = None,
) -> list[SourceResult]:
    """Fetch every source concurrently, bounded by `settings.concurrency`.

    `feeds`/`front_pages` switch either acquisition path off for the run;
    `front_page_cache` holds the previous run's validators per source name.
    """
    if not sources:
        return []

    owned = client is None
    active = client or build_client(settings)
    limiter = RateLimiter(settings.rate_limit_seconds)
    robots = None if ignore_robots else RobotsCache(active, settings.user_agent)
    semaphore = asyncio.Semaphore(settings.concurrency)

    if ignore_robots:
        logger.warning("--ignore-robots is set: robots.txt rules will not be consulted")

    try:
        return list(
            await asyncio.gather(
                *(
                    fetch_source(
                        active,
                        source,
                        settings,
                        limiter=limiter,
                        robots=robots,
                        semaphore=semaphore,
                        feeds=feeds,
                        front_pages=front_pages,
                        front_page_cache=(front_page_cache or {}).get(source.name),
                    )
                    for source in sources
                )
            )
        )
    finally:
        if owned:
            await active.aclose()
