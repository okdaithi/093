"""Read headlines straight off a publisher's live front page.

A second acquisition path next to the feed (see the README, "Front pages").
The feed stays primary: whatever happens here never changes how a feed is
fetched or stored, and a front page that cannot be read only costs its own
headlines.

Extraction runs in layers and keeps the page's order, which is editorial:

1. the source's configured CSS selectors, when given and when they match;
2. otherwise every link on the page, scored on where it sits (inside
   `<article>`, a heading, a story/card/teaser container; never navigation,
   menus, footers or account/social/cookie chrome) and on whether its URL and
   text look like an article;
3. JSON-LD (`NewsArticle`, `Article`, `ItemList`) and schema.org microdata,
   which add dates, sections and images to links found above, and stand in
   for them when the page lists articles only as metadata.

Fetching obeys robots.txt and the shared per-domain rate limit, sends one
request per domain at a time, follows redirects itself so that every hop is
checked (http(s) only, never a private or local address), caps the body size
and sends `If-None-Match`/`If-Modified-Since` from the previous run. Bot
challenges are recorded as `blocked`, never worked around. Downloaded HTML is
only parsed: no script runs and nothing from the page is written to disk.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urljoin, urlsplit

import httpx

from headliner.config import FrontPage, Settings, Source
from headliner.models import (
    MIN_TITLE_LENGTH,
    Headline,
    InvalidHeadlineError,
    clean_text,
    match_key,
    site_of,
    title_key,
    utcnow,
)
from headliner.parsers import Node, ParseError, build_tree, descendants, parse_datetime

if TYPE_CHECKING:  # fetcher imports this module
    from headliner.fetcher import RateLimiter, RobotsCache

logger = logging.getLogger(__name__)

METHOD: Final = "front_page"

# Outcomes of one front-page read. `healthy` and `not_modified` are successes.
HEALTHY: Final = "healthy"
NOT_MODIFIED: Final = "not_modified"
BLOCKED: Final = "blocked"
ROBOTS_DENIED: Final = "robots_denied"
TIMEOUT: Final = "timeout"
PARSE_ERROR: Final = "parse_error"
NO_HEADLINES: Final = "no_headlines"
RENDERING_REQUIRED: Final = "rendering_required"
HTTP_ERROR: Final = "http_error"
NETWORK_ERROR: Final = "network_error"
UNSAFE_URL: Final = "unsafe_url"
# Reported by health checks rather than by a read.
STALE: Final = "stale"
DISABLED: Final = "disabled"
SUCCESS_STATES: Final = frozenset({HEALTHY, NOT_MODIFIED})

MAX_BODY_BYTES: Final = 5_000_000
MAX_REDIRECTS: Final = 5
# A page whose HTML yields fewer headlines than this, and looks script-built,
# is reported as needing a browser to render.
RENDERING_THRESHOLD: Final = 3

# Words a navigation link says instead of a headline (compared case-folded,
# whole text only).
NAV_TEXT: Final = frozenset(
    {
        "home",
        "homepage",
        "news",
        "latest",
        "latest news",
        "just in",
        "top stories",
        "subscribe",
        "subscribe now",
        "subscription",
        "log in",
        "login",
        "sign in",
        "sign up",
        "sign out",
        "register",
        "my account",
        "account",
        "contact",
        "contact us",
        "podcast",
        "podcasts",
        "video",
        "videos",
        "watch",
        "listen",
        "live",
        "live tv",
        "search",
        "weather",
        "sport",
        "sports",
        "opinion",
        "about",
        "about us",
        "more",
        "menu",
        "newsletter",
        "newsletters",
        "read more",
        "see more",
        "see all",
        "view all",
        "show more",
        "load more",
        "next",
        "previous",
        "next page",
        "previous page",
        "privacy",
        "privacy policy",
        "terms",
        "terms of use",
        "terms and conditions",
        "cookie settings",
        "cookie policy",
        "cookies",
        "manage cookies",
        "accept",
        "advertise",
        "advertise with us",
        "advertisement",
        "sponsored",
        "help",
        "faq",
        "careers",
        "jobs",
        "shop",
        "puzzles",
        "crosswords",
        "games",
        "skip to content",
        "skip to main content",
        "back to top",
        "share",
        "facebook",
        "twitter",
        "x",
        "instagram",
        "linkedin",
        "youtube",
        "whatsapp",
        "email",
        "print",
        "rss",
        "accessibility",
        "corrections",
        "editorial standards",
    }
)
_NAV_TEXT_RE: Final = re.compile(
    r"^(?:(?:read|see|view|show|load|watch|listen)\s+(?:more|all|now|live)\b|"
    r"\d+\s*comments?$|page\s+\d+$|follow\s+us\b|download\s+the\s+app\b|"
    r"sign\s*up\b|subscribe\s+to\b|(?:listen|watch|play)\b.*\b\d{1,2}:\d\d$)",
    re.IGNORECASE,
)
# Text that is markup or data, not a headline (e.g. JSON-LD inside a link).
_NOT_TEXT_RE: Final = re.compile(r"[{}]|@context|https?://", re.IGNORECASE)
# Parts of a story card that are not its headline.
_LABEL_RE: Final = re.compile(
    r"kicker|label|byline|author|teaser|summary|standfirst|description|excerpt|"
    r"timestamp|time|date|tag|badge|categor|section|premium|icon|count|comment|caption|"
    r"credit|rank|number|eyebrow|flag|overline",
    re.IGNORECASE,
)
_NOT_HEADLINE_TAGS: Final = frozenset(
    {
        "title",
        "svg",
        "time",
        "script",
        "style",
        "noscript",
        "img",
        "picture",
        "source",
        "figcaption",
    }
)
# Files linked from front pages that are not articles.
_FILE_EXT_RE: Final = re.compile(
    r"\.(?:mp3|m4a|mp4|m3u8|pdf|jpe?g|png|gif|webp|svg|zip|xml|rss|json|ics)$", re.IGNORECASE
)
# Index segments that mark a tag, topic or author page wherever they appear.
_INDEX_ANYWHERE: Final = frozenset(
    {
        "tag",
        "tags",
        "topic",
        "topics",
        "author",
        "authors",
        "contributor",
        "contributors",
        "profile",
        "profiles",
        "series",
    }
)
_SLUG_STOPWORDS: Final = frozenset({"and", "the", "of", "a", "in", "on"})
# Containers whose links are chrome, matched on whole class/id tokens.
_CHROME_RE: Final = re.compile(
    r"(?:^|[-_])(?:nav|navbar|navigation|menu|megamenu|breadcrumbs?|footer|social|share|"
    r"sharing|subscribe|subscription|login|signin|account|cookie|cookies|consent|"
    r"pagination|pager|skip|skiplink|masthead|topbar|toolbar|newsletter|advert|"
    r"advertisement|ads?|adslot|sponsored|paywall|offcanvas|drawer)(?:[-_]|$)",
    re.IGNORECASE,
)
_CHROME_TAGS: Final = frozenset({"nav", "footer", "menu", "form", "dialog", "noscript"})
_CHROME_ROLES: Final = frozenset(
    {"navigation", "banner", "contentinfo", "menu", "menubar", "search", "dialog"}
)
# Containers that usually hold one story (layer 4: common publisher patterns).
_STORY_RE: Final = re.compile(
    r"(?:^|[-_])(?:story|stories|card|teaser|headline|promo|post|entry|tile|article|"
    r"item|media|lead|hero|feature|splash)(?:[-_]|$|s\b)",
    re.IGNORECASE,
)
_HEADINGS: Final = frozenset({"h1", "h2", "h3", "h4"})
# First path segments of index, account and policy pages, never articles.
_INDEX_SEGMENTS: Final = frozenset(
    {
        "tag",
        "tags",
        "topic",
        "topics",
        "category",
        "categories",
        "section",
        "sections",
        "author",
        "authors",
        "by",
        "contributor",
        "contributors",
        "people",
        "profile",
        "profiles",
        "search",
        "subscribe",
        "subscription",
        "subscriptions",
        "account",
        "my-account",
        "login",
        "signin",
        "sign-in",
        "register",
        "newsletter",
        "newsletters",
        "about",
        "about-us",
        "contact",
        "contact-us",
        "privacy",
        "privacy-policy",
        "terms",
        "terms-of-use",
        "help",
        "faq",
        "cookies",
        "cookie-policy",
        "advertise",
        "careers",
        "jobs",
        "shop",
        "rss",
        "feeds",
        "weather",
        "puzzles",
        "games",
    }
)
_ARTICLE_EXT_RE: Final = re.compile(r"\.(?:s?html?|stm|ece|php|aspx?)$", re.IGNORECASE)
_DATE_PATH_RE: Final = re.compile(r"/(?:19|20)\d\d(?:[-/](?:\d\d|[a-z]{3}))(?:[-/]\d\d)?(?:/|$)")
_LONG_DIGITS_RE: Final = re.compile(r"\d{5,}")
# An opaque id such as `c8zxl62yzxzxo` (at least two digits, so "signup2" is not one).
_MIXED_ID_RE: Final = re.compile(
    r"^(?=(?:[a-z_-]*\d){2})(?=[a-z0-9_-]*[a-z])[a-z0-9_-]{8,}$", re.IGNORECASE
)

# What the page may say about a story's section, mapped onto a small fixed set.
SECTIONS: Final = {
    "news": "news",
    "world": "world",
    "world news": "world",
    "international": "world",
    "politics": "politics",
    "business": "business",
    "money": "business",
    "economy": "business",
    "markets": "markets",
    "technology": "technology",
    "tech": "technology",
    "science": "science",
    "sport": "sport",
    "sports": "sport",
    "football": "sport",
    "culture": "culture",
    "arts": "culture",
    "entertainment": "culture",
    "opinion": "opinion",
    "comment": "opinion",
    "commentisfree": "opinion",
    "analysis": "analysis",
}
_JSONLD_ARTICLE_TYPES: Final = frozenset(
    {
        "article",
        "newsarticle",
        "reportagenewsarticle",
        "analysisnewsarticle",
        "opinionnewsarticle",
        "backgroundnewsarticle",
        "reviewnewsarticle",
        "blogposting",
        "liveblogposting",
        "socialmediaposting",
    }
)
_JSONLD_SECTION_TYPES: Final = {
    "analysisnewsarticle": "analysis",
    "opinionnewsarticle": "opinion",
}

# Pages served instead of the real one by bot protection.
_CHALLENGE_RE: Final = re.compile(
    r"<title>\s*(?:just a moment|attention required|access denied|pardon our interruption|"
    r"are you a robot|security check|please verify you are a human|request unsuccessful)"
    r"|cf-browser-verification|/cdn-cgi/challenge-platform/|px-captcha|_incapsula_resource"
    r"|captcha-delivery\.com|geo\.captcha-delivery",
    re.IGNORECASE,
)
# Signs that a page builds its content with JavaScript.
_SCRIPT_APP_RE: Final = re.compile(
    r"<noscript[^>]*>[^<]*(?:enable|turn on|requires?)\s+javascript"
    r"|id=[\"'](?:root|app|__next|__nuxt|svelte)[\"'][^>]*>\s*</div>"
    r"|ng-version=|data-reactroot",
    re.IGNORECASE,
)
_SCRIPT_RE: Final = re.compile(r"<script\b", re.IGNORECASE)
_STRIP_RE: Final = re.compile(
    r"<(script|style|noscript|template|svg)\b.*?</\1\s*>|<!--.*?-->", re.IGNORECASE | re.DOTALL
)
_TAGS_RE: Final = re.compile(r"<[^>]+>")


class UnsafeURLError(ValueError):
    """A URL we will not request: not http(s), or resolving to a non-public address."""


# --- Extraction -----------------------------------------------------------------


@dataclass(slots=True)
class Candidate:
    """One headline found on the page, before validation."""

    title: str
    url: str
    layer: str
    published: datetime | None = None
    section: str | None = None
    image: str | None = None
    # Found in a heading (or a heading inside the link): the surest headline text.
    heading: bool = False


@dataclass(slots=True)
class Extraction:
    """What one page yielded."""

    headlines: list[Headline]
    # Every headline-like link found, before dropping duplicates.
    found: int = 0
    duplicates: int = 0
    # How many of the kept headlines each layer found (selectors, article,
    # heading, pattern, link, json-ld, microdata).
    layers: dict[str, int] = field(default_factory=dict)
    rendering_required: bool = False
    canonical_url: str | None = None
    note: str | None = None

    @property
    def method(self) -> str:
        """The layers that produced the headlines, most productive first."""
        ordered = sorted(self.layers.items(), key=lambda item: (-item[1], item[0]))
        return "+".join(name for name, _ in ordered) or "none"


def _classes(node: Node) -> str:
    return f"{node.attr('class') or ''} {node.attr('id') or ''}"


def _tokens_match(pattern: re.Pattern[str], text: str) -> bool:
    return any(pattern.search(token) for token in text.split())


def _ancestors(node: Node, limit: int = 14) -> Iterator[Node]:
    current = node.parent()
    for _ in range(limit):
        if current is None or current.tag() in {"body", "html", "-undef", "#document", ""}:
            return
        yield current
        current = current.parent()


def _same_site(url: str, page_url: str) -> bool:
    return site_of(urlsplit(url).hostname or "") == site_of(urlsplit(page_url).hostname or "")


def url_score(url: str) -> int:
    """How much `url` looks like an article: below 0 means an index or policy page."""
    path = urlsplit(url).path
    segments = [segment for segment in path.split("/") if segment]
    if not segments or _FILE_EXT_RE.search(path):
        return -1
    last = segments[-1]
    words = [word for word in re.split(r"[-_]+", _ARTICLE_EXT_RE.sub("", last)) if word]
    # A date in the path, a long number or a mixed letters-and-digits id.
    has_id = bool(
        _DATE_PATH_RE.search(path)
        or _LONG_DIGITS_RE.search(path)
        or any(_MIXED_ID_RE.match(segment) for segment in segments)
    )
    lowered = [segment.lower() for segment in segments]
    if lowered[-1] in _INDEX_SEGMENTS or lowered[-1] in _INDEX_ANYWHERE:
        return -1
    if lowered[0] in _INDEX_ANYWHERE:
        return -1
    if lowered[0] in _INDEX_SEGMENTS:
        # Some sites keep articles under /category/<section>/<slug>: only a
        # long slug or an id makes such a path an article.
        if len(segments) <= 2 or not (has_id or len(words) >= 6):
            return -1
    elif any(segment in _INDEX_ANYWHERE for segment in lowered[1:-1]):
        # /news/topics/c2vdnvdg6xxt is a topic page, id or not.
        return -1
    score = 0
    if len(words) >= 3:
        score += 2
    if has_id:
        score += 2
    if _ARTICLE_EXT_RE.search(last):
        score += 1
    if len(segments) >= 2:
        score += 1
    if score == 0 or (score == 1 and len(segments) <= 2):
        # `/sport`, `/news/world`: a section front, not a story.
        return -1
    return score


def names_its_url(title: str, url: str) -> bool:
    """True for a short link text that just spells out its URL's last segment.

    "Health & Families" -> /life-style/health-and-families is a section link.
    """
    words = [word for word in title_key(title).split() if word not in _SLUG_STOPWORDS]
    if not words or len(words) > 4:
        return False
    segments = [segment for segment in urlsplit(url).path.split("/") if segment]
    if not segments:
        return False
    slug = [
        word
        for word in re.split(r"[-_]+", segments[-1].lower())
        if word and word not in _SLUG_STOPWORDS
    ]
    return slug == words


def is_nav_text(text: str) -> bool:
    folded = text.casefold().strip(" .:|›»>-–—")  # noqa: RUF001
    return folded in NAV_TEXT or bool(_NAV_TEXT_RE.match(folded))


def map_section(value: str | None) -> str | None:
    """A section the page states, mapped onto `SECTIONS`; "other" when unmapped."""
    text = clean_text(value).casefold()
    if not text:
        return None
    return SECTIONS.get(text, "other")


def _section_from_url(url: str) -> str | None:
    segments = [segment.lower() for segment in urlsplit(url).path.split("/") if segment]
    # Only a leading path segment naming a known section counts as evidence.
    return SECTIONS.get(segments[0]) if len(segments) >= 2 else None


def _image_of(node: Node, page_url: str, selector: str = "img") -> str | None:
    image = node.select_one(selector)
    if image is None:
        return None
    if image.tag() != "img":
        image = image.select_one("img") or image
    src = image.attr("src") or image.attr("data-src")
    if not src:
        srcset = image.attr("srcset") or image.attr("data-srcset") or ""
        src = srcset.split(",")[0].strip().split(" ")[0] if srcset else None
    if not src or src.startswith("data:"):
        return None
    resolved = urljoin(page_url, src.strip())
    return resolved if urlsplit(resolved).scheme in {"http", "https"} else None


def _time_of(node: Node, selector: str = "time") -> datetime | None:
    target = node.select_one(selector)
    if target is None:
        return None
    return parse_datetime(target.attr("datetime") or target.attr("content") or target.text())


def _href(node: Node, page_url: str) -> str | None:
    href = (node.attr("href") or node.attr("data-href") or "").strip()
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
        return None
    resolved = urljoin(page_url, href)
    return resolved if urlsplit(resolved).scheme in {"http", "https"} else None


def _without_labels(node: Node) -> str:
    """`node`'s text minus kickers, labels, bylines and icon titles inside it."""
    text = clean_text(node.text())
    for part in descendants(node, "*"):
        if part.tag() in _NOT_HEADLINE_TAGS or _tokens_match(_LABEL_RE, _classes(part)):
            piece = clean_text(part.text())
            # A plain label goes whatever its length; a wrapper whose class
            # happens to match must not swallow the headline inside it.
            wrapper = bool(descendants(part, "*"))
            if piece and (not wrapper or len(piece) < len(text) * 0.5):
                text = text.replace(piece, " ", 1)
    return clean_text(text)


