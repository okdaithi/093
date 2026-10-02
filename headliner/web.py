"""Read-only web viewer for the headline database: `headliner web`.

A small WSGI app on the standard library, so the deployment gains no
dependencies. It opens the database read-only on every request and never
writes; the fetch timer stays the only writer. Pages are plain HTML and CSS
with no JavaScript: Latest, Rewrites, Search, Sources and one page per
article showing every title it has carried.

Times follow the CLI: local time (the process's `TZ` or system zone) with the
zone named, UTC on request (`?utc=1`), and the UTC ISO timestamp on every
`<time>` element.
"""

from __future__ import annotations

import difflib
import html
import json
import logging
import re
import socketserver
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from headliner.config import Config, ConfigError, load_config
from headliner.models import Headline, utcnow
from headliner.store import (
    SCHEMA_VERSION,
    LiveFilter,
    Revision,
    TitleChange,
    article_history,
    connect_readonly,
    count_title_changes,
    list_headlines,
    list_title_changes,
    recent_runs,
    revision_counts,
    schema_version,
    search_headlines,
    search_history,
    source_status,
    totals,
)

logger = logging.getLogger(__name__)

PAGE_SIZE: Final = 50
MAX_PAGE: Final = 200
MAX_QUERY_LENGTH: Final = 200
SUMMARY_LENGTH: Final = 280
# A source whose last success is older than this missed at least one 6-hourly run.
STALE_AFTER: Final = timedelta(hours=13)

SINCE_CHOICES: Final = {
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "all": None,
}
LIVE_CHOICES: Final[tuple[LiveFilter, ...]] = ("exclude", "include", "only")
# Query parameters kept when following a link to a different page.
CARRIED_PARAMS: Final = frozenset({"tag", "source", "since", "utc"})

SECURITY_HEADERS: Final = [
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; img-src 'self' data:; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'",
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
]

_ICON: Final = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>"
    "<rect width='16' height='16' rx='3' fill='%23b4472a'/>"
    "<path d='M4 4h8M4 8h8M4 12h5' stroke='white' stroke-width='2'/></svg>"
)


# --- HTML building -------------------------------------------------------------


class Markup(str):
    """Text that is already HTML; `esc` passes it through unchanged."""

    __slots__ = ()


def esc(value: object) -> Markup:
    """HTML-escape `value` unless it is already `Markup`."""
    if isinstance(value, Markup):
        return value
    return Markup(html.escape("" if value is None else str(value), quote=True))


def render(template: str, **values: object) -> Markup:
    """`template.format(**values)` with every value escaped (Markup passes through)."""
    return Markup(template.format(**{key: esc(value) for key, value in values.items()}))


def join(parts: Iterable[object], separator: str = "") -> Markup:
    """Concatenate escaped `parts`."""
    return Markup(separator.join(esc(part) for part in parts))


def _safe_href(url: str) -> str | None:
    """`url` if it is http(s), else None: stored URLs never become script links."""
    return url if urlsplit(url).scheme in {"http", "https"} else None


def external_link(url: str, text: object, css: str = "") -> Markup:
    href = _safe_href(url)
    if href is None:
        return render('<span class="{css}">{text}</span>', css=css, text=text)
    return render(
        '<a class="{css}" href="{href}" target="_blank" rel="noopener noreferrer">{text}</a>',
        css=css,
        href=href,
        text=text,
    )


def word_diff(old: str, new: str) -> Markup:
    """`new` with words removed from `old` struck through and added words marked."""
    before, after = old.split(), new.split()
    parts: list[Markup] = []
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        if op == "equal":
            parts.append(esc(" ".join(after[j1:j2])))
            continue
        if op in {"delete", "replace"}:
            parts.append(render("<del>{t}</del>", t=" ".join(before[i1:i2])))
        if op in {"insert", "replace"}:
            parts.append(render("<ins>{t}</ins>", t=" ".join(after[j1:j2])))
    return join(parts, " ")


