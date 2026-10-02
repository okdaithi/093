"""Command-line entry point.

Exit codes: 0 full success, 1 at least one source failed, 2 config or fatal error.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, TextIO
from urllib.parse import urlsplit

from headliner import __version__
from headliner.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    Settings,
    load_config,
    parse_tags,
)
from headliner.discover import discover, render_yaml
from headliner.fetcher import SourceResult, fetch_all
from headliner.models import Headline, utcnow
from headliner.store import (
    DEFAULT_DB_PATH,
    LiveFilter,
    SourceStatus,
    TitleChange,
    count_title_changes,
    list_headlines,
    list_title_changes,
    migrate,
    open_db,
    record_fetch,
    search_headlines,
    search_history,
    source_status,
    store_headlines,
    upgrade_plan,
)

logger = logging.getLogger("headliner")

EXIT_OK: Final = 0
EXIT_PARTIAL_FAILURE: Final = 1
EXIT_FATAL: Final = 2

_DURATION_RE: Final = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdw])\s*$", re.IGNORECASE)
_DURATION_UNITS: Final = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


def parse_duration(value: str) -> timedelta:
    """Parse `24h`, `30m`, `7d` and friends into a `timedelta`."""
    match = _DURATION_RE.match(value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid duration {value!r}; use a number with a unit, e.g. 30m, 24h, 7d"
        )
    amount, unit = match.groups()
    return timedelta(**{_DURATION_UNITS[unit.lower()]: float(amount)})


def configure_logging(*, verbose: bool, quiet: bool) -> None:
    """Structured logging to stderr. Data always goes to stdout."""
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        force=True,
    )
    # httpx logs every request at INFO, which drowns out our own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def time_column(values: Sequence[datetime | None], *, utc: bool) -> tuple[list[str], str]:
    """Format a table column of datetimes and name its timezone for the header.

    Stored values are UTC. Table output shows them in local time (the `TZ`
    environment variable or the system zone) unless `utc` is set. When the
    column spans a daylight-saving change, each cell carries its own zone
    abbreviation and the header just says "local".
    """
    shown = [
        (value.astimezone(UTC) if utc else value.astimezone()) if value else None
        for value in values
    ]
    zones = {value.tzname() or "local" for value in shown if value}
    if utc:
        label = "UTC"
    elif len(zones) == 1:
        label = next(iter(zones))
    elif not zones:
        label = datetime.now().astimezone().tzname() or "local"
    else:
        label = "local"
    per_cell = label == "local" and len(zones) > 1
    cells = [
        "-"
        if value is None
        else value.strftime("%Y-%m-%d %H:%M") + (f" {value.tzname()}" if per_cell else "")
        for value in shown
    ]
    return cells, label


LIVE_TAG: Final = "[LIVE] "
FIRST_SEEN: Final = "(first seen)"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1].rstrip() + "…"


def render_table(rows: list[list[str]], headers: list[str], stream: TextIO) -> None:
    """Fixed-width table with a dashed rule under the header."""
    if not rows:
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    line = "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    print(line.rstrip(), file=stream)
    print("  ".join("-" * width for width in widths), file=stream)
    for row in rows:
        body = "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row))
        print(body.rstrip(), file=stream)


def output_headlines(
    headlines: list[Headline],
    fmt: str,
    stream: TextIO,
    *,
    utc: bool = False,
    matched: Sequence[str | None] | None = None,
) -> None:
    """Write headlines to `stream` as a table, JSON or CSV.

    JSON and CSV always carry UTC ISO-8601 timestamps; only the table follows `utc`.
    `matched` (from `search --history`) adds each hit's matched earlier title.
    """
    if fmt == "json":
        items = [headline.as_dict() for headline in headlines]
        if matched is not None:
            for item, title in zip(items, matched, strict=True):
                item["matched_title"] = title
        json.dump(items, stream, indent=2)
        stream.write("\n")
        return

    if fmt == "csv":
        writer = csv.writer(stream, lineterminator="\n")
        header = ["source", "published_at", "title", "url", "summary", "is_live"]
        writer.writerow([*header, "matched_title"] if matched is not None else header)
        for index, headline in enumerate(headlines):
            row = [
                headline.source,
                headline.published_at.isoformat() if headline.published_at else "",
                headline.title,
                headline.url,
                headline.summary or "",
                "1" if headline.is_live else "0",
            ]
            if matched is not None:
                row.append(matched[index] or "")
            writer.writerow(row)
        return

    if not headlines:
        print("No headlines found.", file=stream)
        return
    published, zone = time_column([headline.published_at for headline in headlines], utc=utc)
    rows = [
        [
            _truncate(headline.source, 22),
            when,
            _truncate(f"{LIVE_TAG}{headline.title}" if headline.is_live else headline.title, 78),
            headline.url,
        ]
        for headline, when in zip(headlines, published, strict=True)
    ]
    headers = ["SOURCE", f"PUBLISHED ({zone})", "TITLE", "URL"]
    # Only when some hit matched an earlier title: say which one, before the URL.
    if matched is not None and any(matched):
        for row, title in zip(rows, matched, strict=True):
            row.insert(3, _truncate(title, 50) if title else "-")
        headers.insert(3, "MATCHED EARLIER TITLE")
    render_table(rows, headers, stream)


def _summarise(results: list[SourceResult], *, dry_run: bool) -> None:
    ok = sum(1 for result in results if result.status == "ok")
    skipped = sum(1 for result in results if result.status == "skipped")
    failed = sum(1 for result in results if result.status == "error")
    found = sum(result.items_found for result in results)
    new = sum(result.items_new for result in results)
    changed = sum(result.items_changed for result in results)
    changed_live = sum(result.items_changed_live for result in results)
    live_note = f" ({changed_live} live)" if changed_live else ""
    suffix = " (dry run, nothing written)" if dry_run else ""
    logger.info(
        "done: %d ok, %d skipped, %d failed; %d item(s) found, %d new, %d retitled%s%s",
        ok,
        skipped,
        failed,
        found,
        new,
        changed,
        live_note,
        suffix,
    )


def cmd_fetch(args: argparse.Namespace, config: Config) -> int:
    """Fetch every selected source, store new headlines and log the run."""
    sources = config.select(args.only, args.tag)
    if not sources:
        logger.error("no enabled sources to fetch")
        return EXIT_FATAL

    logger.info(
        "fetching %d source(s) with concurrency %d", len(sources), config.settings.concurrency
    )
    results = asyncio.run(fetch_all(sources, config.settings, ignore_robots=args.ignore_robots))

    if args.dry_run:
        for result in results:
            if result.headlines:
                output_headlines(result.headlines, "table", sys.stdout, utc=args.utc)
                print(file=sys.stdout)
        _summarise(results, dry_run=True)
    else:
        with open_db(args.db) as conn:
            for result in results:
                if result.headlines:
                    stored = store_headlines(conn, result.headlines)
                    result.items_new = stored.new
                    result.items_changed = stored.retitled
                    result.items_changed_live = stored.retitled_live
                record_fetch(
                    conn,
                    source=result.source,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                    status=result.status,
                    items_found=result.items_found,
                    items_new=result.items_new,
                    items_changed=result.items_changed,
                    error=result.error,
                )
        _summarise(results, dry_run=False)

    return EXIT_OK if all(result.ok for result in results) else EXIT_PARTIAL_FAILURE


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    """Print stored headlines."""
    if args.source:
        config.select([args.source])
    since = utcnow() - args.since if args.since else None
    with open_db(args.db) as conn:
        headlines = list_headlines(
            conn,
            since=since,
            source=args.source,
            sources=_tag_sources(args, config),
            limit=args.limit,
        )
    output_headlines(headlines, args.format, sys.stdout, utc=args.utc)
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    """Search stored headlines: current versions, or every version with --history."""
    with open_db(args.db) as conn:
        if not args.history:
            headlines = search_headlines(
                conn, args.query, limit=args.limit, sources=_tag_sources(args)
            )
            output_headlines(headlines, args.format, sys.stdout, utc=args.utc)
            return EXIT_OK
        hits = search_history(conn, args.query, limit=args.limit, sources=_tag_sources(args))
    output_headlines(
        [hit.headline for hit in hits],
        args.format,
        sys.stdout,
        utc=args.utc,
        matched=[hit.matched_title for hit in hits],
    )
    return EXIT_OK


def output_changes(
    changes: list[TitleChange], fmt: str, stream: TextIO, *, utc: bool = False
) -> None:
    """Write headline rewrites to `stream` as a table, JSON or CSV.

    JSON and CSV always carry UTC ISO-8601 timestamps; only the table follows `utc`.
    """
    if fmt == "json":
        payload = [
            {
                "source": change.source,
                "url": change.url,
                "changed_at": change.changed_at.isoformat() if change.changed_at else None,
                "old_title": change.old_title,
                "new_title": change.new_title,
                "is_live": change.is_live,
            }
            for change in changes
        ]
        json.dump(payload, stream, indent=2)
        stream.write("\n")
        return

    if fmt == "csv":
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["source", "changed_at", "old_title", "new_title", "url", "is_live"])
        for change in changes:
            writer.writerow(
                [
                    change.source,
                    change.changed_at.isoformat() if change.changed_at else "",
                    change.old_title or "",
                    change.new_title,
                    change.url,
                    "1" if change.is_live else "0",
                ]
            )
        return

    if not changes:
        print("No headline changes found.", file=stream)
        return
    changed, zone = time_column([change.changed_at for change in changes], utc=utc)
    rows = [
        [
            when,
            _truncate(change.source, 22),
            _truncate(change.old_title or FIRST_SEEN, 60),
            _truncate(change.new_title, 60),
        ]
        for change, when in zip(changes, changed, strict=True)
    ]
    render_table(rows, [f"CHANGED ({zone})", "SOURCE", "OLD TITLE", "NEW TITLE"], stream)


def cmd_changes(args: argparse.Namespace) -> int:
    """Print headline rewrites; live blogs are hidden unless asked for."""
    since = utcnow() - args.since if args.since else None
    tagged = _tag_sources(args)
    live: LiveFilter = "only" if args.live_only else "include" if args.include_live else "exclude"
    with open_db(args.db) as conn:
        changes = list_title_changes(
            conn,
            since=since,
            source=args.source,
            sources=tagged,
            limit=args.limit,
            live=live,
            oldest_first=args.oldest_first,
        )
        # Live rewrites only; a live blog's first headline is not a change.
        hidden = (
            count_title_changes(
                conn, since=since, source=args.source, sources=tagged, live="include"
            )
            - count_title_changes(
                conn, since=since, source=args.source, sources=tagged, live="exclude"
            )
            if live == "exclude"
            else 0
        )
    output_changes(changes, args.format, sys.stdout, utc=args.utc)
    if hidden:
        note = f"{hidden} live-blog headline(s) hidden; use --include-live or --live-only"
        if args.format == "table":
            print(f"\n({note})", file=sys.stdout)
        else:
            logger.info("%s", note)
    return EXIT_OK


def cmd_migrate(args: argparse.Namespace) -> int:
    """Upgrade the database schema, or with --dry-run describe the upgrade."""
    if not args.db.exists():
        logger.error("no database at %s", args.db)
        return EXIT_FATAL
    with open_db(args.db, migrate_schema=False) as conn:
        plan = upgrade_plan(conn)
    if not plan.needed:
        print(f"{args.db}: schema version {plan.from_version} is current; nothing to do.")
        return EXIT_OK

    steps = []
    if plan.from_version < 1:
        verb = "would merge" if args.dry_run else "merging"
        steps.append(
            f"{verb} {plan.rows_merged} row(s) across {plan.merged_urls} URL(s) into title history"
        )
    if plan.from_version < 2:
        verb = "would flag" if args.dry_run else "flagging"
        steps.append(f"{verb} {plan.live_articles} live blog(s)")
    if plan.from_version < 3:
        verb = "would re-normalise" if args.dry_run else "re-normalising"
        steps.append(
            f"{verb} {plan.urls_normalised} URL(s), folding {plan.rows_folded} row(s); "
            "indexing title history for search"
        )
    print(
        f"{args.db}: schema version {plan.from_version} -> {plan.to_version}; "
        f"{plan.headlines} headline row(s); {'; '.join(steps)}."
    )
    if args.dry_run:
        print("Dry run: nothing changed.")
        return EXIT_OK
    with open_db(args.db, migrate_schema=False) as conn:
        migrate(conn)
    print("Done.")
    return EXIT_OK


def _render_sources(
    statuses: list[SourceStatus], config: Config, fmt: str, *, utc: bool = False
) -> None:
    by_name = {source.name: source for source in config.sources}
    if fmt == "json":
        payload = [
            {
                "name": status.name,
                "url": by_name[status.name].url,
                "type": by_name[status.name].type,
                "enabled": by_name[status.name].enabled,
                "tags": list(by_name[status.name].tags),
                "items": status.total_items,
                "last_success": status.last_success.isoformat() if status.last_success else None,
                "last_status": status.last_status,
                "last_error": status.last_error,
            }
            for status in statuses
        ]
        json.dump(payload, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return

    successes, zone = time_column([status.last_success for status in statuses], utc=utc)
    rows = []
    for status, when in zip(statuses, successes, strict=True):
        source = by_name[status.name]
        rows.append(
            [
                status.name,
                source.type,
                "yes" if source.enabled else "no",
                ",".join(source.tags) or "-",
                str(status.total_items),
                when,
                status.last_status or "never fetched",
            ]
        )
    render_table(
        rows,
        ["NAME", "TYPE", "ENABLED", "TAGS", "ITEMS", f"LAST SUCCESS ({zone})", "LAST STATUS"],
        sys.stdout,
    )


def cmd_sources(args: argparse.Namespace, config: Config) -> int:
    """List configured sources with their last fetch state."""
    shown = config.tagged(args.tag) if args.tag else config.sources
    with open_db(args.db) as conn:
        statuses = source_status(conn, [source.name for source in shown])
    _render_sources(statuses, config, args.format, utc=args.utc)
    return EXIT_OK


def _tag_sources(args: argparse.Namespace, config: Config | None = None) -> list[str] | None:
    """Source names for `--tag`, or None when no tag was given.

    Commands that otherwise only read the database load the config just for this.
    """
    if not getattr(args, "tag", None):
        return None
    active = config or load_config(args.sources)
    return [source.name for source in active.tagged(args.tag)]


def cmd_discover(args: argparse.Namespace) -> int:
    """Find feeds for site URLs and print source entries ready to paste."""
    tags = list(parse_tags(args.tag, "--tag"))
    for site in args.sites:
        if urlsplit(site).scheme not in {"http", "https"} or not urlsplit(site).hostname:
            logger.error("not an http(s) URL: %r", site)
            return EXIT_FATAL
    try:
        config: Config | None = load_config(args.sources)
    except ConfigError as exc:
        logger.warning("%s; using default settings and not checking for existing sources", exc)
        config = None
    settings = config.settings if config else Settings()
    if "you@example.com" in settings.user_agent:
        logger.warning("user_agent still has the placeholder contact; set a real one in settings")

    results = asyncio.run(discover(args.sites, settings, config=config))
    sys.stdout.write(render_yaml(results, tags, generated=datetime.now().astimezone()))
    found = sum(1 for result in results if result.status == "ok")
    known = sum(1 for result in results if result.status == "configured")
    failed = sum(1 for result in results if result.status == "failed")
    logger.info(
        "discover: %d feed(s) found, %d already configured, %d without a usable feed",
        found,
        known,
        failed,
    )
    return EXIT_OK if not failed else EXIT_PARTIAL_FAILURE


def cmd_web(args: argparse.Namespace) -> int:
    """Serve the read-only web viewer until interrupted."""
    from headliner.web import serve  # the CLI's other commands never need it

    config_path = args.sources if args.sources.exists() else None
    if config_path is None:
        logger.warning(
            "no sources file at %s; tag filters and the Sources page are off", args.sources
        )
    try:
        serve(args.db, config_path, host=args.host, port=args.port)
    except OSError as exc:
        logger.error("cannot listen on %s:%d: %s", args.host, args.port, exc)
        return EXIT_FATAL
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Assemble the argparse command tree."""
    parser = argparse.ArgumentParser(
        prog="headliner",
        description="Scrape news headlines from a configurable list of sites.",
    )
    parser.add_argument("--version", action="version", version=f"headliner {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--sources",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help="path to sources.yaml (default: %(default)s)",
    )
    common.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        metavar="PATH",
        help="SQLite database path (default: %(default)s)",
    )
    common.add_argument(
        "--utc",
        action="store_true",
        help="show table times in UTC instead of local time (JSON and CSV are always UTC)",
    )
    verbosity = common.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG level")
    verbosity.add_argument("-q", "--quiet", action="store_true", help="log errors only")

    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch", parents=[common], help="fetch headlines from configured sources"
    )
    fetch.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="fetch only these sources (by configured name)",
    )
    fetch.add_argument(
        "--tag", action="append", metavar="TAG", help="only sources with this tag (repeatable)"
    )
    fetch.add_argument(
        "--ignore-robots",
        action="store_true",
        help="do not consult robots.txt (off by default)",
    )
    fetch.add_argument(
        "--dry-run",
        action="store_true",
        help="fetch and parse but write nothing to the database",
    )

    listing = subparsers.add_parser("list", parents=[common], help="list stored headlines")
    listing.add_argument(
        "--since",
        type=parse_duration,
        metavar="DURATION",
        help="only headlines newer than this age, e.g. 24h, 7d",
    )
    listing.add_argument("--source", metavar="NAME", help="restrict to one source")
    listing.add_argument(
        "--tag", action="append", metavar="TAG", help="only sources with this tag (repeatable)"
    )
    listing.add_argument(
        "--limit", type=int, default=50, metavar="N", help="maximum rows (default: %(default)s)"
    )
    listing.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="output format (default: %(default)s)",
    )

    search = subparsers.add_parser("search", parents=[common], help="search stored headlines")
    search.add_argument("query", help="search terms")
    search.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="only sources with this tag (repeatable; reads the config file)",
    )
    search.add_argument(
        "--history",
        action="store_true",
        help="also search earlier titles and summaries (default: current versions only)",
    )
    search.add_argument(
        "--limit", type=int, default=50, metavar="N", help="maximum rows (default: %(default)s)"
    )
    search.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="output format (default: %(default)s)",
    )

    changes = subparsers.add_parser(
        "changes", parents=[common], help="list headlines that were rewritten after publication"
    )
    changes.add_argument(
        "--since",
        type=parse_duration,
        metavar="DURATION",
        help="only changes seen within this age, e.g. 24h, 7d",
    )
    changes.add_argument("--source", metavar="NAME", help="restrict to one source")
    changes.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="only sources with this tag (repeatable; reads the config file)",
    )
    changes.add_argument(
        "--limit", type=int, default=50, metavar="N", help="maximum rows (default: %(default)s)"
    )
    changes.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="output format (default: %(default)s)",
    )
    live_group = changes.add_mutually_exclusive_group()
    live_group.add_argument(
        "--include-live",
        action="store_true",
        help="also show live blogs, which are hidden by default",
    )
    live_group.add_argument(
        "--live-only",
        action="store_true",
        help="only live blogs, as a timeline including each blog's first headline",
    )
    changes.add_argument(
        "--oldest-first",
        action="store_true",
        help="chronological order (useful with --live-only)",
    )

    migrate_cmd = subparsers.add_parser(
        "migrate", parents=[common], help="upgrade the database schema (runs automatically)"
    )
    migrate_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="describe the upgrade without changing the database",
    )

    sources_cmd = subparsers.add_parser(
        "sources", parents=[common], help="list configured sources and their last fetch"
    )
    sources_cmd.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="output format (default: %(default)s)",
    )
    sources_cmd.add_argument(
        "--tag", action="append", metavar="TAG", help="only sources with this tag (repeatable)"
    )

    discover_cmd = subparsers.add_parser(
        "discover",
        parents=[common],
        help="find RSS/Atom feeds for site URLs and print source entries to paste",
    )
    discover_cmd.add_argument("sites", nargs="+", metavar="URL", help="site homepages")
    discover_cmd.add_argument(
        "--tag",
        action="append",
        metavar="TAG",
        help="tag to put on every discovered source (repeatable), e.g. --tag AU",
    )

    web = subparsers.add_parser(
        "web", parents=[common], help="serve a read-only web viewer of the database"
    )
    web.add_argument(
        "--host",
        default="127.0.0.1",
        help="address to listen on (default: %(default)s; 0.0.0.0 for the whole network)",
    )
    web.add_argument(
        "--port", type=int, default=8090, help="port to listen on (default: %(default)s)"
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch, and map failures onto exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(verbose=args.verbose, quiet=args.quiet)

    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        logger.error("--limit must be greater than 0")
        return EXIT_FATAL

    try:
        # These read only the database, so they do not need a config file.
        if args.command == "search":
            return cmd_search(args)
        if args.command == "changes":
            return cmd_changes(args)
        if args.command == "migrate":
            return cmd_migrate(args)
        if args.command == "discover":
            return cmd_discover(args)
        if args.command == "web":
            return cmd_web(args)
        config = load_config(args.sources)
        if args.command == "fetch":
            return cmd_fetch(args, config)
        if args.command == "list":
            return cmd_list(args, config)
        if args.command == "sources":
            return cmd_sources(args, config)
    except ConfigError as exc:
        logger.error("%s", exc)
        return EXIT_FATAL
    except KeyboardInterrupt:
        logger.error("interrupted")
        return EXIT_FATAL
    except Exception as exc:
        logger.error("fatal: %s: %s", type(exc).__name__, exc)
        logger.debug("traceback", exc_info=True)
        return EXIT_FATAL

    logger.error("unknown command %r", args.command)
    return EXIT_FATAL


if __name__ == "__main__":
    raise SystemExit(main())