def _longest_line(node: Node) -> str:
    """The longest piece of text directly inside one element that is not a label."""
    best = ""
    for part in descendants(node, "*"):
        if part.tag() in _NOT_HEADLINE_TAGS or _tokens_match(_LABEL_RE, _classes(part)):
            continue
        if any(
            ancestor.tag() in _NOT_HEADLINE_TAGS or _tokens_match(_LABEL_RE, _classes(ancestor))
            for ancestor in _ancestors(part, 4)
        ):
            continue
        own = clean_text(part.own_text())
        if len(own) > len(best):
            best = own
    return best


_HEADING_SELECTOR: Final = "h1, h2, h3, h4, [class*=headline], [class*=Headline]"
# In a card around an empty overlay link, a "title" element is the headline too.
_CARD_HEADING_SELECTOR: Final = (
    f"{_HEADING_SELECTOR}, [class*=title]:not([class*=subtitle]), [class*=Title]"
)


def _headline_of(node: Node) -> str:
    """The headline inside a link: its heading, else its text, without kickers and labels.

    Cards often wrap a kicker ("EXCLUSIVE", "World"), labels, a teaser and a
    byline around the headline. When what is left is still long, the longest
    single line of text is taken.
    """
    inner = next(iter(descendants(node, _HEADING_SELECTOR)), None)
    if inner is not None:
        text = _without_labels(inner)
        if len(text) >= MIN_TITLE_LENGTH:
            return text
    text = _without_labels(node)
    if len(text) > 160:
        line = _longest_line(node)
        if len(line) >= MIN_TITLE_LENGTH:
            return line
    return text


