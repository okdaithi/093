"""Core data shapes: the `Headline` record plus text and URL normalisation."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Titles shorter than this are almost always navigation chrome ("More", "Video")
# rather than a real headline.
MIN_TITLE_LENGTH = 10

_WHITESPACE_RE = re.compile(r"\s+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")

# Non-breaking space -> plain space; zero-width space -> dropped.
_ODD_SPACE_MAP = {ord("\u00a0"): " ", ord("\u200b"): None}

# Tracking parameters that vary per visit and would otherwise defeat dedup.
_TRACKING_PARAMS = frozenset(
    {
        "cmpid",
        "fbclid",
        "ftag",
        "gclid",
        "icid",
        "igshid",
        "mbid",
        "mc_cid",
        "mc_eid",
        "ncid",
        "ocid",
        "partner",
        "smid",
        "srnd",
        "taid",
        "utm_brand",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_name",
        "utm_source",
        "utm_term",
    }
)
# Whole families of tracking parameters. `at_` is AT Internet / Piano Analytics,
# which the BBC appends to every feed link (`at_campaign=rss&at_medium=RSS`).
_TRACKING_PREFIXES = ("at_", "utm_")


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def clean_text(value: str | None) -> str:
    """Unescape entities, drop markup and control chars, collapse whitespace."""
    if not value:
        return ""
    text = _TAG_RE.sub(" ", value)
    # Twice: feeds routinely double-escape (`&amp;amp;` -> `&amp;` -> `&`).
    text = html.unescape(html.unescape(text))
    text = text.translate(_ODD_SPACE_MAP)
    text = _CONTROL_RE.sub("", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def normalise_url(url: str, *, drop_query: bool = False) -> str:
    """Canonicalise a URL so the same article hashes identically across runs.

    Lowercases scheme and host, strips a default port, removes the fragment and
    known tracking parameters, and trims a trailing slash from non-root paths.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    netloc = host.lower()
    if parts.port and not (
        (scheme == "http" and parts.port == 80) or (scheme == "https" and parts.port == 443)
    ):
        netloc = f"{netloc}:{parts.port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    query = ""
    if parts.query and not drop_query:
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking_param(key)
        ]
        query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


# Live blogs re-headline on every update. Only a whole `live`/`liveblog` path
# segment or a "… live:" style title counts, so a story about how to "live in
# harmony" is not a live blog.
_LIVE_PATH_RE = re.compile(r"/(?:live|liveblog)(?:/|$)", re.IGNORECASE)
_LIVE_TITLE_RE = re.compile(r"(?:^|\s)live(?:\s+(?:updates|blog))?\s*[:|–—]", re.IGNORECASE)  # noqa: RUF001


def looks_live(url: str, title: str, extra_url_pattern: re.Pattern[str] | None = None) -> bool:
    """True when the URL or title marks a live blog.

    `extra_url_pattern` is a source's own `live_url_pattern`, searched in the
    full URL in addition to the built-in rules.
    """
    return bool(
        _LIVE_PATH_RE.search(urlsplit(url).path)
        or _LIVE_TITLE_RE.search(title)
        or (extra_url_pattern is not None and extra_url_pattern.search(url))
    )


def compute_hash(url: str, title: str) -> str:
    """sha256 over the normalised url and title — the dedup key."""
    payload = f"{normalise_url(url)}\n{clean_text(title).casefold()}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def to_utc(value: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC; naive input is assumed UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


class InvalidHeadlineError(ValueError):
    """Raised when a parsed row cannot become a usable `Headline`."""


@dataclass(frozen=True, slots=True)
class Headline:
    """One normalised headline. Construct via `Headline.create`."""

    source: str
    title: str
    url: str
    published_at: datetime | None
    fetched_at: datetime
    summary: str | None
    content_hash: str
    is_live: bool = False

    @classmethod
    def create(
        cls,
        *,
        source: str,
        title: str | None,
        url: str | None,
        published_at: datetime | None = None,
        fetched_at: datetime | None = None,
        summary: str | None = None,
        live_url_pattern: re.Pattern[str] | None = None,
    ) -> Headline:
        """Normalise raw parser output into a `Headline`.

        Raises `InvalidHeadlineError` when the row lacks a usable URL or when
        the title is empty or shorter than `MIN_TITLE_LENGTH` characters.
        `is_live` comes from `looks_live`, with the source's own pattern if any.
        """
        clean_url = (url or "").strip()
        if not clean_url:
            raise InvalidHeadlineError("missing url")
        scheme = urlsplit(clean_url).scheme.lower()
        if scheme not in {"http", "https"}:
            raise InvalidHeadlineError(f"unsupported url scheme: {clean_url!r}")

        clean_title = clean_text(title)
        if not clean_title:
            raise InvalidHeadlineError("empty title")
        if len(clean_title) < MIN_TITLE_LENGTH:
            raise InvalidHeadlineError(
                f"title shorter than {MIN_TITLE_LENGTH} chars: {clean_title!r}"
            )

        clean_summary = clean_text(summary) or None
        canonical_url = normalise_url(clean_url)
        return cls(
            source=source,
            title=clean_title,
            url=canonical_url,
            published_at=to_utc(published_at),
            fetched_at=to_utc(fetched_at) or utcnow(),
            summary=clean_summary,
            content_hash=compute_hash(clean_url, clean_title),
            is_live=looks_live(canonical_url, clean_title, live_url_pattern),
        )

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly mapping; datetimes become ISO-8601 strings."""
        return {
            "source": self.source,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "fetched_at": self.fetched_at.isoformat(),
            "summary": self.summary,
            "content_hash": self.content_hash,
            "is_live": self.is_live,
        }
