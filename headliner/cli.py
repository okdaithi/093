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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final, TextIO

from headliner import __version__
from headliner.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from headliner.fetcher import SourceResult, fetch_all
from headliner.models import Headline, utcnow
from headliner.store import (
    DEFAULT_DB_PATH,
    SourceStatus,
    insert_headlines,
    list_headlines,
    open_db,
    record_fetch,
    search_headlines,
    source_status,
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


def _format_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else "-"


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


def output_headlines(headlines: list[Headline], fmt: str, stream: TextIO) -> None:
    """Write headlines to `stream` as a table, JSON or CSV."""
    if fmt == "json":
        json.dump([headline.as_dict() for headline in headlines], stream, indent=2)
        stream.write("\n")
        return

    if fmt == "csv":
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["source", "published_at", "title", "url", "summary"])
        for headline in headlines:
            writer.writerow(
                [
                    headline.source,
                    headline.published_at.isoformat() if headline.published_at else "",
                    headline.title,
                    headline.url,
                    headline.summary or "",
                ]
            )
        return

    if not headlines:
        print("No headlines found.", file=stream)
        return
    rows = [
        [
            _truncate(headline.source, 22),
            _format_dt(headline.published_at),
            _truncate(headline.title, 78),
            headline.url,
        ]
        for headline in headlines
    ]
    render_table(rows, ["SOURCE", "PUBLISHED", "TITLE", "URL"], stream)


def _summarise(results: list[SourceResult], *, dry_run: bool) -> None:
    ok = sum(1 for result in results if result.status == "ok")
    skipped = sum(1 for result in results if result.status == "skipped")
    failed = sum(1 for result in results if result.status == "error")
    found = sum(result.items_found for result in results)
    new = sum(result.items_new for result in results)
    suffix = " (dry run, nothing written)" if dry_run else ""
    logger.info(
        "done: %d ok, %d skipped, %d failed; %d item(s) found, %d new%s",
        ok,
        skipped,
        failed,
        found,
        new,
        suffix,
    )


def cmd_fetch(args: argparse.Namespace, config: Config) -> int:
    """Fetch every selected source, store new headlines and log the run."""
    sources = config.select(args.only)
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
                output_headlines(result.headlines, "table", sys.stdout)
                print(file=sys.stdout)
        _summarise(results, dry_run=True)
    else:
        with open_db(args.db) as conn:
            for result in results:
                if result.headlines:
                    result.items_new = insert_headlines(conn, result.headlines)
                record_fetch(
                    conn,
                    source=result.source,
                    started_at=result.started_at,
                    finished_at=result.finished_at,
                    status=result.status,
                    items_found=result.items_found,
                    items_new=result.items_new,
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
        headlines = list_headlines(conn, since=since, source=args.source, limit=args.limit)
    output_headlines(headlines, args.format, sys.stdout)
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    """Search stored headlines."""
    with open_db(args.db) as conn:
        headlines = search_headlines(conn, args.query, limit=args.limit)
    output_headlines(headlines, args.format, sys.stdout)
    return EXIT_OK


def _render_sources(statuses: list[SourceStatus], config: Config, fmt: str) -> None:
    by_name = {source.name: source for source in config.sources}
    if fmt == "json":
        payload = [
            {
                "name": status.name,
                "url": by_name[status.name].url,
                "type": by_name[status.name].type,
                "enabled": by_name[status.name].enabled,
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

    rows = []
    for status in statuses:
        source = by_name[status.name]
        rows.append(
            [
                status.name,
                source.type,
                "yes" if source.enabled else "no",
                str(status.total_items),
                _format_dt(status.last_success),
                status.last_status or "never fetched",
            ]
        )
    render_table(
        rows,
        ["NAME", "TYPE", "ENABLED", "ITEMS", "LAST SUCCESS", "LAST STATUS"],
        sys.stdout,
    )


def cmd_sources(args: argparse.Namespace, config: Config) -> int:
    """List configured sources with their last fetch state."""
    with open_db(args.db) as conn:
        statuses = source_status(conn, [source.name for source in config.sources])
    _render_sources(statuses, config, args.format)
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
        "--limit", type=int, default=50, metavar="N", help="maximum rows (default: %(default)s)"
    )
    search.add_argument(
        "--format",
        choices=("table", "json", "csv"),
        default="table",
        help="output format (default: %(default)s)",
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
        # `search` reads only the database, so it does not need a config file.
        if args.command == "search":
            return cmd_search(args)
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