def _anchor_title(anchor: Node) -> str:
    """The headline a link carries; for an empty "overlay" link, its card's heading."""
    text = _headline_of(anchor)
    if len(text) >= MIN_TITLE_LENGTH:
        return text
    named = clean_text(anchor.attr("title") or anchor.attr("aria-label"))
    if len(named) >= MIN_TITLE_LENGTH:
        return named
    # Overlay links cover a whole card and say nothing themselves: use the
    # card's heading, if the card has exactly one.
    for ancestor in _ancestors(anchor, 3):
        texts = [
            _without_labels(heading) for heading in descendants(ancestor, _CARD_HEADING_SELECTOR)
        ]
        texts = [found for found in texts if found]
        if not texts:
            continue
        # One headline (perhaps nested in another heading element), not a list.
        card = max(texts, key=len)
        if all(found in card for found in texts) and MIN_TITLE_LENGTH <= len(card) <= 250:
            return card
        break
    return text


def _from_selectors(tree: Node, page: FrontPage, page_url: str) -> list[Candidate]:
    """Layer 1: the source's own selectors."""
    assert page.article_selector is not None
    found: list[Candidate] = []
    for container in tree.select_all(page.article_selector):
        link_node = (
            container.select_one(page.link_selector)
            if page.link_selector
            else (container if container.tag() == "a" else container.select_one("a[href]"))
        )
        if link_node is not None and link_node.tag() != "a" and not link_node.attr("href"):
            link_node = link_node.select_one("a[href]") or link_node
        url = _href(link_node, page_url) if link_node is not None else None
        if page.title_selector:
            title = clean_text(_text_of(container.select_one(page.title_selector)))
        else:
            heading = container.select_one("h1, h2, h3, h4")
            title = clean_text(heading.text()) if heading is not None else ""
            if not title and link_node is not None:
                title = _anchor_title(link_node)
        if not url or not title:
            continue
        found.append(
            Candidate(
                title=title,
                url=url,
                layer="selectors",
                published=_time_of(container, page.published_selector or "time"),
                section=map_section(_text_of(container.select_one(page.section_selector)))
                if page.section_selector
                else None,
                image=_image_of(container, page_url, page.image_selector or "img"),
            )
        )
    return found