def highlight(text: str, query: str) -> Markup:
    """`text` with each query word (as a prefix, ignoring case) wrapped in <mark>."""
    words = sorted({word for word in re.findall(r"\w+", query) if word}, key=len, reverse=True)
    if not words:
        return esc(text)
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\w*", re.I)
    parts: list[Markup] = []
    last = 0
    for match in pattern.finditer(text):
        parts.append(esc(text[last : match.start()]))
        parts.append(render("<mark>{t}</mark>", t=match.group(0)))
        last = match.end()
    parts.append(esc(text[last:]))
    return join(parts)


def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rsplit(" ", 1)[0] + "…"


# --- Requests and responses ----------------------------------------------------


@dataclass(slots=True)
class Request:
    path: str
    query: dict[str, list[str]]

    def get(self, key: str, default: str = "") -> str:
        values = self.query.get(key)
        return values[0].strip() if values else default

    def get_all(self, key: str) -> list[str]:
        return [value.strip() for value in self.query.get(key, []) if value.strip()]

    @property
    def utc(self) -> bool:
        return self.get("utc") == "1"

    @property
    def page(self) -> int:
        try:
            return min(MAX_PAGE, max(1, int(self.get("page", "1"))))
        except ValueError:
            return 1


@dataclass(slots=True)
class Response:
    body: bytes
    status: str = "200 OK"
    content_type: str = "text/html; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


EMPTY: Final = Markup("")


