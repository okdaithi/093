"""Read-only web viewer for the headline database: `headliner web`.

A small WSGI app on the standard library, so the deployment gains no
dependencies. It opens the database read-only on every request and never
writes; the fetch timer stays the only writer. Pages are plain HTML and CSS:
Briefing, Latest, Stories, Rewrites, Search, Trends, Sources, and one page per
article and per story. `static/app.js` only enhances them (instant filters,
"new since your last visit"); every page works without it.

Times follow the CLI: local time (the process's `TZ` or system zone) with the
zone named, UTC on request (`?utc=1`), and the UTC ISO timestamp on every
`<time>` element.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import re
import socketserver
import sqlite3
import sys
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from importlib import resources
from pathlib import Path
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from headliner import build as build_info
from headliner import charts, local_timezone, network, rewrites, tz_abbrev
from headliner.backup import default_dir, list_backups
from headliner.config import Config, ConfigError, load_config
from headliner.markup import EMPTY, Markup, esc, join, render
from headliner.models import Headline, is_minor_change, utcnow
from headliner.store import (
    SCHEMA_VERSION,
    FirstSeen,
    LiveFilter,
    Revision,
    RewriteStat,
    SourceStatus,
    TitleChange,
    Totals,
    article_history,
    connect_readonly,
    count_new,
    counts_by_source,
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
from headliner.stories import MAX_STORY_HEADLINES, Story, by_url, cluster, spans, words

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
# The Briefing: stories active in this window, the top few, then a few per country.
BRIEFING_WINDOW: Final = timedelta(hours=12)
BRIEFING_TOP: Final = 8
BRIEFING_PER_COUNTRY: Final = 3
# Days of history behind the Briefing's "typical day" line.
BRIEFING_BASELINE_DAYS: Final = 7
# The watchdog runs every 10 minutes; this long without a report is itself a problem.
WATCHDOG_STALE_AFTER: Final = timedelta(minutes=35)
NAV_PRIMARY: Final = (
    ("/", "Briefing"),
    ("/latest", "Latest"),
    ("/stories", "Stories"),
    ("/trends", "Trends"),
)
NAV_MORE: Final = (("/rewrites", "Rewrites"), ("/search", "Search"), ("/sources", "Sources"))
# Rewrites read at most for the kinds chart and the ?kind= filter.
REWRITE_CHART_LIMIT: Final = 5000
DELAY_BUCKETS: Final = (
    ("< 15 min", timedelta(minutes=15)),
    ("15\N{EN DASH}60 min", timedelta(hours=1)),
    ("1\N{EN DASH}3 h", timedelta(hours=3)),
    ("3\N{EN DASH}12 h", timedelta(hours=12)),
    ("12\N{EN DASH}24 h", timedelta(hours=24)),
    ("> 1 day", timedelta.max),
)
STORY_TIMELINE_MIN_SPAN: Final = timedelta(hours=2)
STORY_TIMELINE_TICKS: Final = 4
TREND_WINDOWS: Final = {"7d": 7, "14d": 14, "30d": 30}
HEAT_LEVELS: Final = 5
# Trends: heatmap rows shown before "show all", rewrite sample size worth ranking,
# rising topics listed, and how many outlets a term needs in the last day.
HEAT_TOP_ROWS: Final = 20
MIN_REWRITE_SAMPLE: Final = 10
RISING_TOP: Final = 15
RISING_MIN_COUNT: Final = 3
RISING_MIN_SOURCES: Final = 2
BIG_STORIES: Final = 10
# Source profile: days covered, stories it broke, distinctive words listed.
PROFILE_DAYS: Final = 14
PROFILE_STORIES: Final = 5
PROFILE_WORDS: Final = 20
PROFILE_MIN_WORD: Final = 3

SECURITY_HEADERS: Final = [
    (
        "Content-Security-Policy",
        "default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; "
        "img-src 'self' data:; form-action 'self'; "
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
        return value.astimezone(UTC) if self.utc else value.astimezone(local_timezone())

    @property
    def zone(self) -> str:
        return "UTC" if self.utc else tz_abbrev(local_timezone())

    def time(self, value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> Markup:
        """A `<time>` element; its title and datetime attribute carry UTC."""
        if value is None:
            return Markup('<span class="muted">-</span>')
        shown = self.shown(value)
        label = shown.strftime(fmt)
        # Across a daylight-saving change the zone differs per value; say which.
        zone = tz_abbrev(shown)
        if not self.utc and zone != self.zone:
            label += f" {zone}"
        utc_iso = value.astimezone(UTC).isoformat(timespec="seconds")
        return render(
            '<time datetime="{iso}" title="{title}">{label}</time>',
            iso=utc_iso,
            title=utc_iso.replace("+00:00", "Z"),
            label=label,
        )

    def day(self, value: datetime) -> str:
        shown = self.shown(value)
        return f"{shown.strftime('%A')} {shown.day} {shown.strftime('%B %Y')}"

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
        static = resources.files("headliner").joinpath("static")
        self._static = {
            "/static/app.css": (static.joinpath("app.css").read_bytes(), "text/css"),
            "/static/app.js": (static.joinpath("app.js").read_bytes(), "text/javascript"),
        }
        self.routes: dict[str, Callable[[Request, sqlite3.Connection], Markup]] = {
            "/": self.page_briefing,
            "/latest": self.page_latest,
            "/rewrites": self.page_rewrites,
            "/search": self.page_search,
            "/sources": self.page_sources,
            "/article": self.page_article,
            "/stories": self.page_stories,
            "/story": self.page_story,
            "/source": self.page_source,
            "/trends": self.page_trends,
        }
        self._story_cache: dict[tuple[object, ...], list[Story]] = {}
        self._story_lock = threading.Lock()
        self._publishers: tuple[Config | None, dict[str, str]] = (None, {})
        self._watchdog: tuple[int, dict[str, Any]] = (0, {})
        self.build = build_info.load()
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
        if request.path in self._static:
            content, kind = self._static[request.path]
            return Response(
                content,
                content_type=f"{kind}; charset=utf-8",
                headers=[("Cache-Control", "max-age=3600")],
            )
        if request.path == "/healthz":
            return self.healthz()
        if request.path == "/api/status":
            return self.api_status()
        if request.path == "/api/new":
            return self.api_new(request)
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
                    "build": self.build.raw if self.build else None,
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
        """Everything the status check needs, as JSON, without sudo."""
        try:
            with self.connection() as conn:
                payload = self.status_report(conn)
        except HttpError as exc:
            return Response(
                json.dumps({"status": "error", "error": exc.message}).encode(),
                status=exc.status,
                content_type="application/json",
                headers=[("Cache-Control", "no-store")],
            )
        return Response(
            json.dumps(payload, indent=2).encode(),
            content_type="application/json",
            headers=[("Cache-Control", "no-store")],
        )

    def status_report(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """Runs, source health, database and backups, for /api/status and the Briefing.

        `status` is "ok", or "attention" with the reasons in `checks`. A source
        that is skipped (robots.txt) is listed but does not need attention.
        """
        now = utcnow()
        checks: list[str] = []
        info = self.totals(conn)
        version = schema_version(conn)
        runs = recent_runs(conn, limit=8)
        config = self.config
        sources: dict[str, Any] | None = None
        problems: list[dict[str, Any]] = []
        network_down = False
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
            failed = [p for p in problems if p["state"] == "failed"]
            # Nearly every source failing with DNS/routing errors is the machine's
            # network (or VPN), not that many broken feeds.
            network_down = len(failed) >= max(3, sources["enabled"] * 0.8) and all(
                network.is_network_error(p["error"]) for p in failed
            )
            if attention and not network_down:
                checks.append(f"{len(attention)} source(s) need attention")
        else:
            checks.append(f"sources file not loaded: {self._config_error}")
        if not runs:
            checks.append("no fetch runs logged")
        else:
            if now - runs[0].finished_at > RUN_LATE_AFTER:
                checks.append(f"last run finished {runs[0].finished_at:%Y-%m-%d %H:%M}Z")
            if network_down:
                checks.append(
                    f"network down: the {runs[0].started_at:%H:%M}Z run failed on "
                    f"{runs[0].failed} source(s) with DNS or connection errors "
                    "(check ProtonVPN/Tailscale DNS), not a feed problem"
                )
            elif runs[0].failed:
                checks.append(f"last run: {runs[0].failed} source(s) failed")
        backups = list_backups(default_dir(self.db_path), self.db_path.stem)
        if not backups:
            checks.append("no backups")
        elif now - backups[0].taken_at > BACKUP_LATE_AFTER:
            checks.append(f"latest backup is from {backups[0].taken_at:%Y-%m-%d}")
        return {
            "status": "attention" if checks else "ok",
            "network_down": network_down,
            "watchdog": self.watchdog_problems(),
            "checks": checks,
            "checked_at": now.isoformat(timespec="seconds"),
            "schema": version,
            "build": self.build.raw if self.build else None,
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

    def api_new(self, request: Request) -> Response:
        """How many articles arrived after `since` (an ISO time): the "N new" nav count."""
        try:
            since = datetime.fromisoformat(request.get("since").replace("Z", "+00:00"))
        except ValueError:
            since = None
        if since is None or since.tzinfo is None:
            payload: dict[str, Any] = {"error": "since must be an ISO time with a zone"}
            status = "400 Bad Request"
        else:
            try:
                with self.connection() as conn:
                    payload = {"since": since.isoformat(), "latest": count_new(conn, since=since)}
                    status = "200 OK"
            except HttpError as exc:
                payload, status = {"error": exc.message}, exc.status
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

        def tab(path: str, label: str, css: str = "") -> Markup:
            return render(
                '<a href="{href}"{key}{cls}{current}>{label}</a>',
                href=self.link(request, path),
                # Only the always-visible copy carries data-nav (app.js badges it).
                key=EMPTY if css else render(' data-nav="{k}"', k=path.strip("/") or "briefing"),
                cls=render(' class="{c}"', c=css) if css else EMPTY,
                current=Markup(' aria-current="page"') if request.path == path else EMPTY,
                label=label,
            )

        current_more = next((label for path, label in NAV_MORE if path == request.path), "")
        # On phones the less-used pages fold under "More"; wide screens show every tab.
        nav = render(
            '{primary}{wide}<details class="more narrow"><summary{cur}>More</summary>'
            '<div class="more-menu">{folded}</div></details>',
            primary=join(tab(path, label) for path, label in NAV_PRIMARY),
            wide=join(tab(path, label, "wide") for path, label in NAV_MORE),
            cur=render(' class="current" title="Now on {c}"', c=current_more)
            if current_more
            else EMPTY,
            folded=join(tab(path, label, "folded") for path, label in NAV_MORE),
        )
        other = Clock(False).zone if request.utc else "UTC"
        switch = self.link(
            request,
            request.path or "/",
            utc=not request.utc,
            page=request.page if request.page > 1 else None,
        )
        toggle = render(
            '<a class="zone" href="{href}" title="Times are in {zone}; switch to {other}">'
            "{zone} → {other}</a>",
            href=switch,
            zone=clock.zone,
            other=other,
        )
        warning = (
            render('<p class="notice">{e}</p>', e=f"Sources file problem: {self._config_error}")
            if self._config_error
            else Markup("")
        )
        warning = join([warning, self.watchdog_banner(clock)])
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
<script src="/static/app.js" defer></script>
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
<footer>Read-only view of the headliner database. Times in {zone}; hover a time for UTC,
or <a href="{switch}">show times in {other}</a>.
<span class="build">{build}</span></footer>
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
            switch=switch,
            other=other,
            build=self.build_line(request, clock),
        )

    def watchdog_state(self) -> dict[str, Any]:
        """The watchdog's saved state (next to the database), re-read only when it changes."""
        path = self.db_path.parent / "watchdog.json"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return {}
        if self._watchdog[0] != stamp:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._watchdog = (stamp, data if isinstance(data, dict) else {})
        return self._watchdog[1]

    def watchdog_problems(self) -> list[dict[str, str]]:
        """Announced problems (still failing) and whether the watchdog itself has gone quiet."""
        state = self.watchdog_state()
        if not state:
            return []
        problems = [
            {
                "check": name,
                "level": entry["level"],
                "detail": entry.get("detail", ""),
                "since": entry.get("since", ""),
            }
            for name, entry in sorted(state.get("checks", {}).items())
            if isinstance(entry, dict)
            and entry.get("level") in {"warn", "fail"}
            and entry.get("notified_level", "ok") != "ok"
        ]
        try:
            updated = datetime.fromisoformat(state["updated_at"])
        except (KeyError, ValueError):
            return problems
        if utcnow() - updated > WATCHDOG_STALE_AFTER:
            problems.append(
                {
                    "check": "watchdog",
                    "level": "fail",
                    "detail": "the health watchdog has not reported",
                    "since": state["updated_at"],
                }
            )
        return problems

    @staticmethod
    def since_label(value: str, clock: Clock) -> Markup | str:
        """A watchdog timestamp for the banner; a damaged state file must not break pages."""
        try:
            return clock.time(datetime.fromisoformat(value), "%a %H:%M")
        except ValueError:
            return "unknown"

    def watchdog_banner(self, clock: Clock) -> Markup:
        """A notice on every page while the watchdog has an announced problem."""
        problems = self.watchdog_problems()
        if not problems:
            return EMPTY
        items = join(
            render(
                "<li><strong>{check}</strong>: {detail} (since {since})</li>",
                check=problem["check"],
                detail=problem["detail"],
                since=self.since_label(problem["since"], clock),
            )
            for problem in problems
        )
        return render('<div class="notice bad" role="alert"><ul>{i}</ul></div>', i=items)

    def build_line(self, request: Request, clock: Clock) -> Markup:
        """The footer's "which code is this" line."""
        build = self.build
        if build is None:
            return render(
                '<a href="{href}">Development build</a> (not installed by the deploy script).',
                href=self.link(request, "/sources") + "#build",
            )
        pr = render(" · {link}", link=self.pr_link(build)) if build.pr_number else Markup("")
        return render(
            '<a href="{href}">Build <code>{short}</code></a>{dirty}{pr}{built}',
            href=self.link(request, "/sources") + "#build",
            short=build.short,
            dirty=Markup(' <span class="warn">+ local changes</span>')
            if build.dirty
            else Markup(""),
            pr=pr,
            built=render(
                " · installed {when} ({ago})",
                when=clock.time(build.built_at, "%a %-d %b %H:%M"),
                ago=clock.ago(build.built_at),
            )
            if build.built_at
            else Markup(""),
        )

    @staticmethod
    def pr_link(build: build_info.Build) -> Markup:
        title = f" {build.pr_title}" if build.pr_title else ""
        if not build.pr_url:
            return render("PR #{n}{t}", n=build.pr_number, t=title)
        return render(
            '<a href="{href}" rel="noreferrer">PR #{n}</a>{t}',
            href=build.pr_url,
            n=build.pr_number,
            t=title,
        )

    def build_details(self, conn: sqlite3.Connection, clock: Clock) -> Markup:
        """The Sources page's "About this build" block."""
        build = self.build
        rows: list[tuple[str, Markup | str]] = []
        if build is None:
            rows.append(("Build", "Development build: no build record (headliner/_build.json)."))
        else:
            rows.append(
                (
                    "Commit",
                    render(
                        "<code>{c}</code>{d}",
                        c=build.commit or "unknown",
                        d=" (with local changes)" if build.dirty else "",
                    ),
                )
            )
            if build.branch:
                rows.append(("Branch", build.branch))
            if build.committed_at:
                rows.append(("Committed", clock.time(build.committed_at)))
            if build.pr_number is not None:
                rows.append(("Pull request", self.pr_link(build)))
            if build.merge_commit:
                rows.append(
                    (
                        "Latest merge",
                        render(
                            "<code>{c}</code> {when}",
                            c=build.merge_commit[:7],
                            when=clock.time(build.merge_at) if build.merge_at else "",
                        ),
                    )
                )
            if build.built_at:
                rows.append(
                    (
                        "Installed",
                        render(
                            "{when} ({ago})",
                            when=clock.time(build.built_at),
                            ago=clock.ago(build.built_at),
                        ),
                    )
                )
        rows.append(("Python", sys.version.split()[0]))
        rows.append(("Database schema", str(schema_version(conn))))
        return render(
            '<h2 id="build">About this build</h2>\n<dl class="build-info">{rows}</dl>',
            rows=join(render("<dt>{k}</dt><dd>{v}</dd>", k=k, v=v) for k, v in rows),
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
        """The filter bar: grouped tag checkboxes, source picker, time window.

        Tags and source sit in a collapsible panel (closed on phones; app.js
        opens it on wider screens), with the active ones repeated as removable
        chips below so a closed panel still says what is filtered.
        """
        config = self.config
        all_tags = config.all_tags if config else []

        def boxes(tags: Iterable[str]) -> Markup:
            return join(
                render(
                    '<label class="chip"><input type="checkbox" name="tag" value="{tag}"{checked}>'
                    "{tag}</label>",
                    tag=tag,
                    checked=Markup(" checked") if tag in filters.tags else EMPTY,
                )
                for tag in tags
            )

        countries = [tag for tag in all_tags if is_country(tag)]
        others = [tag for tag in all_tags if not is_country(tag)]
        groups = [
            render(
                '<fieldset class="tags"><legend>{label}</legend>{boxes}</fieldset>',
                label=label,
                boxes=boxes(tags),
            )
            for label, tags in (("Countries", countries), ("Regions & topics", others))
            if tags
        ]
        tag_boxes = join(groups) or Markup('<span class="muted">No tags configured.</span>')
        source_select = render(
            '<label>Source <select name="source"><option value="">All sources</option>'
            "{options}</select></label>",
            options=self.source_options(filters.source),
        )
        since_select = EMPTY
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
                        sel=Markup(" selected") if key == chosen else EMPTY,
                    )
                    for key in SINCE_CHOICES
                ),
            )
        hidden = join(
            render('<input type="hidden" name="{k}" value="{v}">', k=key, v=value)
            for key in ("utc", *keep)
            for value in request.get_all(key)
        )
        active = len(filters.tags) + bool(filters.source)
        notices = join(render('<p class="notice">{n}</p>', n=note) for note in filters.notices)
        return render(
            """<form class="filters" method="get" action="{action}">
  {extra}
  <details class="panel">
    <summary>Tags &amp; sources{count}</summary>
    {tags}
    {source}
  </details>
  {since}{hidden}
  <button type="submit">Apply</button>
  <a class="reset" href="{reset}">Reset</a>
</form>
{chips}{notices}""",
            action=request.path,
            extra=extra,
            count=render(' <span class="count">{n} active</span>', n=active) if active else EMPTY,
            tags=tag_boxes,
            source=source_select,
            since=since_select,
            hidden=hidden,
            reset=request.path + ("?utc=1" if request.utc else ""),
            chips=self.active_chips(request, filters),
            notices=notices,
        )

    def source_options(self, chosen: str) -> Markup:
        """`<option>`s for every source, grouped by country (a source's first country tag)."""
        config = self.config
        grouped: dict[str, list[str]] = {}
        for source in config.sources if config else []:
            country = next((tag for tag in source.tags if is_country(tag)), "Other")
            grouped.setdefault(country, []).append(source.name)
        known = {name for names in grouped.values() for name in names}
        orphan = (
            render('<option value="{n}" selected>{n}</option>', n=chosen)
            if chosen and chosen not in known
            else EMPTY
        )
        groups = join(
            render(
                '<optgroup label="{label}">{options}</optgroup>',
                label=country,
                options=join(
                    render(
                        '<option value="{name}"{sel}>{name}</option>',
                        name=name,
                        sel=Markup(" selected") if name == chosen else EMPTY,
                    )
                    for name in sorted(names, key=str.casefold)
                ),
            )
            for country, names in sorted(
                grouped.items(), key=lambda item: (item[0] == "Other", item[0])
            )
        )
        return join([orphan, groups])

    def active_chips(self, request: Request, filters: Filters) -> Markup:
        """The active tag and source filters as chips; following one removes it."""
        chips = [
            render(
                '<a class="chip on" href="{h}" title="Remove this filter">{t} &times;</a>',
                h=self.link(
                    request, request.path, tag=[t for t in filters.tags if t != tag] or None
                ),
                t=tag,
            )
            for tag in filters.tags
        ]
        if filters.source:
            chips.append(
                render(
                    '<a class="chip on" href="{h}" title="Remove this filter">{s} &times;</a>',
                    h=self.link(request, request.path, source=None),
                    s=filters.source,
                )
            )
        if not chips:
            return EMPTY
        return render('<p class="active">Filtered by {c}</p>', c=join(chips, " "))

    def since(self, request: Request, default: str) -> datetime | None:
        key = request.get("since", default)
        window = SINCE_CHOICES.get(key, SINCE_CHOICES[default])
        return utcnow() - window if window else None

    def day_range(self, request: Request) -> tuple[datetime, datetime] | None:
        """`day=YYYY-MM-DD` as a [start, end) range in UTC, midnight to midnight shown time."""
        try:
            day = date.fromisoformat(request.get("day", ""))
        except ValueError:
            return None
        zone = Clock(request.utc).shown(utcnow()).tzinfo
        start = datetime.combine(day, time.min, tzinfo=zone)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        return start.astimezone(UTC), end.astimezone(UTC)

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
        if story is not None and self.reach(story) > 1:
            parts.append(
                render(
                    '<a class="badge outlets" href="{h}" title="{t}">{n} outlets</a>',
                    h=self.link(request, "/story", url=headline.url),
                    n=self.reach(story),
                    t=self.reach_note(story),
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
                    """<li class="item" data-seen="{seen}">
  <div class="meta">{time} <a class="source" href="{src_href}">{source}</a> {badges}</div>
  {title}
  {earlier}{summary}
</li>""",
                    seen=_iso_or_none(headline.fetched_at),
                    time=clock.time(when, "%H:%M" if group_by_day else "%Y-%m-%d %H:%M"),
                    src_href=self.link(
                        request, "/latest", source=headline.source, tag=None, q=None
                    ),
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
        day_range = self.day_range(request)
        rows = list_headlines(
            conn,
            since=None if day_range else self.since(request, "all"),
            source=filters.source or None,
            sources=filters.sources,
            limit=PAGE_SIZE + 1,
            offset=(page - 1) * PAGE_SIZE,
            fetched_since=day_range[0] if day_range else None,
            fetched_until=day_range[1] if day_range else None,
        )
        has_more = len(rows) > PAGE_SIZE
        day_note = (
            render(
                '<p class="notice">First fetched on {d}. <a href="{all}">Show all days</a>.</p>',
                d=request.get("day"),
                all=self.link(request, "/latest", day=None),
            )
            if day_range
            else EMPTY
        )
        return render(
            "<h1>Latest headlines</h1>{form}{note}{items}{pager}",
            note=day_note,
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
        kind = request.get("kind")
        if kind not in rewrites.KINDS:
            kind = ""
        # Kinds are judged in Python, so the period's changes are read in one go
        # for the chart (and for the list when filtering by kind).
        period = list_title_changes(
            conn, **common, live=live, oldest_first=oldest, minor=minor, limit=REWRITE_CHART_LIMIT
        )
        kinds = {
            (change.url, change.new_title): rewrites.classify(change.old_title, change.new_title)
            for change in period
            if change.old_title is not None
        }
        if kind:
            matching = [c for c in period if kinds.get((c.url, c.new_title)) == kind]
            start = (page - 1) * PAGE_SIZE
            changes = matching[start : start + PAGE_SIZE + 1]
        else:
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
            "<h1>Rewritten headlines</h1>{form}{intro}{note}{minor_note}{charts}{items}{pager}",
            form=self.filter_form(request, filters, since="7d", extra=live_select),
            intro=intro,
            note=note,
            minor_note=minor_note,
            charts=self.rewrite_charts(request, conn, filters, since, list(kinds.values()), kind),
            items=self.change_items(request, changes[:PAGE_SIZE], kinds),
            pager=self.pager(request, has_more),
        )

    def rewrite_charts(
        self,
        request: Request,
        conn: sqlite3.Connection,
        filters: Filters,
        since: datetime | None,
        kinds: Sequence[str],
        current: str,
    ) -> Markup:
        """What kinds of rewrite, and how long after publication they come."""
        if not kinds:
            return EMPTY
        counts = Counter(kinds)
        chips = join(
            render(
                '<li><a href="{h}"{cur}><svg class="swatch" viewBox="0 0 10 10" '
                'aria-hidden="true"><rect class="c-key k{i}" width="10" height="10"/></svg>'
                '{k} <span class="muted">{n}</span></a></li>',
                h=self.link(request, "/rewrites", kind=None if name == current else name),
                cur=Markup(' aria-current="true"') if name == current else EMPTY,
                i=index,
                k=name,
                n=counts[name],
            )
            for index, name in enumerate(rewrites.KINDS)
        )
        donut = charts.donut(
            [(name, counts[name], f"k{index}") for index, name in enumerate(rewrites.KINDS)],
            label="Likely kind of each rewrite",
            legend=render('<ul class="key">{c}</ul>', c=chips),
        )
        stats = rewrite_stats(conn, since=since or utcnow() - timedelta(days=7))
        wanted = {name.casefold() for name in filters.sources} if filters.sources else None
        if filters.source:
            wanted = {filters.source.casefold()}
        delays = [
            delay
            for stat in stats
            if wanted is None or stat.source.casefold() in wanted
            for delay in stat.delays
        ]
        buckets = [0] * len(DELAY_BUCKETS)
        for delay in delays:
            index = next(
                (i for i, (_, limit) in enumerate(DELAY_BUCKETS) if delay < limit),
                len(DELAY_BUCKETS) - 1,
            )
            buckets[index] += 1
        histogram = (
            charts.histogram(
                buckets,
                [name for name, _ in DELAY_BUCKETS],
                label="Time from first seen to first rewrite",
            )
            if delays
            else EMPTY
        )
        rules = join(
            render("<li><strong>{k}</strong>: {n}</li>", k=name, n=rewrites.KIND_NOTES[name])
            for name in rewrites.KINDS
        )
        return render(
            """<section class="rewrite-charts">
<div><h2>Kinds of rewrite</h2>{donut}
<details class="muted"><summary>How kinds are judged</summary><p>From the two titles alone,
so treat them as likely, not certain. Click a kind to list only those.</p><ul>{rules}</ul>
</details></div>
<div><h2>How soon they come</h2><p class="muted">From when an article was first seen to its
first rewrite that changed words; live blogs excluded.</p>{histogram}</div>
</section>""",
            donut=donut,
            rules=rules,
            histogram=histogram,
        )

    def change_items(
        self,
        request: Request,
        changes: Sequence[TitleChange],
        kinds: dict[tuple[str, str], str] | None = None,
    ) -> Markup:
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
  <div class="meta">{time} <a class="source" href="{src}">{source}</a> {live}{minor}{kind}
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
                    kind=render(' <span class="badge kind">{k}</span>', k=kind)
                    if (kind := (kinds or {}).get((change.url, change.new_title)))
                    else EMPTY,
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
  <td><a href="{profile}">{name}</a> <span class="muted small">{type}</span>{group}</td>
  <td>{tags}</td>
  <td class="num">{items}</td>
  <td>{success} <span class="muted small">{ago}</span></td>
  <td><span class="state {css}" title="{error}">{state}</span>{detail}</td>
  <td>{feed}</td>
</tr>""",
                    profile=self.link(request, "/source", name=source.name, tag=None),
                    group=render(' <span class="chip small">{g}</span>', g=source.group)
                    if source.group
                    else EMPTY,
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
<div class="scroll"><table class="sortable">
<thead><tr><th>Source</th><th>Tags</th><th class="num">Items</th>
<th>Last success ({zone})</th><th>Last run</th><th>Feed</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<h2>Recent runs</h2>
<div class="scroll"><table>
<thead><tr><th>Started ({zone})</th><th class="num">Took</th><th class="num">OK</th>
<th class="num">Skipped</th><th class="num">Failed</th><th class="num">Found</th>
<th class="num">New</th><th class="num">Retitled</th><th>Failed sources</th></tr></thead>
<tbody>{run_rows}</tbody></table></div>
{build}""",
            build=self.build_details(conn, clock),
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
        story = by_url(self.stories(conn, STORY_LINK_WINDOW)).get(headline.url)
        trail = [
            ("Sources", self.link(request, "/sources", tag=None, source=None)),
            (
                headline.source,
                self.link(request, "/source", name=headline.source, tag=None, source=None),
            ),
        ]
        if story is not None and len(story.headlines) > 1:
            trail.append(
                ("Story", self.link(request, "/story", url=headline.url, tag=None, source=None))
            )
        return render(
            """{crumbs}<h1>{title}</h1>
<p class="meta">{source} {live} · published {published} · {open}</p>
<h2>{n} title(s), oldest first</h2>
<ol class="items timeline">{items}</ol>""",
            crumbs=self.crumbs(trail, headline.title),
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

    def publishers(self) -> dict[str, str]:
        """Source name (casefolded) -> publisher: its `group:`, or itself."""
        config = self.config
        if config is None:
            return {}
        if self._publishers[0] is not config:
            self._publishers = (
                config,
                {source.name.casefold(): source.publisher for source in config.sources},
            )
        return self._publishers[1]

    def reach(self, story: Story) -> int:
        """Independent outlets on a story: mastheads of one group count once."""
        publisher = self.publishers()
        return len({publisher.get(name.casefold(), name) for name in story.sources})

    def reach_note(self, story: Story) -> str:
        mastheads = len(story.sources)
        if mastheads == self.reach(story):
            return "Outlets reporting this"
        return f"{mastheads} mastheads; ones sharing a newsroom or copy count once"

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
                '<li><span class="meta">{time} <a class="source" href="{profile}">{source}</a>'
                " {badges}</span> {title}</li>",
                time=clock.time(headline.published_at or headline.fetched_at, "%a %H:%M"),
                profile=self.link(request, "/source", name=headline.source, tag=None),
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
            if self.reach(story) >= minimum
            and (filters.named is None or any(n.casefold() in wanted for n in story.sources))
        ]
        if newest:
            chosen.sort(key=lambda story: story.last_seen, reverse=True)
        else:
            chosen.sort(key=lambda story: (self.reach(story), story.last_seen), reverse=True)
        page = request.page
        shown = chosen[(page - 1) * STORIES_PAGE_SIZE : page * STORIES_PAGE_SIZE]
        scale = (
            (
                max(self.reach(story) for story in shown),
                min(story.first_seen for story in shown),
                max(story.last_seen for story in shown),
            )
            if shown
            else None
        )
        cards = join(self.story_card(request, conn, story, scale=scale) for story in shown)
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
Headlines are grouped by shared words, so the occasional grouping is wrong.
Under each story, the dark bar is its outlets against the most on this page and the light bar
when it was reported within the page's time span.</p>
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

    def story_card(
        self,
        request: Request,
        conn: sqlite3.Connection,
        story: Story,
        *,
        compact: bool = False,
        scale: tuple[int, datetime, datetime] | None = None,
    ) -> Markup:
        """One story: when, how many outlets (by tag), its title, and (unless compact) members."""
        clock = Clock(request.utc)
        members = (
            EMPTY
            if compact
            else render(
                '<details><summary>{count} headlines</summary><ol class="members">{m}</ol>'
                "</details>",
                count=len(story.headlines),
                m=self.story_members(request, conn, story),
            )
        )
        return render(
            """<li class="story{compact}" data-seen="{seen}">
  <div class="meta">{first} to {last} ·
    <strong title="{note}">{n} outlets</strong> {tags}</div>
  <h3><a href="{href}">{title}</a></h3>
  {spread}{members}
</li>""",
            spread=self.story_spread(story, scale, clock) if scale else EMPTY,
            compact=" compact" if compact else "",
            seen=_iso_or_none(min(headline.fetched_at for headline in story.headlines)),
            first=clock.time(story.first_seen, "%a %H:%M"),
            last=clock.time(story.last_seen, "%a %H:%M"),
            n=self.reach(story),
            note=self.reach_note(story),
            tags=self.tag_counts(story),
            href=self.link(request, "/story", url=(story.lead or story.headlines[0]).url),
            title=story.title,
            members=members,
        )

    def story_spread(
        self, story: Story, scale: tuple[int, datetime, datetime], clock: Clock
    ) -> Markup:
        most, start, end = scale
        span = (end - start).total_seconds() or 1.0
        lasted = story.last_seen - story.first_seen
        return charts.spread(
            self.reach(story) / (most or 1),
            (story.first_seen - start).total_seconds() / span,
            (story.last_seen - start).total_seconds() / span,
            title=f"{self.reach(story)} of up to {most} outlets on this page; "
            f"reported over {_duration(lasted) if lasted >= timedelta(minutes=1) else '1 min'}, "
            f"{clock.shown(story.first_seen):%a %H:%M} to {clock.shown(story.last_seen):%a %H:%M}",
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
            if self.reach(story) < 2
            else EMPTY
        )
        framing = (
            render(
                """<h2>How each outlet put it</h2>
<p class="muted">Oldest first, with the time after the first report. <span class="shared">Faded
words</span> are used by most outlets; <mark class="own">highlighted</mark> ones by this outlet
alone.</p>
<ol class="framing">{rows}</ol>""",
                rows=self.framing_rows(request, conn, story),
            )
            if len(story.headlines) > 1
            else EMPTY
        )
        return render(
            """{crumbs}<h1>{title}</h1>
<p class="meta"><strong title="{note}">{n} outlet(s)</strong> {tags}</p>
{lone}
{timeline}
{framing}
<details><summary>{count} headline(s) with times and sources</summary>
<ol class="members wide">{members}</ol></details>""",
            title=story.title,
            n=self.reach(story),
            note=self.reach_note(story),
            tags=self.tag_counts(story),
            lone=lone,
            timeline=self.story_timeline(request, story),
            crumbs=self.crumbs(
                [("Stories", self.link(request, "/stories", url=None, since=None))], story.title
            ),
            framing=framing,
            count=len(story.headlines),
            members=self.story_members(request, conn, story),
        )

    @staticmethod
    def crumbs(trail: Sequence[tuple[str, str]], here: str) -> Markup:
        """Where this page sits: links back up, then the page itself."""
        return render(
            '<nav class="crumbs" aria-label="Breadcrumb"><ol>{links}'
            '<li aria-current="page">{here}</li></ol></nav>',
            links=join(
                render('<li><a href="{h}">{t}</a></li>', h=href, t=text) for text, href in trail
            ),
            here=here,
        )

    def story_timeline(self, request: Request, story: Story) -> Markup:
        """When each outlet reported the story: one lane per publisher, a dot per headline."""
        if len(story.headlines) < 2:
            return EMPTY
        clock = Clock(request.utc)
        publisher = self.publishers()
        start = story.first_seen
        span = max(story.last_seen - start, STORY_TIMELINE_MIN_SPAN)
        # A margin either side keeps the first and last dots off the edges.
        start -= span * 0.05
        span *= 1.1
        lanes: dict[str, list[charts.Dot]] = {}
        first_url = min(story.headlines, key=lambda h: h.published_at or h.fetched_at).url
        for item in story.headlines:
            when = item.published_at or item.fetched_at
            lanes.setdefault(publisher.get(item.source.casefold(), item.source), []).append(
                charts.Dot(
                    at=(when - start) / span,
                    title=f"{clock.shown(when):%a %H:%M} · {item.source}: {item.title}",
                    href=self.link(request, "/article", url=item.url, tag=None, source=None),
                    css="first" if item.url == first_url else "",
                )
            )
        ticks = [
            f"{clock.shown(start + span * index / STORY_TIMELINE_TICKS):%a %H:%M}"
            for index in range(STORY_TIMELINE_TICKS + 1)
        ]
        names: dict[str, list[str]] = {}
        for item in story.headlines:
            key = publisher.get(item.source.casefold(), item.source)
            names.setdefault(key, [])
            if item.source not in names[key]:
                names[key].append(item.source)
        rows: list[tuple[Markup | str, list[charts.Dot]]] = [
            (
                render(
                    '<span title="{all}">{name} \N{MULTIPLICATION SIGN}{n}</span>',
                    all=", ".join(names[key]),
                    name=key,
                    n=len(names[key]),
                )
                if len(names[key]) > 1
                else names[key][0],
                dots,
            )
            for key, dots in lanes.items()
        ]
        label = f"When each outlet reported this, {clock.zone}"
        return render(
            "<h2>Timeline</h2>{chart}",
            chart=charts.swimlanes(rows, label=label, ticks=ticks),
        )

    def framing_rows(self, request: Request, conn: sqlite3.Connection, story: Story) -> Markup:
        """Each headline with the words most outlets share faded, and its own words marked."""
        headlines = story.headlines
        stems = [{stem for stem, _ in words(headline.title)} for headline in headlines]
        used_by: Counter[str] = Counter()
        for own in stems:
            used_by.update(own)
        total = len(headlines)
        counts = revision_counts(conn, [headline.url for headline in headlines])
        first = story.first_seen

        def marked(title: str) -> Markup:
            parts: list[object] = []
            at = 0
            for start, end, stem in spans(title):
                parts.append(title[at:start])
                text = title[start:end]
                if stem is None:
                    parts.append(text)
                elif used_by[stem] * 2 > total:
                    parts.append(render('<span class="shared">{t}</span>', t=text))
                elif used_by[stem] == 1:
                    parts.append(render('<mark class="own">{t}</mark>', t=text))
                else:
                    parts.append(text)
                at = end
            parts.append(title[at:])
            return join(parts)

        rows = []
        for index, headline in enumerate(headlines):
            when = headline.published_at or headline.fetched_at
            after = when - first
            rows.append(
                render(
                    '<li><span class="meta"><span class="after">{after}</span> '
                    '<a class="source" href="{profile}">{source}</a> {badges}</span>'
                    "<span>{title}</span></li>",
                    after="first"
                    if index == 0 or after < timedelta(minutes=1)
                    else "+" + _duration(after)
                    if after >= timedelta(hours=1)
                    else f"+{int(after.total_seconds() // 60)} min",
                    profile=self.link(request, "/source", name=headline.source, tag=None),
                    source=headline.source,
                    badges=self.badges(headline, counts.get(headline.url, 1), request),
                    title=external_link(headline.url, marked(headline.title)),
                )
            )
        return join(rows)

    # -- Source profiles

    def page_source(self, request: Request, conn: sqlite3.Connection) -> Markup:
        """One outlet: volume, publishing hours, rewrites, stories it broke, its words."""
        config = self.config
        wanted = request.get("name", "")
        source = next(
            (
                s
                for s in (config.sources if config else ())
                if s.name.casefold() == wanted.casefold()
            ),
            None,
        )
        if source is None:
            raise HttpError("404 Not Found", "No configured source by that name.")
        clock = Clock(request.utc)
        now = utcnow()
        days = PROFILE_DAYS
        today = clock.shown(now).date()
        day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        since = datetime.combine(day_list[0], time.min, tzinfo=clock.shown(now).tzinfo).astimezone(
            UTC
        )
        name = source.name.casefold()
        everyone = first_seen(conn, since=since)
        rows = [row for row in everyone if row.source.casefold() == name]
        per_day: Counter[Any] = Counter(clock.shown(row.fetched_at).date() for row in rows)
        heat = (
            count_strip(
                [
                    (
                        str(day.day),
                        f"{day.strftime('%a')} {day.day} {day.strftime('%b')}",
                        per_day[day],
                    )
                    for day in day_list
                ],
                "articles a day",
            )
            if rows
            else Markup('<p class="empty">No articles in this period.</p>')
        )
        hours = self.hour_strip(clock, rows)

        stat = next(
            (s for s in rewrite_stats(conn, since=since) if s.source.casefold() == name), None
        )
        rewrites = (
            render(
                "<p>{r} of {a} articles reworded ({pct}), median {d} after first fetch. "
                '<a href="{h}">See its rewrites →</a></p>',
                r=stat.rewritten,
                a=stat.articles,
                pct=f"{stat.share:.0%}",
                d=_duration(stat.median_delay),
                h=self.link(request, "/rewrites", source=source.name, tag=None, since="30d"),
            )
            if stat and stat.articles
            else Markup('<p class="empty">No articles in this period.</p>')
        )

        broke = sorted(
            (
                story
                for story in self.stories(conn, "7d")
                if self.reach(story) >= 2 and story.headlines[0].source.casefold() == name
            ),
            key=lambda story: (self.reach(story), story.last_seen),
            reverse=True,
        )[:PROFILE_STORIES]
        joined = sum(
            1
            for story in self.stories(conn, "7d")
            if self.reach(story) >= 2 and any(s.casefold() == name for s in story.sources)
        )
        first_cards = (
            render(
                '<ol class="stories">{c}</ol>',
                c=join(self.story_card(request, conn, story, compact=True) for story in broke),
            )
            if broke
            else Markup('<p class="empty">None in the last 7 days.</p>')
        )

        # Words this outlet uses far more than outlets overall.
        mine: Counter[str] = Counter()
        overall: Counter[str] = Counter()
        spelling: dict[str, Counter[str]] = {}
        ignored = {stem for stem, _ in words(source.name)}
        for row in everyone:
            own = row.source.casefold() == name
            for stem, plain in dict(words(row.title)).items():
                if stem in ignored or stem.isdigit():
                    continue
                overall[stem] += 1
                if own:
                    mine[stem] += 1
                    spelling.setdefault(stem, Counter())[plain] += 1
        share = len(rows) / len(everyone) if everyone else 0.0
        distinctive = sorted(
            (
                (count / (overall[stem] * share or 1), count, stem)
                for stem, count in mine.items()
                if count >= PROFILE_MIN_WORD
            ),
            reverse=True,
        )[:PROFILE_WORDS]
        word_chips = join(
            render(
                '<a class="chip" href="{h}" title="{n} headlines">{w}</a>',
                h=self.link(
                    request,
                    "/search",
                    q=spelling[stem].most_common(1)[0][0],
                    source=source.name,
                    tag=None,
                    since=None,
                ),
                n=count,
                w=spelling[stem].most_common(1)[0][0],
            )
            for _, count, stem in distinctive
        )

        status = source_status(conn, [source.name])[0]
        state, css = source_state(source.enabled, status, now)
        return render(
            """{crumbs}<h1>{name}</h1>
<p class="meta"><span class="state {css}">{state}</span> · {tags}{group} ·
<a href="{latest}">latest headlines</a> · {feed}</p>
<p>{n} articles in the last {days} days; in {joined} multi-outlet stories this week.</p>
<h2>Articles per day</h2>
{heat}
<h2>When it publishes ({zone})</h2>
<p class="muted">Articles by the hour they were first fetched, so this follows the fetch
schedule as much as the outlet's own rhythm.</p>
{hours}
<h2>Rewrites</h2>
{rewrites}
<h2>Stories it reported first · last 7 days</h2>
{first}
<h2>Its words</h2>
<p class="muted">Headline words this outlet uses far more than the others do.</p>
<p class="chips">{words}</p>""",
            crumbs=self.crumbs(
                [("Sources", self.link(request, "/sources", name=None, tag=None, source=None))],
                source.name,
            ),
            name=source.name,
            css=css,
            state=state,
            tags=join(
                render(
                    '<a class="chip" href="{h}">{t}</a>',
                    h=self.link(request, "/sources", tag=tag),
                    t=tag,
                )
                for tag in source.tags
            ),
            group=render(' <span class="chip" title="Publisher group">{g}</span>', g=source.group)
            if source.group
            else EMPTY,
            latest=self.link(request, "/latest", source=source.name, tag=None),
            feed=external_link(source.url, "feed ↗"),
            n=f"{len(rows):,}",
            days=days,
            joined=joined,
            heat=heat,
            zone=clock.zone,
            hours=hours,
            rewrites=rewrites,
            first=first_cards,
            words=word_chips or Markup('<span class="empty">Not enough headlines yet.</span>'),
        )

    @staticmethod
    def hour_strip(clock: Clock, rows: Sequence[FirstSeen]) -> Markup:
        """Articles per hour of day, shown time: one row of 24 shaded cells."""
        if not rows:
            return Markup('<p class="empty">No articles in this period.</p>')
        per_hour = Counter(clock.shown(row.fetched_at).hour for row in rows)
        return count_strip(
            [(f"{hour:02d}", f"{hour:02d}:00", per_hour[hour]) for hour in range(24)],
            "articles in each hour",
        )

    # -- Briefing

    def page_briefing(self, request: Request, conn: sqlite3.Connection) -> Markup:
        """The home page: is everything working, what are the big stories, what changed."""
        now = utcnow()
        clock = Clock(request.utc)
        recent = [
            story
            for story in self.stories(conn, "24h")
            if self.reach(story) >= 2 and now - story.last_seen <= BRIEFING_WINDOW
        ]
        recent.sort(key=lambda story: (self.reach(story), story.last_seen), reverse=True)
        top = recent[:BRIEFING_TOP]
        shown = {id(story) for story in top}

        config = self.config
        regions: list[Markup] = []
        if config is not None:
            tags_of = {source.name.casefold(): source.tags for source in config.sources}
            for tag in (tag for tag in config.all_tags if is_country(tag)):
                # The stories this country's outlets lead on: the larger their share
                # of a story's outlets, the more local it is. Each story shows once.
                local = {
                    id(story): sum(
                        tag in tags_of.get(name.casefold(), ()) for name in story.sources
                    )
                    for story in recent
                }
                mine = sorted(
                    (s for s in recent if id(s) not in shown and local[id(s)]),
                    key=lambda s: (local[id(s)] / len(s.sources), local[id(s)], len(s.sources)),
                    reverse=True,
                )[:BRIEFING_PER_COUNTRY]
                if not mine:
                    continue
                shown.update(id(story) for story in mine)
                regions.append(
                    render(
                        """<section class="region"><h3>{tag}</h3>
<ol class="stories">{cards}</ol>
<a class="more" href="{more}">All {tag} stories →</a></section>""",
                        tag=tag,
                        cards=join(
                            self.story_card(request, conn, story, compact=True) for story in mine
                        ),
                        more=self.link(request, "/stories", tag=tag, source=None),
                    )
                )

        changes = list_title_changes(
            conn, since=now - timedelta(hours=24), live="exclude", minor=False, limit=6
        )
        fresh = count_new(conn, since=now - BRIEFING_WINDOW)
        hours = int(BRIEFING_WINDOW.total_seconds() // 3600)
        return render(
            """<h1>Briefing</h1>
{health}
<p class="muted">{fresh} new articles in the last {hours} hours.
<span class="since-visit" hidden></span></p>
{glance}
<h2>Top stories · last {hours} h</h2>
{top}
<p><a class="more" href="{all_stories}">All stories →</a></p>
{regions}
<h2>Notable rewrites · last 24 h</h2>
{changes}
<p><a class="more" href="{all_rewrites}">All rewrites →</a></p>""",
            health=self.health_strip(request, conn, clock),
            glance=self.today_glance(conn, clock, now),
            fresh=f"{fresh:,}",
            hours=hours,
            top=render(
                '<ol class="stories">{c}</ol>',
                c=join(self.story_card(request, conn, story, compact=True) for story in top),
            )
            if top
            else Markup(
                '<p class="empty">No story has been reported by two or more outlets yet.</p>'
            ),
            all_stories=self.link(request, "/stories", tag=None, source=None),
            regions=render('<h2>By country</h2><div class="regions">{r}</div>', r=join(regions))
            if regions
            else EMPTY,
            changes=self.change_items(request, changes),
            all_rewrites=self.link(request, "/rewrites", tag=None, source=None),
        )

    def today_glance(self, conn: sqlite3.Connection, clock: Clock, now: datetime) -> Markup:
        """Articles first seen in each hour today, against a typical day's line."""
        shown_now = clock.shown(now)
        midnight = shown_now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start = midnight.astimezone(UTC)
        baseline_start = (midnight - timedelta(days=BRIEFING_BASELINE_DAYS)).astimezone(UTC)
        today = [0] * 24
        typical = [0.0] * 24
        for row in first_seen(conn, since=baseline_start):
            hour = clock.shown(row.fetched_at).hour
            if row.fetched_at >= today_start:
                today[hour] += 1
            else:
                typical[hour] += 1 / BRIEFING_BASELINE_DAYS
        if not any(today) and not any(typical):
            return EMPTY
        current = shown_now.hour
        so_far = sum(today)
        usual = sum(typical[: current + 1])
        compare = (
            f" · {round(100 * (so_far - usual) / usual):+d}% against a typical day by this hour"
            if usual >= 1
            else ""
        )
        return render(
            '<h2>Today at a glance</h2><p class="muted">{n} articles since midnight{c}. '
            "The line is the average of the previous {d} days.</p>{chart}{table}",
            n=f"{so_far:,}",
            c=compare,
            d=BRIEFING_BASELINE_DAYS,
            chart=charts.bars(
                today,
                label=f"Articles first seen each hour today ({clock.zone})",
                ticks=[f"{hour:02d}" if hour % 3 == 0 else "" for hour in range(24)],
                titles=[
                    f"{hour:02d}:00 · {count} today · {typical[hour]:.0f} typical"
                    for hour, count in enumerate(today)
                ],
                highlight=current,
                average=typical,
                muted_from=current + 1,
            ),
            table=charts.data_table(
                f"Articles per hour ({clock.zone})",
                ("Hour", "Today", "Typical"),
                [
                    (f"{hour:02d}:00", today[hour], f"{typical[hour]:.1f}")
                    for hour in range(current + 1)
                ],
            ),
        )

    def health_strip(self, request: Request, conn: sqlite3.Connection, clock: Clock) -> Markup:
        """One line: all well, or what needs attention (from the /api/status checks)."""
        report = self.status_report(conn)
        sources = report["sources"] or {}
        states: dict[str, int] = sources.get("states", {})
        backup = report["backups"]["latest_at"]
        parts = [
            render(
                "{ok} of {enabled} sources ok",
                ok=states.get("ok", 0),
                enabled=sources.get("enabled", 0),
            )
            if sources
            else EMPTY,
            render(
                "last backup {when}",
                when=clock.time(datetime.fromisoformat(backup), "%a %H:%M"),
            )
            if backup
            else EMPTY,
        ]
        detail = join((part for part in parts if part), " · ")
        if report["status"] == "ok":
            return render('<p class="health good">✓ All normal · {d}</p>', d=detail)
        return render(
            '<p class="health warn">⚠ Needs attention: {checks} · {d} · '
            '<a href="{href}">Sources</a></p>',
            checks="; ".join(report["checks"]),
            d=detail,
            href=self.link(request, "/sources", tag=None, source=None),
        )

    # -- Trends

    def page_trends(self, request: Request, conn: sqlite3.Connection) -> Markup:
        filters = self.filters(request)
        window = request.get("since", "7d")
        if window not in TREND_WINDOWS:
            window = "7d"
        clock = Clock(request.utc)
        days = TREND_WINDOWS[window]
        now = utcnow()
        today = clock.shown(now).date()
        day_list = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
        start_local = datetime.combine(day_list[0], datetime.min.time())
        start_local = start_local.replace(tzinfo=clock.shown(now).tzinfo)
        since = start_local.astimezone(UTC)
        previous_since = since - timedelta(days=days)
        wanted = {name.casefold() for name in filters.named} if filters.named is not None else None

        def keep(source: str) -> bool:
            return wanted is None or source.casefold() in wanted

        # Articles per source per local day, and live blogs per day.
        rows = [row for row in first_seen(conn, since=since) if keep(row.source)]
        per_day: dict[str, Counter[Any]] = {}
        live_per_day: Counter[Any] = Counter()
        for row in rows:
            day = clock.shown(row.fetched_at).date()
            per_day.setdefault(row.source, Counter())[day] += 1
            live_per_day[day] += int(row.is_live)
        previous = {
            source: count
            for source, count in counts_by_source(conn, since=previous_since, until=since).items()
            if keep(source)
        }
        relative = request.get("shade") == "row"
        heat = render(
            '<p class="chips shade">Shade: <a href="{a}"{ac}>compare sources</a> '
            '<a href="{r}"{rc}>each source\'s own rhythm</a></p>{h}',
            a=self.link(request, "/trends", shade=None),
            ac=EMPTY if relative else Markup(' aria-current="true"'),
            r=self.link(request, "/trends", shade="row"),
            rc=Markup(' aria-current="true"') if relative else EMPTY,
            h=self.heatmap(request, per_day, live_per_day, day_list, previous, relative=relative),
        )
        countries = self.country_heatmap(request, rows, day_list, clock)
        current_total = len(rows)
        previous_total = sum(previous.values())
        summary = render(
            "<p>{n} articles in the last {days} days{delta}.</p>",
            n=f"{current_total:,}",
            days=days,
            delta=render(
                " ({d} on the previous {days} days)",
                d=f"{(current_total - previous_total) / previous_total:+.0%}",
                days=days,
            )
            if previous_total
            else EMPTY,
        )

        rising = self.rising_topics(request, clock, rows, day_list, now)
        big = self.big_stories(request, conn, since)

        stats = sorted(
            (stat for stat in rewrite_stats(conn, since=since) if keep(stat.source)),
            key=lambda stat: (stat.articles >= MIN_REWRITE_SAMPLE, stat.share, stat.rewritten),
            reverse=True,
        )
        rewrite_rows = join(
            render(
                """<tr class="{css}"><td><a href="{h}">{source}</a></td>
<td class="num">{articles}</td><td class="num">{rewritten}</td>
<td data-sort="{share}"><span class="barwrap">{bar}<span>{pct}</span></span></td>
<td class="num" data-sort="{secs}">{delay}</td></tr>""",
                css="lowsample" if stat.articles < MIN_REWRITE_SAMPLE else "",
                share=f"{stat.share:.4f}",
                secs=int(stat.median_delay.total_seconds()) if stat.median_delay else "",
                h=self.link(request, "/rewrites", source=stat.source, tag=None),
                source=stat.source,
                articles=stat.articles,
                rewritten=stat.rewritten,
                bar=bar(stat.share),
                pct=f"{stat.share:.0%}"
                + (f" (n<{MIN_REWRITE_SAMPLE})" if stat.articles < MIN_REWRITE_SAMPLE else ""),
                delay=_duration(stat.median_delay),
            )
            for stat in stats
        )
        rewrite_total = EMPTY
        if stats:
            overall = RewriteStat(
                "All",
                sum(stat.articles for stat in stats),
                sum(stat.rewritten for stat in stats),
                tuple(delay for stat in stats for delay in stat.delays),
            )
            rewrite_total = render(
                """<tfoot><tr class="sum"><th scope="row">All outlets</th>
<td class="num">{articles}</td><td class="num">{rewritten}</td>
<td><span class="barwrap">{bar}<span>{pct}</span></span></td>
<td class="num">{delay}</td></tr></tfoot>""",
                articles=overall.articles,
                rewritten=overall.rewritten,
                bar=bar(overall.share),
                pct=f"{overall.share:.0%}",
                delay=_duration(overall.median_delay),
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
{summary}
<h2>Rising topics · last 24 h</h2>
<p class="muted">Words in headlines from the last 24 hours, compared with their daily average
over the rest of the period. Click one to search for it.</p>
{rising}
<h2>Biggest stories</h2>
<p class="muted">Stories reported by the most outlets{story_note}.</p>
{big}
<h2>Articles per day</h2>
<p class="muted">New articles by the day they were first fetched ({zone}). Darker is more;
click a cell for that day's articles. ▲/▼ compares each total with the previous {days}
days.</p>
{heat}
<h3>By country</h3>
<p class="muted">Articles from each country's outlets (by their country tag) per day. Shaded
against each country's own busiest day, so quieter countries' peaks still show.</p>
{countries}
<h3>By hour of day</h3>
<p class="muted">All articles in the period by the hour they were first fetched: gaps
between fetch runs show as empty hours.</p>
{hours}
<h2>Rewrites by outlet</h2>
<p class="muted">Of the articles first seen in this period (live blogs excluded), how many
were reworded later. Punctuation-only changes don't count. The delay is from when the article
was first fetched to when the new wording was, so it can't be shorter than the time between
fetch runs. Outlets with fewer than {min} articles are greyed and listed last.</p>
<div class="scroll"><table class="sortable">
<thead><tr><th>Source</th><th class="num">Articles</th><th class="num">Rewritten</th>
<th>Share</th><th class="num">Median delay</th></tr></thead>
<tbody>{rewrite_rows}</tbody>{rewrite_total}</table></div>
<details class="section"><summary><h2>Feed turnover and reliability</h2></summary>
<p class="muted">How much of each feed is new at each run. A feed that is entirely new run
after run (<span class="warn">highlighted</span> when it happens in half the runs or more) is
probably dropping stories between runs, so fetching more often would catch more. Each
source's first-ever run is left out.</p>
<div class="scroll"><table class="sortable">
<thead><tr><th>Source</th><th class="num">OK</th><th class="num">Failed</th>
<th class="num">Skipped</th><th class="num">Items/run</th><th>New per run</th>
<th class="num">Runs all new</th></tr></thead>
<tbody>{turnover_rows}</tbody></table></div></details>""",
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
            summary=summary,
            rising=rising,
            big=big,
            story_note=", among the last 7 days" if days > 7 else "",
            zone=clock.zone,
            days=days,
            heat=heat,
            countries=countries,
            hours=self.hour_strip(clock, rows),
            min=MIN_REWRITE_SAMPLE,
            rewrite_rows=rewrite_rows or Markup('<tr><td colspan="5">No articles yet.</td></tr>'),
            rewrite_total=rewrite_total,
            turnover_rows=turnover_rows
            or Markup('<tr><td colspan="7">No runs logged yet.</td></tr>'),
        )

    def rising_topics(
        self,
        request: Request,
        clock: Clock,
        rows: Sequence[FirstSeen],
        days: list[Any],
        now: datetime,
    ) -> Markup:
        """Terms much more common in the last 24 hours than over the rest of the period."""
        recent_since = now - timedelta(hours=24)
        config = self.config
        ignored: set[str] = set()
        if config is not None:
            for source in config.sources:
                ignored.update(stem for stem, _ in words(source.name))
                ignored.update(tag.casefold() for tag in source.tags)
        recent: Counter[str] = Counter()
        earlier: Counter[str] = Counter()
        outlets: dict[str, set[str]] = {}
        per_day: dict[str, Counter[Any]] = {}
        spelling: dict[str, Counter[str]] = {}
        for row in rows:
            if row.is_live:
                continue
            day = clock.shown(row.fetched_at).date()
            fresh = row.fetched_at >= recent_since
            for stem, plain in dict(words(row.title)).items():
                if stem in ignored or stem.isdigit():
                    continue
                per_day.setdefault(stem, Counter())[day] += 1
                if fresh:
                    recent[stem] += 1
                    outlets.setdefault(stem, set()).add(row.source)
                    spelling.setdefault(stem, Counter())[plain] += 1
                else:
                    earlier[stem] += 1
        span = (
            max(1.0, (recent_since - rows[0].fetched_at).total_seconds() / 86400) if rows else 1.0
        )
        scored = []
        for stem, count in recent.items():
            if count < RISING_MIN_COUNT or len(outlets[stem]) < RISING_MIN_SOURCES:
                continue
            baseline = earlier[stem] / span
            scored.append(((count + 1) / (baseline + 1), count, stem, baseline))
        scored.sort(reverse=True)
        if not scored:
            return Markup('<p class="empty">Nothing stands out in the last 24 hours yet.</p>')
        items = []
        for ratio, count, stem, baseline in scored[:RISING_TOP]:
            word = spelling[stem].most_common(1)[0][0]
            items.append(
                render(
                    '<li><a href="{h}">{word}</a> {spark} <span class="muted">{n} in 24 h · '
                    "{ratio}</span></li>",
                    h=self.link(request, "/search", q=word, since=None, page=None),
                    word=word,
                    spark=sparkline([per_day[stem].get(day, 0) for day in days]),
                    n=count,
                    ratio="new" if baseline == 0 else f"\N{MULTIPLICATION SIGN}{ratio:.1f}",
                )
            )
        return render('<ol class="rising">{i}</ol>', i=join(items))

    def big_stories(self, request: Request, conn: sqlite3.Connection, since: datetime) -> Markup:
        """The stories with the most outlets, among the last 7 days at most."""
        stories = sorted(
            (
                story
                for story in self.stories(conn, "7d")
                if self.reach(story) >= 2 and story.last_seen >= since
            ),
            key=lambda story: (self.reach(story), story.last_seen),
            reverse=True,
        )[:BIG_STORIES]
        if not stories:
            return Markup(
                '<p class="empty">No story has been reported by two or more outlets yet.</p>'
            )
        return render(
            '<ol class="stories">{c}</ol>',
            c=join(self.story_card(request, conn, story, compact=True) for story in stories),
        )

    def country_heatmap(
        self, request: Request, rows: Sequence[FirstSeen], days: list[Any], clock: Clock
    ) -> Markup:
        """Articles per country tag per day, each row shaded against its own busiest day."""
        config = self.config
        if config is None or not rows:
            return EMPTY
        countries_of = {
            source.name.casefold(): [tag for tag in source.tags if is_country(tag)]
            for source in config.sources
        }
        per_day: dict[str, Counter[Any]] = {}
        for row in rows:
            for tag in countries_of.get(row.source.casefold(), ()):
                per_day.setdefault(tag, Counter())[clock.shown(row.fetched_at).date()] += 1
        if not per_day:
            return EMPTY

        def cells(tag: str, counts: Counter[Any]) -> Markup:
            top = max(counts.values()) or 1
            return join(
                render(
                    '<td class="heat h{l}"><a href="{h}" title="{n} on {d}">{n}</a></td>',
                    l=max(1, math.ceil(HEAT_LEVELS * counts[day] / top)),
                    h=self.link(
                        request, "/latest", tag=tag, source=None, since=None, day=day.isoformat()
                    ),
                    n=counts[day],
                    d=day.isoformat(),
                )
                if counts[day]
                else Markup('<td class="heat h0"></td>')
                for day in days
            )

        ordered = sorted(per_day.items(), key=lambda item: -sum(item[1].values()))
        return render(
            '<div class="scroll"><table class="heatmap"><thead><tr><th>Country</th>{d}'
            '<th class="num">Total</th></tr></thead><tbody>{r}</tbody></table></div>',
            d=join(
                render('<th class="day">{d}</th>', d=f"{day.strftime('%a')} {day.day}")
                for day in days
            ),
            r=join(
                render(
                    '<tr><th scope="row"><a href="{h}">{t}</a></th>{c}'
                    '<td class="num">{n}</td></tr>',
                    h=self.link(request, "/trends", tag=tag, source=None),
                    t=tag,
                    c=cells(tag, counts),
                    n=sum(counts.values()),
                )
                for tag, counts in ordered
            ),
        )

    def heatmap(
        self,
        request: Request,
        per_day: dict[str, Counter[Any]],
        live_per_day: Counter[Any],
        days: list[Any],
        previous: dict[str, int] | None = None,
        *,
        relative: bool = False,
    ) -> Markup:
        """Articles per source per day; `relative` shades each row against its own busiest day."""
        if not per_day:
            return Markup('<p class="empty">No articles in this period.</p>')
        previous = previous or {}
        peak = max(count for counts in per_day.values() for count in counts.values())
        row_peak = {
            source: max(counts.values(), default=0) or 1 for source, counts in per_day.items()
        }
        totals_by_day: Counter[Any] = Counter()
        for counts in per_day.values():
            totals_by_day.update(counts)

        def cell(source: str, day: Any, count: int) -> Markup:
            top = row_peak[source] if relative else peak
            level = 0 if count == 0 else max(1, math.ceil(HEAT_LEVELS * count / top))
            if not count:
                return render('<td class="heat h{l}"></td>', l=level)
            return render(
                '<td class="heat h{l}"><a href="{h}" title="{n} on {d}">{n}</a></td>',
                l=level,
                h=self.link(
                    request, "/latest", source=source, tag=None, since=None, day=day.isoformat()
                ),
                n=count,
                d=day.isoformat(),
            )

        def change(source: str, total: int) -> Markup:
            before = previous.get(source, 0)
            if not before:
                return EMPTY
            ratio = (total - before) / before
            if abs(ratio) < 0.1:
                return EMPTY
            return render(
                ' <span class="{c}" title="{b} in the previous period">{a}</span>',
                c="up" if ratio > 0 else "down",
                b=before,
                a="▲" if ratio > 0 else "▼",
            )

        ordered = sorted(per_day.items(), key=lambda item: -sum(item[1].values()))

        def table_rows(chunk: Sequence[tuple[str, Counter[Any]]]) -> Markup:
            return join(
                render(
                    '<tr><th scope="row"><a href="{h}">{source}</a></th>{cells}'
                    '<td class="num">{total}{change}</td></tr>',
                    h=self.link(request, "/source", name=source, tag=None, since=None),
                    source=source,
                    cells=join(cell(source, day, counts.get(day, 0)) for day in days),
                    total=sum(counts.values()),
                    change=change(source, sum(counts.values())),
                )
                for source, counts in chunk
            )

        head = render(
            '<thead><tr><th>Source</th>{d}<th class="num">Total</th></tr></thead>',
            d=join(
                render(
                    '<th class="day" title="{full}">{d}</th>',
                    full=day.isoformat(),
                    d=f"{day.strftime('%a')} {day.day}",
                )
                for day in days
            ),
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
        main = render(
            '<div class="scroll"><table class="heatmap">{head}<tbody>{rows}</tbody>'
            "<tfoot>{footer}</tfoot></table></div>{legend}",
            legend=Markup(
                '<p class="legend">Each row is shaded against that source\'s own busiest day.</p>'
            )
            if relative
            else heat_legend(peak, "articles a day"),
            head=head,
            rows=table_rows(ordered[:HEAT_TOP_ROWS]),
            footer=footer,
        )
        rest = ordered[HEAT_TOP_ROWS:]
        if not rest:
            return main
        return render(
            '{main}<details class="more-rows"><summary>Show the other {n} sources</summary>'
            '<div class="scroll"><table class="heatmap">{head}<tbody>{rows}</tbody></table></div>'
            "</details>",
            main=main,
            n=len(rest),
            head=head,
            rows=table_rows(rest),
        )


def heat_legend(peak: int, unit: str) -> Markup:
    """The key to a heatmap's shades: what range of counts each one stands for."""
    if peak <= 0:
        return EMPTY
    swatches = []
    for level in range(1, HEAT_LEVELS + 1):
        low = math.floor(peak * (level - 1) / HEAT_LEVELS) + 1
        high = math.floor(peak * level / HEAT_LEVELS)
        if high < low:
            continue
        span = str(low) if low == high else f"{low}\N{EN DASH}{high}"
        swatches.append(render('<span class="heat h{l}" title="{s}">{s}</span>', l=level, s=span))
    return render(
        '<p class="legend" aria-label="Shading key">{u}: {s}</p>',
        u=unit[:1].upper() + unit[1:],
        s=join(swatches),
    )


def count_strip(cells: Sequence[tuple[str, str, int]], unit: str = "articles") -> Markup:
    """One row of shaded count cells under short labels: (label, hover name, count)."""
    peak = max((count for _, _, count in cells), default=0) or 1
    return render(
        '<div class="scroll"><table class="heatmap hours"><thead><tr>{h}</tr></thead>'
        "<tbody><tr>{c}</tr></tbody></table></div>{legend}",
        legend=heat_legend(peak, unit),
        h=join(render('<th class="day">{l}</th>', l=label) for label, _, _ in cells),
        c=join(
            render(
                '<td class="heat h{l}" title="{n} · {name}">{n}</td>',
                l=0 if not count else max(1, math.ceil(HEAT_LEVELS * count / peak)),
                n=count or "",
                name=name,
            )
            for _, name, count in cells
        ),
    )


def sparkline(values: Sequence[int]) -> Markup:
    """A tiny line chart of daily counts, as inline SVG (attributes only, CSP-safe)."""
    if not values:
        return EMPTY
    peak = max(values) or 1
    step = 100 / max(1, len(values) - 1)
    points = " ".join(
        f"{index * step:.1f},{18 - 16 * value / peak:.1f}" for index, value in enumerate(values)
    )
    return render(
        '<svg class="spark" viewBox="0 0 100 20" preserveAspectRatio="none" aria-hidden="true">'
        '<polyline points="{p}"/></svg>',
        p=points,
    )


def is_country(tag: str) -> bool:
    """Country tags are two capital letters (AU, IE); the rest are regions or topics."""
    return len(tag) == 2 and tag.isalpha() and tag.isupper()


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
        try:
            exists = self.path.exists()
        except OSError as exc:
            raise HttpError("503 Service Unavailable", f"Cannot open the database: {exc}") from exc
        if not exists:
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