def _text_of(node: Node | None) -> str:
    return node.text() if node is not None else ""


def _from_links(tree: Node, page_url: str) -> list[Candidate]:
    """Layers 2, 4 and 5: every link, scored on its place in the page and its URL."""
    page_key = match_key(page_url)
    found: list[Candidate] = []
    for anchor in tree.select_all("a[href]"):
        url = _href(anchor, page_url)
        if url is None or not _same_site(url, page_url) or match_key(url) == page_key:
            continue
        if "comment" in urlsplit(url).fragment.lower():
            continue  # "12 comments" jump links
        score_url = url_score(url)
        if score_url < 0:
            continue
        in_article = in_heading = storyish = False
        chrome = False
        container: Node | None = None
        if anchor.select_one("h1, h2, h3, h4") is not None:
            in_heading = True
        lineage = list(_ancestors(anchor))
        tags = [ancestor.tag() for ancestor in lineage]
        for depth, ancestor in enumerate(lineage):
            tag = tags[depth]
            role = (ancestor.attr("role") or "").lower()
            if tag in _CHROME_TAGS or role in _CHROME_ROLES:
                chrome = True
                break
            # A page's <header> is its masthead; an article's <header> holds its headline.
            if tag == "header" and "article" not in tags[depth + 1 :]:
                chrome = True
                break
            if tag == "aside" and _tokens_match(_CHROME_RE, _classes(ancestor)):
                chrome = True
                break
            classes = _classes(ancestor)
            if _tokens_match(_CHROME_RE, classes):
                chrome = True
                break
            if tag in _HEADINGS and depth <= 2:
                in_heading = True
            if tag == "article" or "article" in (ancestor.attr("itemtype") or "").lower():
                in_article = True
                container = container or ancestor
            elif depth <= 4 and _tokens_match(_STORY_RE, classes):
                storyish = True
                container = container or ancestor
        if chrome:
            continue
        title = _anchor_title(anchor)
        words = len(title.split())
        if (
            len(title) < MIN_TITLE_LENGTH
            or words < 3
            or len(title) > 250
            or is_nav_text(title)
            or _NOT_TEXT_RE.search(title)
            or names_its_url(title, url)
        ):
            continue
        context = 2 * in_article + 2 * in_heading + storyish
        if not ((score_url >= 2 and words >= 4) or (context >= 2 and score_url >= 1)):
            continue
        layer = (
            "article"
            if in_article
            else "heading"
            if in_heading
            else "pattern"
            if storyish
            else "link"
        )
        holder = container or anchor
        found.append(
            Candidate(
                title=title,
                url=url,
                layer=layer,
                published=_time_of(holder),
                section=_section_from_url(url),
                image=_image_of(holder, page_url),
                heading=in_heading,
            )
        )
    return found


