"""Find a site's RSS/Atom feed and turn it into a ready-to-paste source entry.

`headliner discover URL...` exists so adding sources is a review step, not a
research task: for each site it reads the feeds the homepage advertises, falls
back to the feed paths common publishing platforms use, checks robots.txt for
every request, and test-parses each candidate before recommending it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from headliner.config import Config, Settings, Source
from headliner.fetcher import FetchError, RateLimiter, RobotsCache, build_client, fetch_url
from headliner.models import clean_text, normalise_url
from headliner.parsers import ParseError, find_feed_links, parse_feed

logger = logging.getLogger(__name__)

# Tried, in order, when a homepage advertises no usable feed. Each is the
# default of a platform news sites commonly run on.
FALLBACK_PATHS: Final = (
    "/feed/",  # WordPress
    "/rss/feed.xml",  # Nine (SMH, The Age, WAtoday, AFR)
    "/feed",
    "/rss",
    "/rss.xml",
    "/feed.xml",
    "/?service=rss",  # Reach plc (Irish Mirror, Dublin Live)
    "/arc/outboundfeeds/rss/?outputType=xml",  # Arc XP (Irish Times, Independent.ie)
    "/atom.xml",
    "/index.xml",
)
# Small pages that redirect elsewhere are common; anything this long that
# isn't a feed is not worth trying further candidates from the same page.
MAX_CANDIDATES: Final = 12

Status = Literal["ok", "configured", "failed"]


@dataclass(slots=True)
class Discovery:
    """What `discover` found for one site."""

    site: str
    status: Status
    feed_url: str | None = None
    name: str | None = None
    items: int = 0
    newest: datetime | None = None
    note: str = ""
    existing: str | None = None
    tried: list[str] = field(default_factory=list)
    item_urls: list[str] = field(default_factory=list)
    # Suggested when the site URL named a section (e.g. /news) but the feed
    # mixes sections: keep only items under that path.
    include_url_pattern: str | None = None
    include_kept: int = 0


def _host(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


def _suggest_name(feed_title: str, site: str) -> str:
    """A short source name: the feed's title up to its first separator, else the host."""
    title = clean_text(feed_title)
    for separator in (" | ", " - ", " – ", " — ", ": "):  # noqa: RUF001
        if separator in title:
            title = title.split(separator)[0].strip()
    if 3 <= len(title) <= 40:
        return title
    return _host(site)


def _already_configured(site: str, config: Config | None) -> Source | None:
    """A configured source on the same host as `site`, if any."""
    if config is None:
        return None
    host = _host(site)
    for source in config.sources:
        if _host(source.url) == host or host.endswith("." + _host(source.url)):
            return source
    # Feeds often live on a sibling host (feeds.bbci.co.uk for bbc.co.uk).
    registrable = ".".join(host.split(".")[-3:]) if host.count(".") >= 2 else host
    for source in config.sources:
        if _host(source.url).endswith(registrable):
            return source
    return None


async def _try_feed(
    client: httpx.AsyncClient,
    url: str,
    site: str,
    settings: Settings,
    limiter: RateLimiter,
    robots: RobotsCache,
) -> tuple[Discovery | None, str]:
    """A successful Discovery for `url`, or None and why it was rejected."""
    if not await robots.can_fetch(url):
        return None, "robots.txt disallows"
    await limiter.acquire(_host(url), min_gap=await robots.crawl_delay(url))
    try:
        response = await fetch_url(client, url, timeout=settings.request_timeout, attempts=1)
    except FetchError as exc:
        return None, str(exc).split(": ", 1)[-1]
    parsed = feedparser.parse(response.content)
    if not parsed.get("version") or not parsed.get("entries"):
        kind = response.headers.get("content-type", "unknown type").split(";")[0].strip()
        return None, f"not an RSS/Atom feed (got {kind})"
    probe = Source(name="probe", url=url, type="rss")
    try:
        headlines = parse_feed(response.content, probe)
    except ParseError as exc:
        return None, str(exc)
    if not headlines:
        return None, "feed has no usable items"
    final_url = str(response.url) if response.url else url
    dated = [h.published_at for h in headlines if h.published_at]
    return (
        Discovery(
            site=site,
            status="ok",
            feed_url=final_url,
            name=_suggest_name(str(parsed.get("feed", {}).get("title", "")), site),
            items=len(headlines),
            newest=max(dated) if dated else None,
            item_urls=[headline.url for headline in headlines],
        ),
        "",
    )


def _section_filter(site: str, found: Discovery) -> None:
    """Suggest `include_url_pattern` when the site URL names a section the feed exceeds.

    `https://www.newstalk.com/news` with a site-wide feed of news, competitions
    and show pages suggests "/news/". Nothing is suggested when every item, or
    none, is under the section path (the feed already fits, or the site does
    not put sections in its URLs).
    """
    section = urlsplit(site).path.rstrip("/")
    if not section:
        return
    prefix = section + "/"
    kept = [url for url in found.item_urls if urlsplit(url).path.startswith(prefix)]
    if kept and len(kept) < len(found.item_urls):
        found.include_url_pattern = re.escape(prefix).replace("\\/", "/")
        found.include_kept = len(kept)


