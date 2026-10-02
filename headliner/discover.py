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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx

from headliner.config import Config, Settings, Source
from headliner.fetcher import FetchError, RateLimiter, RobotsCache, build_client, fetch_url
from headliner.models import clean_text, normalise_url, utcnow
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
# A feed whose newest item is older than this has stopped updating (publishers
# often leave retired feeds online, frozen).
STALE_AFTER: Final = timedelta(days=14)

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
    # Tags for this site's entry; empty means the tags given to `render_yaml`.
    tags: tuple[str, ...] = ()
    # The batch group the site was listed under, for grouping the output.
    group: str = ""


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
    """A configured source on the same host as `site`, if any.

    A section URL (`https://www.rte.ie/news/galway`) only counts as configured
    when an existing source's URL mentions that section; otherwise the section
    may have its own feed worth adding.
    """
    if config is None:
        return None
    found = _same_site(site, config)
    split = urlsplit(site)
    # What distinguishes this URL within its site: the query (`?index=/news/galway/`)
    # if it has one, else the last path segment (`galway`).
    section = (split.query or split.path.rstrip("/").rsplit("/", 1)[-1]).lower()
    # A generic section (e.g. /news on a news site) is the site's main feed.
    if (
        found is not None
        and section
        and section not in found.url.lower()
        and not _is_generic_section(section)
    ):
        return None
    return found


_GENERIC_SECTIONS: Final = frozenset(
    {"news", "home", "index.html", "home.htm", "index.htm", "en", "english", "latest"}
)


def _is_generic_section(section: str) -> bool:
    return section in _GENERIC_SECTIONS


def _same_site(site: str, config: Config) -> Source | None:
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
    dated = [h.published_at for h in headlines if h.published_at]
    if dated and utcnow() - max(dated) > STALE_AFTER:
        return None, f"feed is stale: newest item {max(dated):%Y-%m-%d}"
    final_url = str(response.url) if response.url else url
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
            parsed = feedparser.parse(page.content)
            if parsed.get("version") and parsed.get("entries"):
                # The URL given is itself a feed (e.g. rss.nytimes.com/...).
                candidates.append(site)
            else:
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
        elif candidate in advertised or why.startswith("feed is stale"):
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


def _tag_list(tags: Sequence[str]) -> str | None:
    return f"[{', '.join(tags)}]" if tags else None


def render_yaml(results: list[Discovery], tags: list[str], *, generated: datetime) -> str:
    """Source entries ready to paste under `sources:`, plus comments for the rest.

    Each entry carries its own tags when it has them (from a batch), else `tags`.
    """
    default_tags = _tag_list(tags)
    lines = [
        f"  # --- headliner discover, {generated:%Y-%m-%d %H:%M %Z}: review names, then paste"
        " under `sources:` ---",
    ]
    group = None
    for result in results:
        tag_list = _tag_list(result.tags) or default_tags
        if result.status == "ok" and result.feed_url and result.name:
            if result.group and result.group != group:
                group = result.group
                lines.append(f"  # == {group} ==")
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
            tag_list = _tag_list(result.tags) or default_tags
            hint = f"; add tags {tag_list} there" if tag_list else ""
            lines.append(f"  # CONFIGURED {result.site}: as {result.existing!r}{hint}")
    for result in results:
        if result.status == "failed":
            lines.append(f"  # FAILED {result.site}: {result.note}")
    return "\n".join(lines).rstrip() + "\n"


# --- Batches ------------------------------------------------------------------------
#
# A batch file is a pasted list of sites under group headers:
#
#     IE galway            <- header: every word is a tag for the sites below
#     https://www.galwaybeo.ie/
#     https://www.rte.ie/news/galway/
#
#     asia
#     https://japantoday.com/  JP     <- words after a URL add tags to that site
#
# Two-letter tags are country codes and are upper-cased (`cn` -> `CN`); a
# country code after a URL replaces the group's. Other tags are lower-cased.
# Country tags come first in each entry's list.
# Blank lines and lines starting with `#` are ignored. Repeated sites (same
# host and path, either scheme) are listed once.