def _jsonld_objects(tree: Node) -> Iterator[dict[str, Any]]:
    for script in tree.select_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.text())
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            item = stack.pop(0)
            if isinstance(item, list):
                stack[:0] = item
            elif isinstance(item, dict):
                yield item
                for key in ("@graph", "itemListElement", "mainEntity", "hasPart", "item"):
                    if key in item:
                        stack.insert(0, item[key])


def _jsonld_types(item: dict[str, Any]) -> list[str]:
    kind = item.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return [str(value).lower() for value in kinds if value]


def _jsonld_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _jsonld_url(value.get("@id") or value.get("url"))
    if isinstance(value, list) and value:
        return _jsonld_url(value[0])
    return None


def _from_jsonld(tree: Node, page_url: str) -> list[Candidate]:
    """Layer 3: articles described as JSON-LD."""
    found: list[Candidate] = []
    for item in _jsonld_objects(tree):
        types = _jsonld_types(item)
        if not any(kind in _JSONLD_ARTICLE_TYPES for kind in types):
            continue
        title = clean_text(str(item.get("headline") or item.get("name") or ""))
        raw_url = _jsonld_url(item.get("url")) or _jsonld_url(item.get("mainEntityOfPage"))
        if not title or not raw_url:
            continue
        url = urljoin(page_url, raw_url)
        if urlsplit(url).scheme not in {"http", "https"} or match_key(url) == match_key(page_url):
            continue
        section = item.get("articleSection")
        if isinstance(section, list):
            section = section[0] if section else None
        image = _jsonld_url(item.get("image")) or (
            item["image"].get("url") if isinstance(item.get("image"), dict) else None
        )
        found.append(
            Candidate(
                title=title,
                url=url,
                layer="json-ld",
                published=parse_datetime(str(item.get("datePublished") or "")),
                section=map_section(str(section)) if section else None,
                image=urljoin(page_url, image) if isinstance(image, str) else None,
            )
        )
        for kind in types:
            if kind in _JSONLD_SECTION_TYPES and found[-1].section is None:
                found[-1].section = _JSONLD_SECTION_TYPES[kind]
    return found


def _from_microdata(tree: Node, page_url: str) -> list[Candidate]:
    """Layer 3: schema.org microdata (`itemtype=".../NewsArticle"`)."""
    found: list[Candidate] = []
    for item in tree.select_all("[itemscope][itemtype]"):
        kind = (item.attr("itemtype") or "").rstrip("/").rsplit("/", 1)[-1].lower()
        if kind not in _JSONLD_ARTICLE_TYPES:
            continue
        heading = item.select_one('[itemprop="headline"], [itemprop="name"]')
        title = clean_text(heading.attr("content") or heading.text()) if heading else ""
        link = item.select_one('[itemprop="url"], [itemprop="mainEntityOfPage"]')
        raw = (link.attr("href") or link.attr("content")) if link is not None else None
        if raw is None:
            anchor = item.select_one("a[href]")
            raw = anchor.attr("href") if anchor is not None else None
        if not title or not raw:
            continue
        url = urljoin(page_url, raw.strip())
        if urlsplit(url).scheme not in {"http", "https"}:
            continue
        date = item.select_one('[itemprop="datePublished"]')
        section = item.select_one('[itemprop="articleSection"]')
        found.append(
            Candidate(
                title=title,
                url=url,
                layer="microdata",
                published=parse_datetime(date.attr("datetime") or date.attr("content") or "")
                if date is not None
                else None,
                section=map_section(section.attr("content") or section.text())
                if section is not None
                else None,
                image=_image_of(item, page_url),
            )
        )
    return found


def _enrich(target: Candidate, extra: Candidate) -> None:
    target.published = target.published or extra.published
    target.section = target.section or extra.section
    target.image = target.image or extra.image


def _page_key(url: str) -> str:
    """`match_key`, minus the query when the path alone is clearly one article.

    Live blogs link each update as `?update=123`: one story on the page.
    """
    key = match_key(url)
    if "?" in key and url_score(url.split("?", 1)[0]) >= 2:
        return key.split("?", 1)[0]
    return key


def _dedupe(candidates: Iterable[Candidate]) -> tuple[list[Candidate], int, int]:
    """Keep each story once, at its first (highest) position.

    The same story is the same URL (by `match_key`) or the same headline words
    (by `title_key`). Similar but different headlines are kept apart.
    """
    kept: list[Candidate] = []
    by_url: dict[str, Candidate] = {}
    by_title: dict[str, Candidate] = {}
    total = duplicates = 0
    for candidate in candidates:
        total += 1
        url_k, title_k = _page_key(candidate.url), title_key(candidate.title)
        same = by_url.get(url_k) or by_title.get(title_k)
        if same is not None:
            duplicates += 1
            _enrich(same, candidate)
            # Cards often link one story twice, from its picture or summary and
            # from its heading: the heading's text is the headline.
            if url_k == _page_key(same.url) and candidate.heading and not same.heading:
                by_title.pop(title_key(same.title), None)
                same.title = candidate.title
                same.heading = True
                by_title.setdefault(title_key(same.title), same)
            continue
        kept.append(candidate)
        by_url[url_k] = candidate
        by_title.setdefault(title_k, candidate)
    return kept, total, duplicates


