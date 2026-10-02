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
import math
import re
import socketserver
import sqlite3
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from headliner.backup import default_dir, list_backups
from headliner.config import Config, ConfigError, load_config
from headliner.models import Headline, is_minor_change, utcnow
from headliner.store import (
    SCHEMA_VERSION,
    LiveFilter,
    Revision,
    SourceStatus,
    TitleChange,
    Totals,
    article_history,
    connect_readonly,
    feed_turnover,
    first_seen,
    hidden_changes,
    list_headlines,
    list_title_changes,
    recent_runs,
    revision_counts,
    rewrite_stats,
    schema_version,
    search_headlines,
    search_history,
    source_status,
    totals,
)
from headliner.stories import MAX_STORY_HEADLINES, Story, by_url, cluster

logger = logging.getLogger(__name__)

PAGE_SIZE: Final = 50
MAX_PAGE: Final = 200
MAX_QUERY_LENGTH: Final = 200
SUMMARY_LENGTH: Final = 280
# A source whose last success is older than this missed at least one 6-hourly run.
STALE_AFTER: Final = timedelta(hours=13)
# A feed that fetches fine but whose newest item is older than this has
# probably been frozen by its publisher (as CNN's and Xinhua's were).
CONTENT_STALE_AFTER: Final = timedelta(days=3)
# Runs are 6-hourly with up to 5 minutes of jitter; one missed run is worth a look.
RUN_LATE_AFTER: Final = timedelta(hours=7)
# Backups are daily.
BACKUP_LATE_AFTER: Final = timedelta(hours=26)

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
STORY_WINDOWS: Final = {
    "24h": timedelta(hours=24),
    "3d": timedelta(days=3),
    "7d": timedelta(days=7),
}
# The window behind "N outlets" badges and /story links.
STORY_LINK_WINDOW: Final = "3d"
STORIES_PAGE_SIZE: Final = 30
TREND_WINDOWS: Final = {"7d": 7, "14d": 14, "30d": 30}
HEAT_LEVELS: Final = 5

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
MINOR_BADGE: Final = Markup(
    '<span class="badge minor" title="Only case, punctuation or spacing changed">minor</span>'
)


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
            "/stories": self.page_stories,
            "/story": self.page_story,
            "/trends": self.page_trends,
        }
        self._story_cache: dict[tuple[object, ...], list[Story]] = {}
        self._story_lock = threading.Lock()
        self._totals: tuple[tuple[object, ...], Totals] | None = None

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
        if request.path == "/api/status":
            return self.api_status()
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
                info = self.totals(conn)
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

    def api_status(self) -> Response:
        """Everything the status check needs, as JSON, without sudo.

        `status` is "ok", or "attention" with the reasons in `checks`. A source
        that is skipped (robots.txt) is listed but does not need attention.
        """
        now = utcnow()
        checks: list[str] = []
        try:
            with self.connection() as conn:
                info = self.totals(conn)
                version = schema_version(conn)
                runs = recent_runs(conn, limit=8)
                config = self.config
                sources: dict[str, Any] | None = None
                problems: list[dict[str, Any]] = []
                if config is not None:
                    names = [source.name for source in config.sources]
                    statuses = {status.name: status for status in source_status(conn, names)}
                    states: Counter[str] = Counter()
                    for source in config.sources:
                        status = statuses[source.name]
                        state, _ = source_state(source.enabled, status, now)
                        states[state] += 1
                        if state in {"ok", "disabled"}:
                            continue
                        problems.append(
                            {
                                "name": source.name,
                                "state": state,
                                "error": status.last_error,
                                "last_success": _iso_or_none(status.last_success),
                                "newest_item": _iso_or_none(status.newest_item),
                            }
                        )
                    sources = {
                        "configured": len(config.sources),
                        "enabled": sum(1 for source in config.sources if source.enabled),
                        "states": dict(sorted(states.items())),
                    }
                    attention = [p["name"] for p in problems if p["state"] != "skipped"]
                    if attention:
                        checks.append(f"{len(attention)} source(s) need attention")
                else:
                    checks.append(f"sources file not loaded: {self._config_error}")
        except HttpError as exc:
            return Response(
                json.dumps({"status": "error", "error": exc.message}).encode(),
                status=exc.status,
                content_type="application/json",
                headers=[("Cache-Control", "no-store")],
            )
        if not runs:
            checks.append("no fetch runs logged")
        else:
            if now - runs[0].finished_at > RUN_LATE_AFTER:
                checks.append(f"last run finished {runs[0].finished_at:%Y-%m-%d %H:%M}Z")
            if runs[0].failed:
                checks.append(f"last run: {runs[0].failed} source(s) failed")
        backups = list_backups(default_dir(self.db_path), self.db_path.stem)
        if not backups:
            checks.append("no backups")
        elif now - backups[0].taken_at > BACKUP_LATE_AFTER:
            checks.append(f"latest backup is from {backups[0].taken_at:%Y-%m-%d}")
        payload: dict[str, Any] = {
            "status": "attention" if checks else "ok",
            "checks": checks,
            "checked_at": now.isoformat(timespec="seconds"),
            "schema": version,
            "database": {
                "bytes": self.db_path.stat().st_size,
                "articles": info.articles,
                "title_changes": info.rewrites,
                "live_blogs": info.live,
            },
            "last_fetch": _iso_or_none(info.last_fetch),
            "runs": [
                {
                    "started_at": run.started_at.isoformat(),
                    "seconds": int((run.finished_at - run.started_at).total_seconds()),
                    "ok": run.ok,
                    "skipped": run.skipped,
                    "failed": run.failed,
                    "found": run.found,
                    "new": run.new,
                    "retitled": run.changed,
                    "failed_sources": list(run.failed_sources),
                }
                for run in runs
            ],
            "sources": sources,
            "problems": problems,
            "backups": {
                "count": len(backups),
                "latest": backups[0].path.name if backups else None,
                "latest_at": backups[0].taken_at.isoformat() if backups else None,
                "latest_bytes": backups[0].size if backups else None,
            },
        }
        return Response(
            json.dumps(payload, indent=2).encode(),
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

    def totals(self, conn: sqlite3.Connection) -> Totals:
        """`store.totals`, recomputed only when a fetch has written something."""
        key = tuple(
            conn.execute(
                "SELECT (SELECT MAX(id) FROM headline_revisions), (SELECT MAX(id) FROM fetch_log),"
                " (SELECT MAX(id) FROM headlines)"
            ).fetchone()
        )
        cached = self._totals
        if cached is not None and cached[0] == key:
            return cached[1]
        info = totals(conn)
        self._totals = (key, info)
        return info

    def stats_line(self, conn: sqlite3.Connection, clock: Clock) -> Markup:
        info = self.totals(conn)
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
                ("/stories", "Stories"),
                ("/rewrites", "Rewrites"),
                ("/search", "Search"),
                ("/trends", "Trends"),
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

    def badges(
        self, headline: Headline, titles: int, request: Request, story: Story | None = None
    ) -> Markup:
        parts = []
        if headline.is_live:
            parts.append(Markup('<span class="badge live">LIVE</span>'))
        if story is not None and len(story.sources) > 1:
            parts.append(
                render(
                    '<a class="badge story" href="{h}" title="Other outlets reporting this">'
                    "{n} outlets</a>",
                    h=self.link(request, "/story", url=headline.url),
                    n=len(story.sources),
                )
            )
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
        story_of = by_url(self.stories(conn, STORY_LINK_WINDOW))
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
                    badges=self.badges(
                        headline, counts.get(headline.url, 1), request, story_of.get(headline.url)
                    ),
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
        minor = request.get("minor") == "1"
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
            minor=minor,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
        )
        has_more = len(changes) > PAGE_SIZE
        hidden, hidden_minor = hidden_changes(conn, **common, live=live, minor=minor)
        live_select = render(
            '<label>Live blogs <select name="live">{o}</select></label>'
            '<label class="check"><input type="checkbox" name="oldest" value="1"{c}>'
            "Oldest first</label>"
            '<label class="check"><input type="checkbox" name="minor" value="1"{m}>'
            "Include punctuation-only changes</label>",
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
            m=Markup(" checked") if minor else Markup(""),
        )
        minor_note = (
            render(
                '<p class="notice">{n} minor change(s) hidden: only case, punctuation or '
                'spacing changed. <a href="{inc}">Show them</a>.</p>',
                n=hidden_minor,
                inc=self.link(request, "/rewrites", minor=True),
            )
            if hidden_minor
            else Markup("")
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
            "<h1>Rewritten headlines</h1>{form}{intro}{note}{minor_note}{items}{pager}",
            form=self.filter_form(request, filters, since="7d", extra=live_select),
            intro=intro,
            note=note,
            minor_note=minor_note,
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
  <div class="meta">{time} <a class="source" href="{src}">{source}</a> {live}{minor}
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
                    minor=MINOR_BADGE if change.is_minor else Markup(""),
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
            state, css = source_state(source.enabled, status, now)
            healthy += state == "ok"
            if state == "content stale" and status.newest_item is not None:
                status = replace(
                    status, last_error=f"newest item {status.newest_item:%Y-%m-%d}: feed frozen?"
                )
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
                    if status.last_error and state in {"failed", "skipped", "content stale"}
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
                    label="first seen"
                    if previous is None
                    else "changed to (punctuation only)"
                    if is_minor_change(previous.title, revision.title)
                    else "changed to",
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

    # -- Stories

    def stories(self, conn: sqlite3.Connection, window: str) -> list[Story]:
        """Stories among the last `window` of headlines, cached until the data changes.

        A fetch run changes the newest `fetched_at`; the 15-minute bucket moves
        the window along between runs.
        """
        newest, count = conn.execute("SELECT MAX(fetched_at), COUNT(*) FROM headlines").fetchone()
        key = (window, newest, count, int(utcnow().timestamp() // 900))
        with self._story_lock:
            cached = self._story_cache.get(key)
        if cached is not None:
            return cached
        headlines = list_headlines(
            conn, since=utcnow() - STORY_WINDOWS[window], limit=MAX_STORY_HEADLINES
        )
        result = cluster(headlines)
        with self._story_lock:
            if len(self._story_cache) >= 8:
                self._story_cache.clear()
            self._story_cache[key] = result
        return result

    def tag_counts(self, story: Story) -> Markup:
        """'AU 3 · IE 1': outlets per tag, from the config."""
        config = self.config
        if config is None:
            return EMPTY
        tags_of = {source.name.casefold(): source.tags for source in config.sources}
        counts: Counter[str] = Counter()
        for name in story.sources:
            counts.update(tags_of.get(name.casefold(), ()))
        return join(
            render('<span class="chip">{t} {n}</span>', t=tag, n=n)
            for tag, n in sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
        )

    def story_members(self, request: Request, conn: sqlite3.Connection, story: Story) -> Markup:
        clock = Clock(request.utc)
        counts = revision_counts(conn, [headline.url for headline in story.headlines])
        return join(
            render(
                '<li><span class="meta">{time} <span class="source">{source}</span> {badges}'
                "</span> {title}</li>",
                time=clock.time(headline.published_at or headline.fetched_at, "%a %H:%M"),
                source=headline.source,
                badges=self.badges(headline, counts.get(headline.url, 1), request),
                title=external_link(headline.url, headline.title),
            )
            for headline in story.headlines
        )

    def page_stories(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        window = request.get("since", "24h")
        if window not in STORY_WINDOWS:
            window = "24h"
        try:
            minimum = max(2, min(10, int(request.get("min", "2"))))
        except ValueError:
            minimum = 2
        newest = request.get("sort") == "newest"
        wanted = {name.casefold() for name in (filters.named or [])}
        chosen = [
            story
            for story in self.stories(conn, window)
            if len(story.sources) >= minimum
            and (filters.named is None or any(n.casefold() in wanted for n in story.sources))
        ]
        if newest:
            chosen.sort(key=lambda story: story.last_seen, reverse=True)
        else:
            chosen.sort(key=lambda story: (len(story.sources), story.last_seen), reverse=True)
        page = request.page
        shown = chosen[(page - 1) * STORIES_PAGE_SIZE : page * STORIES_PAGE_SIZE]
        clock = Clock(request.utc)
        cards = join(
            render(
                """<li class="story">
  <div class="meta">{first} to {last} · <strong>{n} outlets</strong> {tags}</div>
  <h3><a href="{href}">{title}</a></h3>
  <details><summary>{count} headlines</summary><ol class="members">{members}</ol></details>
</li>""",
                first=clock.time(story.first_seen, "%a %H:%M"),
                last=clock.time(story.last_seen, "%a %H:%M"),
                n=len(story.sources),
                tags=self.tag_counts(story),
                href=self.link(request, "/story", url=(story.lead or story.headlines[0]).url),
                title=story.title,
                count=len(story.headlines),
                members=self.story_members(request, conn, story),
            )
            for story in shown
        )
        options = render(
            '<label>Outlets <select name="min">{m}</select></label>'
            '<label>Order <select name="sort">{o}</select></label>',
            m=join(
                render(
                    '<option value="{v}"{s}>{v}+</option>',
                    v=value,
                    s=Markup(" selected") if value == minimum else EMPTY,
                )
                for value in (2, 3, 5)
            ),
            o=join(
                render(
                    '<option value="{v}"{s}>{label}</option>',
                    v=value,
                    label=label,
                    s=Markup(" selected") if (value == "newest") == newest else EMPTY,
                )
                for value, label in (("covered", "most outlets"), ("newest", "newest"))
            ),
        )
        return render(
            """<h1>Stories</h1>
{form}
<p class="muted">{total} stories reported by {minimum} or more outlets in the last {window}.
Headlines are grouped by shared words, so the occasional grouping is wrong.</p>
{cards}{pager}""",
            form=self.filter_form(
                request, filters, since=None, extra=join([options, self.window_select(window)])
            ),
            total=len(chosen),
            minimum=minimum,
            window=window,
            cards=render('<ol class="stories">{c}</ol>', c=cards)
            if shown
            else Markup('<p class="empty">No stories match.</p>'),
            pager=self.pager(request, page * STORIES_PAGE_SIZE < len(chosen)),
        )

    @staticmethod
    def window_select(window: str) -> Markup:
        return render(
            '<label>Within <select name="since">{o}</select></label>',
            o=join(
                render(
                    '<option value="{v}"{s}>last {v}</option>',
                    v=key,
                    s=Markup(" selected") if key == window else EMPTY,
                )
                for key in STORY_WINDOWS
            ),
        )

    def page_story(self, request: Request, conn: sqlite3.Connection) -> Markup:
        url = request.get("url")
        window = request.get("since", STORY_LINK_WINDOW)
        if window not in STORY_WINDOWS:
            window = STORY_LINK_WINDOW
        story = by_url(self.stories(conn, window)).get(url) if url else None
        if story is None:
            raise HttpError(
                "404 Not Found", f"No headline with that URL in the last {window} of stories."
            )
        lone = (
            Markup('<p class="muted">Only one outlet has reported this so far.</p>')
            if len(story.sources) < 2
            else EMPTY
        )
        return render(
            """<h1>{title}</h1>
<p class="meta"><strong>{n} outlet(s)</strong> {tags}</p>
{lone}
<h2>{count} headline(s), oldest first</h2>
<ol class="members wide">{members}</ol>""",
            title=story.title,
            n=len(story.sources),
            tags=self.tag_counts(story),
            lone=lone,
            count=len(story.headlines),
            members=self.story_members(request, conn, story),
        )

    # -- Trends

    def page_trends(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        window = request.get("since", "7d")
        if window not in TREND_WINDOWS:
            window = "7d"
        clock = Clock(request.utc)
        days = TREND_WINDOWS[window]
        today = clock.shown(utcnow()).date()
        day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        start_local = datetime.combine(day_list[0], datetime.min.time())
        start_local = start_local.replace(tzinfo=clock.shown(utcnow()).tzinfo)
        since = start_local.astimezone(UTC)
        wanted = {name.casefold() for name in filters.named} if filters.named is not None else None

        def keep(source: str) -> bool:
            return wanted is None or source.casefold() in wanted

        # Articles per source per local day, and live blogs per day.
        per_day: dict[str, Counter[Any]] = {}
        live_per_day: Counter[Any] = Counter()
        for row in first_seen(conn, since=since):
            if not keep(row.source):
                continue
            day = clock.shown(row.fetched_at).date()
            per_day.setdefault(row.source, Counter())[day] += 1
            live_per_day[day] += int(row.is_live)
        heat = self.heatmap(request, per_day, live_per_day, day_list)

        stats = sorted(
            (stat for stat in rewrite_stats(conn, since=since) if keep(stat.source)),
            key=lambda stat: (stat.share, stat.rewritten),
            reverse=True,
        )
        rewrite_rows = join(
            render(
                """<tr><td><a href="{h}">{source}</a></td><td class="num">{articles}</td>
<td class="num">{rewritten}</td><td><span class="barwrap">{bar}<span>{pct}</span></span></td>
<td class="num">{delay}</td></tr>""",
                h=self.link(request, "/rewrites", source=stat.source, tag=None),
                source=stat.source,
                articles=stat.articles,
                rewritten=stat.rewritten,
                bar=bar(stat.share),
                pct=f"{stat.share:.0%}",
                delay=_duration(stat.median_delay),
            )
            for stat in stats
        )

        turnover = sorted(
            (row for row in feed_turnover(conn, since=since) if keep(row.source)),
            key=lambda row: (row.share_new, row.all_new),
            reverse=True,
        )
        turnover_rows = join(
            render(
                """<tr><td>{source}</td><td class="num">{ok}</td>
<td class="num {fcss}">{failed}</td><td class="num">{skipped}</td>
<td class="num">{per_run}</td><td><span class="barwrap">{bar}<span>{pct}</span></span></td>
<td class="num {acss}">{all_new}</td></tr>""",
                source=row.source,
                ok=row.ok,
                failed=row.failed,
                fcss="bad" if row.failed else "",
                skipped=row.skipped,
                per_run=f"{row.found / row.ok:.0f}" if row.ok else "-",
                bar=bar(row.share_new),
                pct=f"{row.share_new:.0%}" if row.ok else "-",
                all_new=row.all_new,
                acss="warn" if row.ok and row.all_new * 2 >= row.ok else "",
            )
            for row in turnover
        )
        return render(
            """<h1>Trends</h1>
{form}
<h2>Articles per day</h2>
<p class="muted">New articles by the day they were first fetched ({zone}). Darker is more.</p>
{heat}
<h2>Rewrites by outlet</h2>
<p class="muted">Of the articles first seen in this period (live blogs excluded), how many
were reworded later. Punctuation-only changes don't count. The delay is from when the article
was first fetched to when the new wording was, so it can't be shorter than the time between
fetch runs.</p>
<div class="scroll"><table>
<thead><tr><th>Source</th><th class="num">Articles</th><th class="num">Rewritten</th>
<th>Share</th><th class="num">Median delay</th></tr></thead>
<tbody>{rewrite_rows}</tbody></table></div>
<h2>Feed turnover and reliability</h2>
<p class="muted">How much of each feed is new at each run. A feed that is entirely new run
after run (<span class="warn">highlighted</span> when it happens in half the runs or more) is
probably dropping stories between runs, so fetching more often would catch more. Each
source's first-ever run is left out.</p>
<div class="scroll"><table>
<thead><tr><th>Source</th><th class="num">OK</th><th class="num">Failed</th>
<th class="num">Skipped</th><th class="num">Items/run</th><th>New per run</th>
<th class="num">Runs all new</th></tr></thead>
<tbody>{turnover_rows}</tbody></table></div>""",
            form=self.filter_form(
                request,
                filters,
                since=None,
                extra=render(
                    '<label>Period <select name="since">{o}</select></label>',
                    o=join(
                        render(
                            '<option value="{v}"{s}>last {v}</option>',
                            v=key,
                            s=Markup(" selected") if key == window else EMPTY,
                        )
                        for key in TREND_WINDOWS
                    ),
                ),
            ),
            zone=clock.zone,
            heat=heat,
            rewrite_rows=rewrite_rows or Markup('<tr><td colspan="5">No articles yet.</td></tr>'),
            turnover_rows=turnover_rows
            or Markup('<tr><td colspan="7">No runs logged yet.</td></tr>'),
        )

    def heatmap(
        self,
        request: Request,
        per_day: dict[str, Counter[Any]],
        live_per_day: Counter[Any],
        days: list[Any],
    ) -> Markup:
        if not per_day:
            return Markup('<p class="empty">No articles in this period.</p>')
        peak = max(count for counts in per_day.values() for count in counts.values())
        totals_by_day: Counter[Any] = Counter()
        for counts in per_day.values():
            totals_by_day.update(counts)

        def cell(count: int) -> Markup:
            level = 0 if count == 0 else max(1, math.ceil(HEAT_LEVELS * count / peak))
            return render('<td class="heat h{l}" title="{n}">{n}</td>', l=level, n=count or "")

        ordered = sorted(per_day.items(), key=lambda item: -sum(item[1].values()))
        rows = join(
            render(
                '<tr><th scope="row"><a href="{h}">{source}</a></th>{cells}'
                '<td class="num">{total}</td></tr>',
                h=self.link(request, "/", source=source, tag=None),
                source=source,
                cells=join(cell(counts.get(day, 0)) for day in days),
                total=sum(counts.values()),
            )
            for source, counts in ordered
        )
        head = join(
            render(
                '<th class="day" title="{full}">{d}</th>',
                full=day.isoformat(),
                d=day.strftime("%a %-d"),
            )
            for day in days
        )
        footer = render(
            '<tr class="sum"><th scope="row">All sources</th>{t}<td class="num">{all}</td></tr>'
            '<tr class="sum"><th scope="row">of which live blogs</th>{l}'
            '<td class="num">{lt}</td></tr>',
            t=join(render('<td class="num">{n}</td>', n=totals_by_day.get(day, 0)) for day in days),
            all=sum(totals_by_day.values()),
            l=join(render('<td class="num">{n}</td>', n=live_per_day.get(day, 0)) for day in days),
            lt=sum(live_per_day.values()),
        )
        return render(
            '<div class="scroll"><table class="heatmap"><thead><tr><th>Source</th>{head}'
            '<th class="num">Total</th></tr></thead><tbody>{rows}</tbody><tfoot>{footer}</tfoot>'
            "</table></div>",
            head=head,
            rows=rows,
            footer=footer,
        )


def source_state(enabled: bool, status: SourceStatus, now: datetime) -> tuple[str, str]:
    """A source's health as (state, css class), from its last fetches."""
    if not enabled:
        return "disabled", "muted"
    if status.last_status == "error":
        return "failed", "bad"
    if status.last_status == "skipped":
        return "skipped", "warn"
    if status.last_success is None:
        return "never fetched", "warn"
    if now - status.last_success > STALE_AFTER:
        return "stale", "warn"
    if status.newest_item is not None and now - status.newest_item > CONTENT_STALE_AFTER:
        return "content stale", "warn"
    return "ok", "good"


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def bar(share: float) -> Markup:
    """A small horizontal bar for a 0..1 share, as inline SVG (no inline CSS needed)."""
    width = max(0.0, min(1.0, share)) * 100
    return render(
        '<svg class="bar" viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true">'
        '<rect class="track" width="100" height="8"/><rect class="fill" width="{w}" height="8"/>'
        "</svg>",
        w=f"{width:.1f}",
    )


def _duration(value: timedelta | None) -> str:
    if value is None:
        return "-"
    hours = value.total_seconds() / 3600
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.1f} d"


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
