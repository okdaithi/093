"""Turn fetched bytes into `Headline` records.

RSS/Atom goes through `feedparser`. HTML uses `selectolax` when it is
installed and falls back to `beautifulsoup4` + `lxml` otherwise.
"""

from __future__ import annotations

import calendar
import importlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urljoin

import feedparser

from headliner.config import Source
from headliner.models import Headline, InvalidHeadlineError, clean_text, to_utc, utcnow

logger = logging.getLogger(__name__)

# selectolax is the preferred HTML backend; beautifulsoup4 is the fallback.
# Both are held as plain callables so only `_build_tree` cares which is in use.
# selectolax 1.0 dropped its old "modest" backend (`selectolax.parser` now raises
# ImportError), so the lexbor backend, present since 0.3, is tried first.
_selectolax: Callable[[str], Any] | None = None
_beautifulsoup: Callable[..., Any] | None = None
HTML_BACKEND = "none"

for _module, _name in (
    ("selectolax.lexbor", "LexborHTMLParser"),
    ("selectolax.parser", "HTMLParser"),
):
    try:
        _selectolax = getattr(importlib.import_module(_module), _name)
    except (ImportError, AttributeError):  # not installed, or a release without that backend
        continue
    break
if _selectolax is not None:
    HTML_BACKEND = "selectolax"

if _selectolax is None:  # pragma: no cover - only without selectolax
    try:
        from bs4 import BeautifulSoup as _bs4_parser
    except ImportError:
        pass
    else:
        _beautifulsoup = _bs4_parser
        HTML_BACKEND = "beautifulsoup4"


class ParseError(RuntimeError):
    """Raised when a document cannot be parsed at all."""


# Common ISO-ish and human date layouts seen in HTML `datetime` attributes.
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%d %B %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
)


def parse_datetime(value: str | None) -> datetime | None:
    """Best-effort parse of a date string into an aware UTC datetime."""
    text = clean_text(value)
    if not text:
        return None

    # Naive values are treated as UTC (via `to_utc`); `astimezone` on a naive
    # datetime would read it as the host's local time instead.
    candidate = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        return to_utc(datetime.fromisoformat(candidate))
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        return to_utc(parsed)

    for fmt in _DATE_FORMATS:
        try:
            return to_utc(datetime.strptime(text, fmt))
        except ValueError:
            continue

    logger.debug("unparseable date %r", text)
    return None