async def discover_site(
    client: httpx.AsyncClient,
    site: str,
    *,
    settings: Settings,
    limiter: RateLimiter,
    robots: RobotsCache,
    config: Config | None = None,
) -> Discovery:
    """Find the best feed for one site URL. Never raises; failures are a status."""
    existing = _already_configured(site, config)
    if existing is not None:
        return Discovery(
            site=site,
            status="configured",
            existing=existing.name,
            feed_url=existing.url,
            note=f"already configured as {existing.name!r}",
        )

    candidates: list[str] = []
    page_note = ""
    if await robots.can_fetch(site):
        await limiter.acquire(_host(site))
        try:
            page = await fetch_url(client, site, timeout=settings.request_timeout, attempts=1)
        except FetchError as exc:
            page_note = f"homepage: {str(exc).split(': ', 1)[-1]}"
            if "HTTP 403" in page_note:
                page_note += ", likely bot protection"
        else:
            base = str(page.url) if page.url else site
            candidates.extend(find_feed_links(page.content, base))
    else:
        page_note = "homepage: robots.txt disallows"

    advertised = set(candidates)
    origin = f"{urlsplit(site).scheme}://{urlsplit(site).netloc}"
    for path in FALLBACK_PATHS:
        candidate = urljoin(origin, path)
        if candidate not in candidates:
            candidates.append(candidate)

    reasons: list[str] = []
    robots_blocked = 0
    tried: list[str] = []
    seen: set[str] = set()
    for candidate in candidates[:MAX_CANDIDATES]:
        key = normalise_url(candidate)
        if key in seen:
            continue
        seen.add(key)
        tried.append(candidate)
        found, why = await _try_feed(client, candidate, site, settings, limiter, robots)
        if found is not None:
            found.tried = tried
            _section_filter(site, found)
            logger.info("%s: feed %s (%d items)", site, found.feed_url, found.items)
            return found
        logger.debug("%s: %s rejected: %s", site, candidate, why)
        if why == "robots.txt disallows":
            robots_blocked += 1
        elif candidate in advertised:
            reasons.append(f"advertised feed {candidate}: {why}")

    if robots_blocked == len(tried) and page_note.endswith("robots.txt disallows"):
        note = "robots.txt blocks crawlers from the homepage and every feed URL tried"
    else:
        note = "no RSS/Atom feed found"
        details = [page_note] if page_note else []
        details += reasons
        if robots_blocked:
            details.append(f"robots.txt disallows {robots_blocked} of {len(tried)} URLs tried")
        if details:
            note += f" ({'; '.join(details)})"
    logger.info("%s: %s", site, note)
    return Discovery(site=site, status="failed", note=note, tried=tried)


async def discover(
    sites: list[str],
    settings: Settings,
    *,
    config: Config | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[Discovery]:
    """Discover feeds for several sites concurrently, in the order given."""
    owned = client is None
    active = client or build_client(settings)
    limiter = RateLimiter(settings.rate_limit_seconds)
    robots = RobotsCache(active, settings.user_agent)
    semaphore = asyncio.Semaphore(settings.concurrency)

    async def one(site: str) -> Discovery:
        async with semaphore:
            return await discover_site(
                active, site, settings=settings, limiter=limiter, robots=robots, config=config
            )

    try:
        return list(await asyncio.gather(*(one(site) for site in sites)))
    finally:
        if owned:
            await active.aclose()


_YAML_PLAIN_RE: Final = re.compile(r"^[A-Za-z0-9][^\n]*$")
_YAML_RESERVED: Final = frozenset({"yes", "no", "true", "false", "on", "off", "null", "~"})


def _yaml_string(value: str) -> str:
    """A YAML scalar for `value`: plain when that is unambiguous, else double-quoted."""
    plain = (
        _YAML_PLAIN_RE.match(value) is not None
        and ": " not in value
        and " #" not in value
        and not value.endswith((" ", ":"))
        and value.lower() not in _YAML_RESERVED
    )
    # A JSON string is a valid YAML double-quoted scalar.
    return value if plain else json.dumps(value, ensure_ascii=False)


def render_yaml(results: list[Discovery], tags: list[str], *, generated: datetime) -> str:
    """Source entries ready to paste under `sources:`, plus comments for the rest."""
    tag_list = f"[{', '.join(tags)}]" if tags else None
    lines = [
        f"  # --- headliner discover, {generated:%Y-%m-%d %H:%M %Z}: review names, then paste"
        " under `sources:` ---",
    ]
    for result in results:
        if result.status == "ok" and result.feed_url and result.name:
            newest = f", newest {result.newest:%Y-%m-%d %H:%M}Z" if result.newest else ""
            lines.append(f"  # {result.site} -> {result.items} items{newest}")
            lines.append(f"  - name: {_yaml_string(result.name)}")
            lines.append(f"    url: {_yaml_string(result.feed_url)}")
            lines.append("    type: rss")
            if tag_list:
                lines.append(f"    tags: {tag_list}")
            if result.include_url_pattern:
                lines.append(
                    f"    # the feed covers the whole site; this keeps {result.include_kept}"
                    f" of {result.items} items, those under {result.include_url_pattern}"
                )
                lines.append(f"    include_url_pattern: {_yaml_string(result.include_url_pattern)}")
            lines.append("")
    for result in results:
        if result.status == "configured":
            hint = f"; add tags {tag_list} there" if tag_list else ""
            lines.append(f"  # CONFIGURED {result.site}: as {result.existing!r}{hint}")
    for result in results:
        if result.status == "failed":
            lines.append(f"  # FAILED {result.site}: {result.note}")
    return "\n".join(lines).rstrip() + "\n"