def looks_script_built(html: str) -> bool:
    """True when the page's HTML is mostly scripts and an empty app root."""
    visible = clean_text(_TAGS_RE.sub(" ", _STRIP_RE.sub(" ", html)))
    scripts = len(_SCRIPT_RE.findall(html))
    return bool(_SCRIPT_APP_RE.search(html)) or (len(visible) < 1500 and scripts >= 3)


def is_challenge(html: str) -> bool:
    """True for a bot-protection page served in place of the real one."""
    return bool(_CHALLENGE_RE.search(html[:200_000]))


def page_canonical(tree: Node, page_url: str) -> str | None:
    """The URL a page names as its own: `<link rel=canonical>`, else `og:url`."""
    for link in tree.select_all("link[rel][href]"):
        if "canonical" in (link.attr("rel") or "").lower().split():
            return urljoin(page_url, (link.attr("href") or "").strip())
    meta = tree.select_one('meta[property="og:url"][content]')
    if meta is not None and meta.attr("content"):
        return urljoin(page_url, (meta.attr("content") or "").strip())
    return None


def extract(
    body: bytes | str,
    source: Source,
    *,
    page_url: str | None = None,
    fetched_at: datetime | None = None,
    limit: int | None = None,
) -> Extraction:
    """Headlines on a front page, in page order (position 1 is the first on the page).

    `page_url` is where the page was finally fetched from (after redirects);
    relative links resolve against it, or against the page's `<base href>`.
    """
    page = source.front_page or FrontPage(url=source.url)
    html = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    tree = build_tree(html)
    base_url = page_url or page.url or source.url
    base = tree.select_one("base[href]")
    if base is not None and base.attr("href"):
        base_url = urljoin(base_url, (base.attr("href") or "").strip())
    stamp = fetched_at or utcnow()

    note = None
    dom: list[Candidate] = []
    if page.article_selector:
        dom = _from_selectors(tree, page, base_url)
        if not dom:
            note = "configured selectors matched nothing; used generic extraction"
    if not dom:
        dom = _from_links(tree, base_url)

    metadata = _from_jsonld(tree, base_url) + _from_microdata(tree, base_url)
    by_key = {match_key(candidate.url): candidate for candidate in dom}
    extra: list[Candidate] = []
    for item in metadata:
        same = by_key.get(match_key(item.url))
        if same is not None:
            _enrich(same, item)
        elif _same_site(item.url, base_url) and not is_nav_text(item.title):
            extra.append(item)

    kept, total, duplicates = _dedupe([*dom, *extra])
    include = source.include_regex
    headlines: list[Headline] = []
    layers: dict[str, int] = {}
    for candidate in kept:
        if include is not None and not include.search(candidate.url):
            continue
        try:
            headline = Headline.create(
                source=source.name,
                title=candidate.title,
                url=candidate.url,
                published_at=candidate.published,
                fetched_at=stamp,
                live_url_pattern=source.live_regex,
                acquisition=(METHOD,),
                front_page_position=len(headlines) + 1,
                section=candidate.section,
                image_url=candidate.image,
            )
        except InvalidHeadlineError as exc:
            logger.debug("%s: skipping front-page link: %s", source.name, exc)
            continue
        headlines.append(headline)
        layers[candidate.layer] = layers.get(candidate.layer, 0) + 1
        if limit is not None and len(headlines) >= limit:
            break

    return Extraction(
        headlines=headlines,
        found=total,
        duplicates=duplicates,
        layers=layers,
        rendering_required=len(headlines) < RENDERING_THRESHOLD and looks_script_built(html),
        canonical_url=page_canonical(tree, base_url),
        note=note,
    )


# --- Fetching -------------------------------------------------------------------