class HttpError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# --- Time display --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Clock:
    """Formats stored UTC datetimes for display, in local time or UTC."""

    utc: bool

    def shown(self, value: datetime) -> datetime:
        return value.astimezone(UTC) if self.utc else value.astimezone()

    @property
    def zone(self) -> str:
        return "UTC" if self.utc else (datetime.now().astimezone().tzname() or "local")

    def time(self, value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> Markup:
        """A `<time>` element; its title and datetime attribute carry UTC."""
        if value is None:
            return Markup('<span class="muted">-</span>')
        shown = self.shown(value)
        label = shown.strftime(fmt)
        # Across a daylight-saving change the zone differs per value; say which.
        if not self.utc and shown.tzname() != self.zone:
            label += f" {shown.tzname()}"
        utc_iso = value.astimezone(UTC).isoformat(timespec="seconds")
        return render(
            '<time datetime="{iso}" title="{title}">{label}</time>',
            iso=utc_iso,
            title=utc_iso.replace("+00:00", "Z"),
            label=label,
        )

    def day(self, value: datetime) -> str:
        return self.shown(value).strftime("%A %-d %B %Y")

    def ago(self, value: datetime | None) -> str:
        if value is None:
            return "never"
        seconds = max(0, int((utcnow() - value).total_seconds()))
        if seconds < 90:
            return "just now"
        if seconds < 90 * 60:
            return f"{seconds // 60} min ago"
        if seconds < 36 * 3600:
            return f"{seconds // 3600} h ago"
        return f"{seconds // 86400} d ago"


# --- The application -----------------------------------------------------------


@dataclass(slots=True)
class Filters:
    """Source filters shared by the list pages, resolved against the config."""

    tags: list[str]
    source: str
    sources: list[str] | None
    notices: list[str]

    @property
    def named(self) -> list[str] | None:
        """`sources` narrowed to `source` when one is picked (for queries without `source=`)."""
        if not self.source:
            return self.sources
        if self.sources is None:
            return [self.source]
        wanted = self.source.casefold()
        return [name for name in self.sources if name.casefold() == wanted]


class WebApp:
    """The WSGI application. One instance serves every request; it holds no DB handle."""

    def __init__(self, db_path: Path | str, config_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path)
        self.config_path = Path(config_path) if config_path else None
        self._config: Config | None = None
        self._config_mtime: float | None = None
        self._config_error: str | None = None
        self._css = resources.files("headliner").joinpath("static/app.css").read_bytes()
        self.routes: dict[str, Callable[[Request, sqlite3.Connection], Markup]] = {
            "/": self.page_latest,
            "/rewrites": self.page_rewrites,
            "/search": self.page_search,
            "/sources": self.page_sources,
            "/article": self.page_article,
        }

    # -- WSGI plumbing

    def __call__(
        self, environ: dict[str, Any], start_response: Callable[..., Any]
    ) -> Iterator[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        if method not in {"GET", "HEAD"}:
            response = self.error_page("405 Method Not Allowed", "This viewer is read-only.")
            response.headers.append(("Allow", "GET, HEAD"))
        else:
            request = Request(
                path=environ.get("PATH_INFO", "/") or "/",
                query=parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=False),
            )
            response = self.dispatch(request)
        headers = [
            ("Content-Type", response.content_type),
            ("Content-Length", str(len(response.body))),
            *SECURITY_HEADERS,
            *response.headers,
        ]
        start_response(response.status, headers)
        return iter([b"" if method == "HEAD" else response.body])

    def dispatch(self, request: Request) -> Response:
        if request.path == "/static/app.css":
            return Response(
                self._css,
                content_type="text/css; charset=utf-8",
                headers=[("Cache-Control", "max-age=3600")],
            )
        if request.path == "/healthz":
            return self.healthz()
        handler = self.routes.get(request.path)
        if handler is None:
            return self.error_page("404 Not Found", "No such page.", request)
        try:
            with self.connection() as conn:
                body = handler(request, conn)
                header = self.stats_line(conn, Clock(request.utc))
        except HttpError as exc:
            return self.error_page(exc.status, exc.message, request)
        except Exception:
            logger.exception("error serving %s", request.path)
            return self.error_page(
                "500 Internal Server Error", "Something went wrong; see the service log.", request
            )
        page = self.layout(request, body, header)
        return Response(page.encode("utf-8"), headers=[("Cache-Control", "no-cache")])

    def connection(self) -> _ReadOnly:
        return _ReadOnly(self.db_path)

    def healthz(self) -> Response:
        try:
            with self.connection() as conn:
                info = totals(conn)
                payload = {
                    "status": "ok",
                    "schema": schema_version(conn),
                    "articles": info.articles,
                    "last_fetch": info.last_fetch.isoformat() if info.last_fetch else None,
                }
                status = "200 OK"
        except HttpError as exc:
            payload = {"status": "error", "error": exc.message}
            status = exc.status
        return Response(
            json.dumps(payload).encode(),
            status=status,
            content_type="application/json",
            headers=[("Cache-Control", "no-store")],
        )

    # -- Config (for tags), reloaded when the file changes

    @property
    def config(self) -> Config | None:
        if self.config_path is None:
            return None
        try:
            mtime = self.config_path.stat().st_mtime
        except OSError as exc:
            self._config, self._config_error = None, f"cannot read {self.config_path}: {exc}"
            return None
        if mtime != self._config_mtime:
            self._config_mtime = mtime
            try:
                self._config, self._config_error = load_config(self.config_path), None
            except ConfigError as exc:
                logger.warning("config not loaded: %s", exc)
                self._config, self._config_error = None, str(exc)
        return self._config

    def filters(self, request: Request) -> Filters:
        """Tag and source filters from the query string; unknown values become notices."""
        notices: list[str] = []
        source = request.get("source")
        config = self.config
        tags = request.get_all("tag")
        sources: list[str] | None = None
        if tags and config is None:
            notices.append("Tag filters need the sources file, which could not be loaded.")
            tags = []
        elif tags and config is not None:
            known = {tag.casefold(): tag for tag in config.all_tags}
            unknown = [tag for tag in tags if tag.casefold() not in known]
            if unknown:
                notices.append(f"Unknown tag(s) ignored: {', '.join(unknown)}.")
            tags = [known[tag.casefold()] for tag in tags if tag.casefold() in known]
            if tags:
                sources = [source.name for source in config.tagged(tags)]
        return Filters(tags=tags, source=source, sources=sources, notices=notices)

    # -- Shared page parts

    def link(self, request: Request, path: str, **changes: object) -> str:
        """A URL to `path` keeping the current filters, with `changes` applied.

        Within a page every parameter carries over; to another page only the
        shared filters (tags, source, time window, UTC) do. A change of None
        drops that parameter; a list sets repeated values. `page` is always
        reset unless given.
        """
        carried = None if path == request.path else CARRIED_PARAMS
        params: dict[str, list[str]] = {
            key: list(values)
            for key, values in request.query.items()
            if key != "page" and (carried is None or key in carried)
        }
        for key, value in changes.items():
            if value is None or value == "" or value is False:
                params.pop(key, None)
            elif isinstance(value, list):
                params[key] = [str(item) for item in value]
            else:
                params[key] = ["1" if value is True else str(value)]
        query = urlencode([(key, item) for key, values in params.items() for item in values])
        return f"{path}?{query}" if query else path

    def stats_line(self, conn: sqlite3.Connection, clock: Clock) -> Markup:
        info = totals(conn)
        return render(
            "{articles} articles · {rewrites} title changes · last fetch {when} ({ago})",
            articles=f"{info.articles:,}",
            rewrites=f"{info.rewrites:,}",
            when=clock.time(info.last_fetch, "%a %H:%M"),
            ago=clock.ago(info.last_fetch),
        )

    def layout(self, request: Request, body: Markup, stats: Markup | None = None) -> str:
        clock = Clock(request.utc)
        nav = join(
            render(
                '<a href="{href}"{current}>{label}</a>',
                href=self.link(request, path),
                current=Markup(' aria-current="page"') if request.path == path else Markup(""),
                label=label,
            )
            for path, label in (
                ("/", "Latest"),
                ("/rewrites", "Rewrites"),
                ("/search", "Search"),
                ("/sources", "Sources"),
            )
        )
        other = Clock(False).zone if request.utc else "UTC"
        toggle = render(
            '<a class="zone" href="{href}">Times: {zone} · show {other}</a>',
            href=self.link(
                request,
                request.path or "/",
                utc=not request.utc,
                page=request.page if request.page > 1 else None,
            ),
            zone=clock.zone,
            other=other,
        )
        warning = (
            render('<p class="notice">{e}</p>', e=f"Sources file problem: {self._config_error}")
            if self._config_error
            else Markup("")
        )
        return render(
            """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>headliner</title>
<link rel="icon" href="{icon}">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<header class="top">
  <div class="bar">
    <a class="brand" href="/">headliner</a>
    <nav>{nav}</nav>
    {toggle}
  </div>
  <p class="stats">{stats}</p>
</header>
<main>
{warning}{body}
</main>
<footer>Read-only view of the headliner database. Times in {zone}; hover a time for UTC.</footer>
</body>
</html>
""",
            icon=_ICON,
            nav=nav,
            toggle=toggle,
            stats=stats or Markup(""),
            warning=warning,
            body=body,
            zone=clock.zone,
        )

    def error_page(self, status: str, message: str, request: Request | None = None) -> Response:
        body = render(
            '<section class="error"><h1>{title}</h1><p>{message}</p></section>',
            title=status.split(" ", 1)[1],
            message=message,
        )
        page = self.layout(request or Request(path="", query={}), body)
        return Response(page.encode("utf-8"), status=status)

    def filter_form(
        self,
        request: Request,
        filters: Filters,
        *,
        since: str | None = "all",
        extra: Markup = EMPTY,
        keep: Sequence[str] = (),
    ) -> Markup:
        """The filter bar: tag checkboxes, source picker, time window."""
        config = self.config
        tag_boxes = join(
            render(
                '<label class="chip"><input type="checkbox" name="tag" value="{tag}"{checked}>'
                "{tag}</label>",
                tag=tag,
                checked=Markup(" checked") if tag in filters.tags else Markup(""),
            )
            for tag in (config.all_tags if config else [])
        )
        names = sorted((s.name for s in config.sources), key=str.casefold) if config else []
        if filters.source and filters.source not in names:
            names.insert(0, filters.source)
        options = join(
            render(
                '<option value="{name}"{sel}>{name}</option>',
                name=name,
                sel=Markup(" selected") if name == filters.source else Markup(""),
            )
            for name in names
        )
        source_select = render(
            '<label>Source <select name="source"><option value="">All sources</option>'
            "{options}</select></label>",
            options=options,
        )
        since_select = Markup("")
        if since is not None:
            chosen = request.get("since", since)
            if chosen not in SINCE_CHOICES:
                chosen = since
            since_select = render(
                '<label>Within <select name="since">{options}</select></label>',
                options=join(
                    render(
                        '<option value="{value}"{sel}>{label}</option>',
                        value=key,
                        label="any time" if key == "all" else f"last {key}",
                        sel=Markup(" selected") if key == chosen else Markup(""),
                    )
                    for key in SINCE_CHOICES
                ),
            )
        hidden = join(
            render('<input type="hidden" name="{k}" value="{v}">', k=key, v=value)
            for key in ("utc", *keep)
            for value in request.get_all(key)
        )
        notices = join(render('<p class="notice">{n}</p>', n=note) for note in filters.notices)
        return render(
            """<form class="filters" method="get" action="{action}">
  {extra}
  <fieldset class="tags"><legend>Tags</legend>{tags}</fieldset>
  {source}{since}{hidden}
  <button type="submit">Apply</button>
  <a class="reset" href="{reset}">Reset</a>
</form>
{notices}""",
            action=request.path,
            extra=extra,
            tags=tag_boxes or Markup('<span class="muted">none configured</span>'),
            source=source_select,
            since=since_select,
            hidden=hidden,
            reset=request.path + ("?utc=1" if request.utc else ""),
            notices=notices,
        )

    def since(self, request: Request, default: str) -> datetime | None:
        key = request.get("since", default)
        window = SINCE_CHOICES.get(key, SINCE_CHOICES[default])
        return utcnow() - window if window else None

    def pager(self, request: Request, has_more: bool) -> Markup:
        page = request.page
        links = []
        if page > 1:
            links.append(
                render(
                    '<a href="{h}">← Newer</a>', h=self.link(request, request.path, page=page - 1)
                )
            )
        if has_more and page < MAX_PAGE:
            links.append(
                render(
                    '<a href="{h}">Older →</a>', h=self.link(request, request.path, page=page + 1)
                )
            )
        if not links:
            return Markup("")
        return render('<nav class="pager">{l}<span>page {p}</span></nav>', l=join(links), p=page)

    def badges(self, headline: Headline, titles: int, request: Request) -> Markup:
        parts = []
        if headline.is_live:
            parts.append(Markup('<span class="badge live">LIVE</span>'))
        if titles > 1:
            parts.append(
                render(
                    '<a class="badge rewritten" href="{h}" title="See every title">{n} titles</a>',
                    h=self.link(request, "/article", url=headline.url, q=None, page=None),
                    n=titles,
                )
            )
        return join(parts, " ")

    def headline_items(
        self,
        request: Request,
        conn: sqlite3.Connection,
        headlines: Sequence[Headline],
        *,
        matched: Sequence[str | None] | None = None,
        query: str = "",
        group_by_day: bool = False,
    ) -> Markup:
        if not headlines:
            return Markup('<p class="empty">No headlines match.</p>')
        clock = Clock(request.utc)
        counts = revision_counts(conn, [headline.url for headline in headlines])
        out: list[Markup] = []
        day = None
        for index, headline in enumerate(headlines):
            when = headline.published_at or headline.fetched_at
            if group_by_day:
                this_day = clock.day(when)
                if this_day != day:
                    if day is not None:
                        out.append(Markup("</ol>"))
                    out.append(render('<h2 class="day">{d}</h2><ol class="items">', d=this_day))
                    day = this_day
            title = highlight(headline.title, query) if query else esc(headline.title)
            earlier = matched[index] if matched else None
            summary = _shorten(headline.summary, SUMMARY_LENGTH) if headline.summary else ""
            out.append(
                render(
                    """<li class="item">
  <div class="meta">{time} <a class="source" href="{src_href}">{source}</a> {badges}</div>
  {title}
  {earlier}{summary}
</li>""",
                    time=clock.time(when, "%H:%M" if group_by_day else "%Y-%m-%d %H:%M"),
                    src_href=self.link(request, "/", source=headline.source, tag=None, q=None),
                    source=headline.source,
                    badges=self.badges(headline, counts.get(headline.url, 1), request),
                    title=external_link(headline.url, title, "title"),
                    earlier=render(
                        '<p class="earlier">Matched earlier title: {t}</p>',
                        t=highlight(earlier, query),
                    )
                    if earlier
                    else Markup(""),
                    summary=render('<p class="summary">{s}</p>', s=highlight(summary, query))
                    if summary
                    else Markup(""),
                )
            )
        if group_by_day:
            out.append(Markup("</ol>"))
            return join(out)
        return render('<ol class="items">{i}</ol>', i=join(out))

    # -- Pages

    def page_latest(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        page = request.page
        rows = list_headlines(
            conn,
            since=self.since(request, "all"),
            source=filters.source or None,
            sources=filters.sources,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
        has_more = len(rows) > PAGE_SIZE
        return render(
            "<h1>Latest headlines</h1>{form}{items}{pager}",
            form=self.filter_form(request, filters, since="all"),
            items=self.headline_items(request, conn, rows[:PAGE_SIZE], group_by_day=True),
            pager=self.pager(request, has_more),
        )

    def page_rewrites(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        live: LiveFilter = request.get("live", "exclude")  # type: ignore[assignment]
        if live not in LIVE_CHOICES:
            live = "exclude"
        oldest = request.get("oldest") == "1"
        since = self.since(request, "7d")
        common: dict[str, Any] = {
            "since": since,
            "source": filters.source or None,
            "sources": filters.sources,
        }
        page = request.page
        changes = list_title_changes(
            conn,
            **common,
            live=live,
            oldest_first=oldest,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
        has_more = len(changes) > PAGE_SIZE
        hidden = (
            count_title_changes(conn, **common, live="include")
            - count_title_changes(conn, **common, live="exclude")
            if live == "exclude"
            else 0
        )
        live_select = render(
            '<label>Live blogs <select name="live">{o}</select></label>'
            '<label class="check"><input type="checkbox" name="oldest" value="1"{c}>'
            "Oldest first</label>",
            o=join(
                render(
                    '<option value="{v}"{s}>{label}</option>',
                    v=value,
                    label=label,
                    s=Markup(" selected") if value == live else Markup(""),
                )
                for value, label in (
                    ("exclude", "hidden"),
                    ("include", "included"),
                    ("only", "only (timeline)"),
                )
            ),
            c=Markup(" checked") if oldest else Markup(""),
        )
        note = (
            render(
                '<p class="notice">{n} live-blog title change(s) hidden. '
                '<a href="{inc}">Include them</a> or <a href="{only}">show live blogs only</a>.'
                "</p>",
                n=hidden,
                inc=self.link(request, "/rewrites", live="include"),
                only=self.link(request, "/rewrites", live="only"),
            )
            if hidden
            else Markup("")
        )
        if request.get("since") == "":
            intro = Markup('<p class="muted">Showing the last 7 days by default.</p>')
        else:
            intro = Markup("")
        return render(
            "<h1>Rewritten headlines</h1>{form}{intro}{note}{items}{pager}",
            form=self.filter_form(request, filters, since="7d", extra=live_select),
            intro=intro,
            note=note,
            items=self.change_items(request, changes[:PAGE_SIZE]),
            pager=self.pager(request, has_more),
        )

    def change_items(self, request: Request, changes: Sequence[TitleChange]) -> Markup:
        if not changes:
            return Markup('<p class="empty">No title changes match.</p>')
        clock = Clock(request.utc)
        items = []
        for change in changes:
            if change.old_title is None:
                text = render('<span class="first">first seen</span> {t}', t=change.new_title)
            else:
                text = word_diff(change.old_title, change.new_title)
            items.append(
                render(
                    """<li class="item change">
  <div class="meta">{time} <a class="source" href="{src}">{source}</a> {live}
    <a class="history" href="{hist}">all titles</a> {open}</div>
  <p class="diff">{text}</p>
  {was}
</li>""",
                    time=clock.time(change.changed_at),
                    src=self.link(request, "/rewrites", source=change.source, tag=None),
                    source=change.source,
                    live=Markup('<span class="badge live">LIVE</span>')
                    if change.is_live
                    else Markup(""),
                    hist=self.link(request, "/article", url=change.url, live=None, oldest=None),
                    open=external_link(change.url, "open article ↗", "history"),
                    text=text,
                    was=render('<p class="was">was: {t}</p>', t=change.old_title)
                    if change.old_title
                    else Markup(""),
                )
            )
        return render('<ol class="items">{i}</ol>', i=join(items))

    def page_search(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        query = request.get("q")[:MAX_QUERY_LENGTH]
        history = request.get("history") == "1"
        box = render(
            '<label class="q">Search <input type="search" name="q" value="{q}" '
            'placeholder="words in titles and summaries" autofocus></label>'
            '<label class="check"><input type="checkbox" name="history" value="1"{c}>'
            "Include earlier titles</label>",
            q=query,
            c=Markup(" checked") if history else Markup(""),
        )
        form = self.filter_form(request, filters, since=None, extra=box)
        if not query:
            return render("<h1>Search</h1>{form}", form=form)
        limit = 100
        if history:
            hits = search_history(conn, query, limit=limit, sources=filters.named)
            headlines = [hit.headline for hit in hits]
            matched: list[str | None] | None = [hit.matched_title for hit in hits]
        else:
            headlines = search_headlines(conn, query, limit=limit, sources=filters.named)
            matched = None
        count = (
            f"{len(headlines)} result(s)" if len(headlines) < limit else f"the best {limit} results"
        )
        scope = "current and earlier titles" if history else "current titles"
        return render(
            '<h1>Search</h1>{form}<p class="muted">{count} for “{q}” in {scope}, best first.</p>'
            "{items}",
            form=form,
            count=count,
            q=query,
            scope=scope,
            items=self.headline_items(request, conn, headlines, matched=matched, query=query),
        )

    def page_sources(self, request: Request, conn: sqlite3.Connection) -> Markup:
        clock = Clock(request.utc)
        config = self.config
        if config is None:
            raise HttpError(
                "503 Service Unavailable",
                "The sources page needs the sources file, which could not be loaded.",
            )
        tags = [
            tag
            for tag in request.get_all("tag")
            if tag.casefold() in {known.casefold() for known in config.all_tags}
        ]
        shown = config.tagged(tags) if tags else config.sources
        statuses = {status.name: status for status in source_status(conn, [s.name for s in shown])}
        now = utcnow()
        rows = []
        healthy = 0
        for source in shown:
            status = statuses[source.name]
            if not source.enabled:
                state, css = "disabled", "muted"
            elif status.last_status == "error":
                state, css = "failed", "bad"
            elif status.last_status == "skipped":
                state, css = "skipped", "warn"
            elif status.last_success is None:
                state, css = "never fetched", "warn"
            elif now - status.last_success > STALE_AFTER:
                state, css = "stale", "warn"
            else:
                state, css = "ok", "good"
                healthy += 1
            rows.append(
                render(
                    """<tr>
  <td><a href="{list}">{name}</a> <span class="muted small">{type}</span></td>
  <td>{tags}</td>
  <td class="num">{items}</td>
  <td>{success} <span class="muted small">{ago}</span></td>
  <td><span class="state {css}" title="{error}">{state}</span>{detail}</td>
  <td>{feed}</td>
</tr>""",
                    list=self.link(request, "/", source=source.name, tag=None),
                    name=source.name,
                    type=source.type,
                    tags=join(
                        render(
                            '<a class="chip" href="{h}">{t}</a>',
                            h=self.link(request, "/sources", tag=tag),
                            t=tag,
                        )
                        for tag in source.tags
                    ),
                    items=f"{status.total_items:,}",
                    success=clock.time(status.last_success),
                    ago=clock.ago(status.last_success) if status.last_success else "",
                    css=css,
                    error=status.last_error or "",
                    state=state,
                    detail=render(
                        '<div class="small muted">{e}</div>', e=_shorten(status.last_error, 120)
                    )
                    if status.last_error and state in {"failed", "skipped"}
                    else Markup(""),
                    feed=external_link(source.url, "feed ↗", "small"),
                )
            )
        enabled = sum(1 for source in shown if source.enabled)
        tag_links = join(
            render(
                '<a class="chip{on}" href="{h}">{t}</a>',
                on=Markup(" on") if tag in tags else Markup(""),
                h=self.link(request, "/sources", tag=None if tag in tags else tag),
                t=tag,
            )
            for tag in config.all_tags
        )
        runs = recent_runs(conn, limit=12)
        run_rows = join(
            render(
                """<tr>
  <td>{start}</td><td class="num">{secs}s</td>
  <td class="num">{ok}</td><td class="num">{skipped}</td>
  <td class="num {fcss}">{failed}</td>
  <td class="num">{found}</td><td class="num">{new}</td><td class="num">{changed}</td>
  <td class="small">{names}</td>
</tr>""",
                start=clock.time(run.started_at, "%a %d %b %H:%M"),
                secs=max(0, int((run.finished_at - run.started_at).total_seconds())),
                ok=run.ok,
                skipped=run.skipped,
                failed=run.failed,
                fcss="bad" if run.failed else "",
                found=run.found,
                new=run.new,
                changed=run.changed,
                names=", ".join(run.failed_sources),
            )
            for run in runs
        )
        return render(
            """<h1>Sources</h1>
<p class="summary-line">
  <span><strong>{healthy}</strong> of {enabled} enabled source(s) healthy.</span>
  <span class="tagbar">Tags: {tag_links}</span></p>
<div class="scroll"><table>
<thead><tr><th>Source</th><th>Tags</th><th class="num">Items</th>
<th>Last success ({zone})</th><th>Last run</th><th>Feed</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Recent runs</h2>
<div class="scroll"><table>
<thead><tr><th>Started ({zone})</th><th class="num">Took</th><th class="num">OK</th>
<th class="num">Skipped</th><th class="num">Failed</th><th class="num">Found</th>
<th class="num">New</th><th class="num">Retitled</th><th>Failed sources</th></tr></thead>
<tbody>{run_rows}</tbody></table></div>""",
            healthy=healthy,
            enabled=enabled,
            tag_links=tag_links,
            zone=clock.zone,
            rows=join(rows),
            run_rows=run_rows or Markup('<tr><td colspan="9">No runs logged yet.</td></tr>'),
        )

    def page_article(self, request: Request, conn: sqlite3.Connection) -> Markup:
        url = request.get("url")
        found = article_history(conn, url) if url else None
        if found is None:
            raise HttpError("404 Not Found", "No stored article has that URL.")
        headline, revisions = found
        clock = Clock(request.utc)
        items = []
        previous: Revision | None = None
        for revision in revisions:
            text = (
                word_diff(previous.title, revision.title)
                if previous is not None
                else esc(revision.title)
            )
            items.append(
                render(
                    '<li class="item"><div class="meta">{t} {label}</div>'
                    '<p class="diff">{text}</p>{summary}</li>',
                    t=clock.time(revision.seen_at),
                    label="first seen" if previous is None else "changed to",
                    text=text,
                    summary=render('<p class="summary">{s}</p>', s=revision.summary)
                    if revision.summary
                    else Markup(""),
                )
            )
            previous = revision
        return render(
            """<h1>{title}</h1>
<p class="meta">{source} {live} · published {published} · {open}</p>
<h2>{n} title(s), oldest first</h2>
<ol class="items timeline">{items}</ol>""",
            title=headline.title,
            source=headline.source,
            live=Markup('<span class="badge live">LIVE</span>') if headline.is_live else Markup(""),
            published=clock.time(headline.published_at),
            open=external_link(headline.url, "open article ↗"),
            n=len(revisions),
            items=join(items),
        )


class _ReadOnly:
    """Context manager: a read-only connection, or an HttpError explaining why not."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise HttpError(
                "503 Service Unavailable",
                f"No database at {self.path} yet; it appears after the first fetch.",
            )
        try:
            self.conn = connect_readonly(self.path)
            version = schema_version(self.conn)
        except sqlite3.Error as exc:
            self.__exit__()
            raise HttpError("503 Service Unavailable", f"Cannot open the database: {exc}") from exc
        if version < SCHEMA_VERSION:
            self.__exit__()
            raise HttpError(
                "503 Service Unavailable",
                f"The database is at schema version {version}; this viewer needs "
                f"{SCHEMA_VERSION}. Run `headliner migrate` (see the README).",
            )
        return self.conn

    def __exit__(self, *exc: object) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# --- Serving -------------------------------------------------------------------


class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class _LoggingHandler(WSGIRequestHandler):
    """Request lines go to the `headliner.web` logger instead of stderr."""

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s %s", self.address_string(), format % args)


def serve(
    db_path: Path | str,
    config_path: Path | str | None,
    *,
    host: str = "127.0.0.1",
    port: int = 8090,
) -> None:
    """Serve the viewer until interrupted."""
    app = WebApp(db_path, config_path)
    with make_server(
        host, port, app, server_class=ThreadingWSGIServer, handler_class=_LoggingHandler
    ) as server:
        logger.info("serving http://%s:%d/ (read-only, database %s)", host, port, app.db_path)
        server.serve_forever()