def _struct_time_to_datetime(value: Any) -> datetime | None:
    """feedparser hands back a UTC `time.struct_time`; convert it safely."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def _entry_url(entry: Any, base_url: str) -> str | None:
    link = entry.get("link")
    if isinstance(link, str) and link.strip():
        return urljoin(base_url, link.strip())
    for candidate in entry.get("links", []) or []:
        href = candidate.get("href") if isinstance(candidate, dict) else None
        if isinstance(href, str) and href.strip():
            return urljoin(base_url, href.strip())
    identifier = entry.get("id")
    if isinstance(identifier, str) and identifier.strip().startswith(("http://", "https://")):
        return identifier.strip()
    return None


def _entry_summary(entry: Any) -> str | None:
    for key in ("summary", "description", "subtitle"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content = entry.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            value = first.get("value")
            if isinstance(value, str) and value.strip():
                return value
    return None


def parse_feed(
    body: bytes | str,
    source: Source,
    *,
    fetched_at: datetime | None = None,
    limit: int | None = None,
) -> list[Headline]:
    """Parse RSS or Atom bytes into headlines, newest-first as published."""
    stamp = fetched_at or utcnow()
    parsed = feedparser.parse(body)

    # feedparser sets `bozo` for malformed XML but still recovers entries most
    # of the time, so only a total absence of entries is fatal.
    if parsed.get("bozo") and not parsed.get("entries"):
        reason = parsed.get("bozo_exception")
        raise ParseError(f"could not parse feed for {source.name}: {reason}")

    headlines: list[Headline] = []
    for entry in parsed.get("entries", []):
        url = _entry_url(entry, source.url)
        published = _struct_time_to_datetime(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if published is None:
            published = parse_datetime(entry.get("published") or entry.get("updated"))
        try:
            headline = Headline.create(
                source=source.name,
                title=entry.get("title"),
                url=url,
                published_at=published,
                fetched_at=stamp,
                live_url_pattern=source.live_regex,
                summary=_entry_summary(entry),
            )
        except InvalidHeadlineError as exc:
            logger.debug("%s: skipping feed entry: %s", source.name, exc)
            continue
        headlines.append(headline)
        if limit is not None and len(headlines) >= limit:
            break
    return headlines


class _Node(Protocol):
    """The slice of a parsed element both HTML backends can provide."""

    def select_one(self, selector: str) -> _Node | None: ...

    def select_all(self, selector: str) -> list[_Node]: ...

    def text(self) -> str: ...

    def attr(self, name: str) -> str | None: ...


class _LexborNode:
    """selectolax adapter."""

    __slots__ = ("_node",)

    def __init__(self, node: Any) -> None:
        self._node = node

    def select_one(self, selector: str) -> _Node | None:
        found = self._node.css_first(selector)
        return _LexborNode(found) if found is not None else None

    def select_all(self, selector: str) -> list[_Node]:
        return [_LexborNode(node) for node in self._node.css(selector)]

    def text(self) -> str:
        return str(self._node.text(separator=" ", strip=False))

    def attr(self, name: str) -> str | None:
        value = self._node.attributes.get(name)
        return str(value) if isinstance(value, str) else None


class _SoupNode:
    """beautifulsoup4 adapter."""

    __slots__ = ("_node",)

    def __init__(self, node: Any) -> None:
        self._node = node

    def select_one(self, selector: str) -> _Node | None:
        found = self._node.select_one(selector)
        return _SoupNode(found) if found is not None else None

    def select_all(self, selector: str) -> list[_Node]:
        return [_SoupNode(node) for node in self._node.select(selector)]

    def text(self) -> str:
        return str(self._node.get_text(" "))

    def attr(self, name: str) -> str | None:
        value = self._node.get(name)
        if isinstance(value, list):
            value = " ".join(str(item) for item in value)
        return str(value) if value is not None else None


def _build_tree(body: bytes | str) -> _Node:
    """Parse a document with whichever HTML backend is installed."""
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    if _selectolax is not None:
        return _LexborNode(_selectolax(text).root)
    if _beautifulsoup is not None:  # pragma: no cover - fallback path
        try:
            return _SoupNode(_beautifulsoup(text, "lxml"))
        except Exception:  # noqa: BLE001 - lxml missing; html.parser always works
            return _SoupNode(_beautifulsoup(text, "html.parser"))
    raise ParseError(
        "no HTML backend available; install 'selectolax' or 'beautifulsoup4' and 'lxml'"
    )


def _node_text(node: _Node | None) -> str:
    return clean_text(node.text()) if node is not None else ""


def _extract_link(node: _Node, selector: str, base_url: str) -> str | None:
    """Resolve the href of `selector` (or of the node itself) against `base_url`."""
    target = node.select_one(selector)
    if target is None:
        return None
    href = target.attr("href") or target.attr("data-href")
    if not href:
        nested = target.select_one("a[href]")
        href = nested.attr("href") if nested is not None else None
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    return urljoin(base_url, href)


def _extract_date(node: _Node, selector: str | None) -> datetime | None:
    if not selector:
        return None
    target = node.select_one(selector)
    if target is None:
        return None
    raw = target.attr("datetime") or target.attr("content") or target.text()
    return parse_datetime(raw)


def parse_html(
    body: bytes | str,
    source: Source,
    *,
    fetched_at: datetime | None = None,
    limit: int | None = None,
) -> list[Headline]:
    """Parse an HTML listing page using the source's configured selectors."""
    if not (source.article_selector and source.title_selector and source.link_selector):
        raise ParseError(f"{source.name}: html source is missing required selectors")

    stamp = fetched_at or utcnow()
    tree = _build_tree(body)
    headlines: list[Headline] = []
    seen: set[str] = set()

    for article in tree.select_all(source.article_selector):
        title = _node_text(article.select_one(source.title_selector))
        url = _extract_link(article, source.link_selector, source.url)
        if not url or url in seen:
            continue
        try:
            headline = Headline.create(
                source=source.name,
                title=title,
                url=url,
                published_at=_extract_date(article, source.date_selector),
                fetched_at=stamp,
                live_url_pattern=source.live_regex,
                summary=_node_text(article.select_one(source.summary_selector))
                if source.summary_selector
                else None,
            )
        except InvalidHeadlineError as exc:
            logger.debug("%s: skipping article node: %s", source.name, exc)
            continue
        seen.add(url)
        headlines.append(headline)
        if limit is not None and len(headlines) >= limit:
            break
    return headlines


def parse(
    body: bytes | str,
    source: Source,
    *,
    fetched_at: datetime | None = None,
    limit: int | None = None,
) -> list[Headline]:
    """Dispatch to the parser matching `source.type`, then apply `include_url_pattern`.

    The filter runs before `limit`, so a filtered source still gets its full
    quota of matching items.
    """
    include = source.include_regex
    raw_limit = None if include is not None else limit
    if source.type == "rss":
        headlines = parse_feed(body, source, fetched_at=fetched_at, limit=raw_limit)
    elif source.type == "html":
        headlines = parse_html(body, source, fetched_at=fetched_at, limit=raw_limit)
    else:
        raise ParseError(f"{source.name}: unsupported source type {source.type!r}")
    if include is not None:
        headlines = [headline for headline in headlines if include.search(headline.url)]
        if limit is not None:
            headlines = headlines[:limit]
    return headlines


_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+xml")


def find_feed_links(body: bytes | str, base_url: str) -> list[str]:
    """Feed URLs a page advertises with `<link rel="alternate" type="...rss/atom...">`.

    In page order, resolved against `base_url`, without duplicates.
    """
    tree = _build_tree(body)
    found: list[str] = []
    for link in tree.select_all("link"):
        rel = (link.attr("rel") or "").lower().split()
        kind = (link.attr("type") or "").lower().split(";")[0].strip()
        href = (link.attr("href") or "").strip()
        if "alternate" in rel and kind in _FEED_TYPES and href:
            url = urljoin(base_url, href)
            if url not in found:
                found.append(url)
    return found