def _addresses(host: str) -> list[str]:
    """Every address `host` resolves to (tests replace this)."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


async def check_url_safe(url: str) -> None:
    """Raise `UnsafeURLError` unless `url` is http(s) on a public address.

    Stops a page (or a redirect) steering requests at this machine or the
    local network. Hosts are resolved once here; the request resolves again,
    so a host that answers differently each time is not fully covered.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() not in {"http", "https"}:
        raise UnsafeURLError(f"not an http(s) URL: {url}")
    host = parts.hostname
    if not host:
        raise UnsafeURLError(f"no host name: {url}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            found = await asyncio.to_thread(_addresses, host)
        except OSError:
            return  # unresolvable: the request itself will fail as a network error
        addresses = [ipaddress.ip_address(value.split("%")[0]) for value in found]
    else:
        addresses = [literal]
    for address in addresses:
        if not address.is_global:
            raise UnsafeURLError(f"{host} resolves to a non-public address ({address})")


@dataclass(slots=True)
class CacheEntry:
    """Validators from the last successful read, for a conditional request."""

    url: str
    etag: str | None = None
    last_modified: str | None = None


@dataclass(slots=True)
class FrontPageResult:
    """One read of one front page. Never raises upward: see `status`."""

    source: str
    url: str
    status: str
    started_at: datetime
    finished_at: datetime
    http_status: int | None = None
    final_url: str | None = None
    response_ms: int | None = None
    error: str | None = None
    headlines: list[Headline] = field(default_factory=list)
    found: int = 0
    duplicates: int = 0
    method: str | None = None
    rendering_required: bool = False
    etag: str | None = None
    last_modified: str | None = None
    canonical_url: str | None = None
    # Filled in when stored (see `store.store_front_page`).
    items_new: int = 0
    items_merged: int = 0
    items_merged_feed: int = 0

    @property
    def ok(self) -> bool:
        return self.status in SUCCESS_STATES


@dataclass(slots=True)
class _Page:
    status: int
    url: str
    headers: httpx.Headers
    body: bytes
    elapsed_ms: int


async def _get_page(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout: float,
    headers: dict[str, str],
    robots: RobotsCache | None,
) -> _Page:
    """GET `url`, following redirects one checked hop at a time."""
    started = time.monotonic()
    current = url
    origin = urlsplit(url)[:2]
    for _ in range(MAX_REDIRECTS + 1):
        await check_url_safe(current)
        # A redirect to another origin is checked against that origin's robots.txt.
        moved = urlsplit(current)[:2] != origin
        if robots is not None and moved and not await robots.can_fetch(current):
            raise _RobotsDenied(current)
        async with client.stream(
            "GET", current, headers=headers, timeout=timeout, follow_redirects=False
        ) as response:
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            chunks: list[bytes] = []
            size = 0
            if response.status_code < 300:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BODY_BYTES:
                        raise _TooLarge(size)
                    chunks.append(chunk)
            return _Page(
                status=response.status_code,
                url=current,
                headers=response.headers,
                body=b"".join(chunks),
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
    raise httpx.TooManyRedirects(f"more than {MAX_REDIRECTS} redirects")


class _RobotsDenied(Exception):
    pass


class _TooLarge(Exception):
    pass


def _domain(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


async def fetch_front_page(
    client: httpx.AsyncClient,
    source: Source,
    settings: Settings,
    *,
    limiter: RateLimiter,
    robots: RobotsCache | None,
    cache: CacheEntry | None = None,
    url: str | None = None,
) -> FrontPageResult:
    """Read and extract one source's front page. Failures become a status, not an exception.

    `limiter` is the run's `fetcher.RateLimiter` and `robots` its
    `RobotsCache` (None only when robots.txt is deliberately ignored).
    """
    target = url or source.front_page_url or source.url
    started_at = utcnow()
    result = FrontPageResult(
        source=source.name,
        url=target,
        status=HEALTHY,
        started_at=started_at,
        finished_at=started_at,
    )

    def done(status: str, error: str | None = None) -> FrontPageResult:
        result.status = status
        result.error = error
        result.finished_at = utcnow()
        _log(result)
        return result

    try:
        await check_url_safe(target)
        crawl_delay = None
        if robots is not None:
            if not await robots.can_fetch(target):
                return done(ROBOTS_DENIED, f"robots.txt disallows {target}")
            crawl_delay = await robots.crawl_delay(target)
        headers = {"Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
        if cache is not None and cache.url == target:
            if cache.etag:
                headers["If-None-Match"] = cache.etag
            if cache.last_modified:
                headers["If-Modified-Since"] = cache.last_modified
        domain = _domain(target)
        page: _Page | None = None
        for attempt in (1, 2):
            async with limiter.hold(domain):
                await limiter.acquire(domain, min_gap=crawl_delay)
                page = await _get_page(
                    client,
                    target,
                    timeout=settings.request_timeout,
                    headers=headers,
                    robots=robots,
                )
            # One polite retry for a server error; never for 429 or a block.
            if page.status in {500, 502, 504} and attempt == 1:
                await asyncio.sleep(min(5.0, settings.rate_limit_seconds * 2))
                continue
            break
        assert page is not None
    except UnsafeURLError as exc:
        return done(UNSAFE_URL, str(exc))
    except _RobotsDenied as exc:
        return done(ROBOTS_DENIED, f"robots.txt disallows the redirect to {exc}")
    except _TooLarge as exc:
        return done(PARSE_ERROR, f"page larger than {MAX_BODY_BYTES:,} bytes ({exc} read)")
    except httpx.TimeoutException as exc:
        return done(TIMEOUT, f"timed out ({type(exc).__name__})")
    except httpx.TooManyRedirects as exc:
        return done(HTTP_ERROR, str(exc))
    except httpx.HTTPError as exc:
        reason = str(exc) or type(exc).__name__
        if "ssl" in reason.lower() or "certificate" in reason.lower():
            return done(NETWORK_ERROR, f"TLS error: {reason}")
        return done(NETWORK_ERROR, f"{type(exc).__name__}: {reason}")
    except Exception as exc:  # noqa: BLE001 - one bad page must not abort the run
        return done(NETWORK_ERROR, f"unexpected {type(exc).__name__}: {exc}")

    result.http_status = page.status
    result.final_url = page.url
    result.response_ms = page.elapsed_ms
    if page.status == 304:
        result.etag = cache.etag if cache else None
        result.last_modified = cache.last_modified if cache else None
        return done(NOT_MODIFIED)
    text = page.body.decode("utf-8", errors="replace")
    challenged = page.headers.get("cf-mitigated", "").lower() == "challenge" or is_challenge(text)
    if page.status in {401, 403, 429, 451} or (page.status == 503 and challenged):
        return done(BLOCKED, f"HTTP {page.status}" + (" (bot challenge)" if challenged else ""))
    if page.status >= 400:
        return done(HTTP_ERROR, f"HTTP {page.status}")
    if challenged:
        return done(BLOCKED, f"HTTP {page.status} bot challenge page")
    kind = page.headers.get("content-type", "").split(";")[0].strip().lower()
    if kind and "html" not in kind and "xml" not in kind:
        return done(PARSE_ERROR, f"not an HTML page ({kind})")
    try:
        extraction = extract(
            page.body,
            source,
            page_url=page.url,
            fetched_at=utcnow(),
            limit=settings.max_items_per_source,
        )
    except (ParseError, ValueError, RecursionError) as exc:
        return done(PARSE_ERROR, f"{type(exc).__name__}: {exc}")
    result.headlines = extraction.headlines
    result.found = extraction.found
    result.duplicates = extraction.duplicates
    result.method = extraction.method
    result.rendering_required = extraction.rendering_required
    result.canonical_url = extraction.canonical_url
    result.etag = page.headers.get("etag")
    result.last_modified = page.headers.get("last-modified")
    if not extraction.headlines:
        if extraction.rendering_required:
            return done(
                RENDERING_REQUIRED, "the page builds its headlines with JavaScript; not rendered"
            )
        return done(NO_HEADLINES, extraction.note or "no headline-like links found")
    return done(HEALTHY, extraction.note)


def _log(result: FrontPageResult) -> None:
    level = logging.INFO if result.ok else logging.WARNING
    parts = [f'source="{result.source}"', f'method="{METHOD}"', f'status="{result.status}"']
    if result.http_status is not None:
        parts.append(f"http={result.http_status}")
    if result.ok:
        parts.append(f"headlines={len(result.headlines)}")
    if result.response_ms is not None:
        parts.append(f"ms={result.response_ms}")
    if result.error and not result.ok:
        parts.append(f'error="{result.error}"')
    logger.log(level, " ".join(parts))


# --- Health ---------------------------------------------------------------------

# Two missed runs (6 h apart) and some slack, as for feeds.
STALE_AFTER: Final = timedelta(hours=13)


def health(
    page: FrontPage | None,
    last_status: str | None,
    last_success: datetime | None,
    now: datetime,
) -> str | None:
    """A source's front-page state, or None when it has no front page configured."""
    if page is None:
        return None
    if not page.enabled:
        return DISABLED
    if last_status is None:
        return "never read"
    if last_status in SUCCESS_STATES:
        if last_success is not None and now - last_success > STALE_AFTER:
            return STALE
        return HEALTHY
    return last_status


# --- Validation -----------------------------------------------------------------

MIN_VALID_HEADLINES: Final = 5
MIN_MEAN_TITLE_LENGTH: Final = 20
MAX_DUPLICATE_RATE: Final = 0.8


@dataclass(slots=True)
class Validation:
    """Whether a front page is worth enabling, with the numbers behind the verdict."""

    result: FrontPageResult
    followed: int = 0
    accessible: int = 0
    canonical_matches: int = 0
    follow_errors: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def headline_count(self) -> int:
        return self.result.found

    @property
    def unique_headline_count(self) -> int:
        return len(self.result.headlines)

    @property
    def valid_article_url_count(self) -> int:
        return sum(
            1
            for headline in self.result.headlines
            if urlsplit(headline.url).scheme in {"http", "https"} and url_score(headline.url) > 0
        )

    @property
    def mean_headline_length(self) -> float:
        titles = [len(headline.title) for headline in self.result.headlines]
        return sum(titles) / len(titles) if titles else 0.0

    @property
    def duplicate_rate(self) -> float:
        return self.result.duplicates / self.result.found if self.result.found else 0.0

    @property
    def valid(self) -> bool:
        return not self.reasons

    def as_dict(self) -> dict[str, Any]:
        result = self.result
        return {
            "source": result.source,
            "url": result.url,
            "final_url": result.final_url,
            "status": result.status,
            "http_status": result.http_status,
            "error": result.error,
            "valid": self.valid,
            "reasons": self.reasons,
            "front_page_response_time": result.response_ms,
            "method": result.method,
            "rendering_required": result.rendering_required,
            "headline_count": self.headline_count,
            "unique_headline_count": self.unique_headline_count,
            "valid_article_url_count": self.valid_article_url_count,
            "followed_articles": self.followed,
            "accessible_article_count": self.accessible,
            "canonical_matches": self.canonical_matches,
            "follow_errors": self.follow_errors,
            "mean_headline_length": round(self.mean_headline_length, 1),
            "duplicate_rate": round(self.duplicate_rate, 3),
            "headlines": [
                {
                    "position": headline.front_page_position,
                    "title": headline.title,
                    "url": headline.url,
                    "section": headline.section,
                }
                for headline in result.headlines
            ],
        }


def judge(validation: Validation, *, follow: int) -> None:
    """Fill `validation.reasons` with every criterion the page fails."""
    result = validation.result
    reasons = validation.reasons
    if not result.ok:
        reasons.append(f"{result.status}: {result.error or 'no detail'}")
        return
    count = validation.unique_headline_count
    if count < MIN_VALID_HEADLINES:
        reasons.append(f"only {count} headline(s); want at least {MIN_VALID_HEADLINES}")
    if count and validation.valid_article_url_count < count * 0.8:
        reasons.append(
            f"{count - validation.valid_article_url_count} of {count} links do not look like "
            "article URLs"
        )
    if count and validation.mean_headline_length < MIN_MEAN_TITLE_LENGTH:
        reasons.append(f"headlines average {validation.mean_headline_length:.0f} characters")
    if validation.duplicate_rate > MAX_DUPLICATE_RATE:
        reasons.append(f"{validation.duplicate_rate:.0%} of links were duplicates")
    if follow and count and validation.accessible == 0:
        reasons.append("no article link could be followed")


async def follow_articles(
    client: httpx.AsyncClient,
    headlines: Sequence[Headline],
    settings: Settings,
    *,
    limiter: RateLimiter,
    robots: RobotsCache | None,
    count: int,
    validation: Validation,
) -> None:
    """Open the first `count` articles: are they reachable, and is their canonical URL ours?"""
    for headline in headlines[:count]:
        validation.followed += 1
        try:
            if robots is not None and not await robots.can_fetch(headline.url):
                validation.follow_errors.append(f"{headline.url}: robots.txt disallows")
                continue
            domain = _domain(headline.url)
            async with limiter.hold(domain):
                await limiter.acquire(domain)
                page = await _get_page(
                    client,
                    headline.url,
                    timeout=settings.request_timeout,
                    headers={"Accept": "text/html,*/*;q=0.5"},
                    robots=robots,
                )
        except (httpx.HTTPError, UnsafeURLError, _RobotsDenied, _TooLarge) as exc:
            validation.follow_errors.append(f"{headline.url}: {type(exc).__name__}: {exc}")
            continue
        if page.status >= 400:
            validation.follow_errors.append(f"{headline.url}: HTTP {page.status}")
            continue
        validation.accessible += 1
        try:
            canonical = page_canonical(build_tree(page.body), page.url)
        except ParseError:
            canonical = None
        if canonical and match_key(canonical) == match_key(headline.url):
            validation.canonical_matches += 1