_URL_LINE: Final = re.compile(r"^(https?://\S+)\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class BatchEntry:
    url: str
    tags: tuple[str, ...]
    group: str


class BatchError(ValueError):
    """A batch file line that is neither a header, a URL, nor a comment."""


def normalise_tag(tag: str) -> str:
    """`cn` -> `CN` (country code); anything longer -> lower case."""
    clean = tag.strip().strip(",")
    return clean.upper() if len(clean) == 2 and clean.isalpha() else clean.lower()


def _tags_from(words: str) -> list[str]:
    return [normalise_tag(word) for word in re.split(r"[\s,]+", words) if word.strip(",")]


def _is_country(tag: str) -> bool:
    return len(tag) == 2 and tag.isalpha() and tag.isupper()


def parse_batch(text: str) -> list[BatchEntry]:
    """Sites and their tags from a batch file (format above)."""
    entries: list[BatchEntry] = []
    seen: set[str] = set()
    group_tags: list[str] = []
    group = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _URL_LINE.match(line)
        if match is None:
            if "://" in line:
                raise BatchError(f"line {number}: not an http(s) URL: {line!r}")
            group_tags = _tags_from(line)
            group = line
            continue
        url, extra = match.groups()
        line_tags = _tags_from(extra)
        if any(_is_country(tag) for tag in line_tags):
            tags = [tag for tag in group_tags if not _is_country(tag)]
        else:
            tags = list(group_tags)
        for tag in line_tags:
            if tag.casefold() not in {existing.casefold() for existing in tags}:
                tags.append(tag)
        if not tags:
            raise BatchError(f"line {number}: {url} has no tags; put a header line above it")
        # Country first, as in the shipped config: [JP, asia, business].
        tags.sort(key=lambda tag: not _is_country(tag))
        split = urlsplit(url)
        key = f"{_host(url)}{split.path.rstrip('/')}?{split.query}"
        if key in seen:
            continue
        seen.add(key)
        entries.append(BatchEntry(url=url, tags=tuple(tags), group=group))
    return entries


def mark_duplicate_feeds(results: list[Discovery], config: Config | None = None) -> None:
    """A feed already in `config`, or found for two sites in one run, is not added twice."""
    configured = {normalise_url(s.url): s.name for s in config.sources} if config else {}
    first: dict[str, Discovery] = {}
    for result in results:
        if result.status != "ok" or result.feed_url is None:
            continue
        key = normalise_url(result.feed_url)
        if key in configured:
            result.status = "configured"
            result.existing = configured[key]
            result.note = f"its feed is {configured[key]!r}'s, already configured"
            continue
        if key in first:
            earlier = first[key]
            result.status = "configured"
            result.existing = earlier.name
            result.note = f"same feed as {earlier.site}"
        else:
            first[key] = result


def render_report(results: list[Discovery]) -> str:
    """A Markdown triage table: one row per site."""
    lines = [
        "| Group | Site | Result | Feed / reason | Items | Tags |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    labels = {"ok": "found", "configured": "already configured", "failed": "FAILED"}
    for result in results:
        if result.status == "ok":
            detail = result.feed_url or ""
            if result.include_url_pattern:
                detail += f" (keep `{result.include_url_pattern}`)"
        elif result.status == "configured":
            detail = result.note or f"as {result.existing!r}"
        else:
            detail = result.note
        lines.append(
            "| {group} | {site} | {label} | {detail} | {items} | {tags} |".format(
                group=result.group or "-",
                site=result.site,
                label=labels[result.status],
                detail=detail.replace("|", "\\|"),
                items=result.items or "",
                tags=", ".join(result.tags),
            )
        )
    found = sum(1 for result in results if result.status == "ok")
    known = sum(1 for result in results if result.status == "configured")
    failed = sum(1 for result in results if result.status == "failed")
    lines.append("")
    lines.append(f"{found} found, {known} already configured, {failed} failed.")
    return "\n".join(lines) + "\n"
