"""CLI wiring: exit codes, output formats and the dry-run guarantee."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from headliner.cli import (
    EXIT_FATAL,
    EXIT_OK,
    EXIT_PARTIAL_FAILURE,
    main,
    parse_duration,
    time_column,
)
from headliner.models import Headline
from headliner.store import connect, insert_headlines, store_headlines

FIXTURES = Path(__file__).parent / "fixtures"
FEED_URL = "https://feed.example.org/rss.xml"
ROBOTS_URL = "https://feed.example.org/robots.txt"
ALLOW_ALL = "User-agent: *\nAllow: /\n"

CONFIG = f"""
settings:
  request_timeout: 5
  rate_limit_seconds: 0
  user_agent: "headliner-tests/0.1 (+contact: tests@example.org)"
  max_items_per_source: 50
  concurrency: 2
sources:
  - name: Example Wire
    url: {FEED_URL}
    type: rss
"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "headlines.db"


@pytest.fixture
def feed_body() -> bytes:
    return (FIXTURES / "sample_feed.xml").read_bytes()


def mock_feed(feed_body: bytes) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_body))


def seed(db_path: Path) -> None:
    conn = connect(db_path)
    insert_headlines(
        conn,
        [
            Headline.create(
                source="Example Wire",
                title="Ferry service restored following repairs",
                url="https://example.org/ferry",
                published_at=datetime.now(UTC) - timedelta(hours=2),
                summary="Sailings resume on the morning timetable.",
            ),
            Headline.create(
                source="Other Wire",
                title="Council approves new tram line",
                url="https://example.org/tram",
                published_at=datetime.now(UTC) - timedelta(days=10),
            ),
        ],
    )
    conn.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("30s", timedelta(seconds=30)),
        ("45m", timedelta(minutes=45)),
        ("24h", timedelta(hours=24)),
        ("7d", timedelta(days=7)),
        ("2w", timedelta(weeks=2)),
        ("1.5h", timedelta(hours=1.5)),
    ],
)
def test_parse_duration(raw: str, expected: timedelta) -> None:
    assert parse_duration(raw) == expected


@pytest.mark.parametrize("raw", ["24", "h", "", "tomorrow", "-1h"])
def test_parse_duration_rejects_junk(raw: str) -> None:
    with pytest.raises(Exception, match="invalid duration"):
        parse_duration(raw)


@respx.mock
def test_fetch_writes_headlines_and_exits_zero(
    config_path: Path, db_path: Path, feed_body: bytes
) -> None:
    mock_feed(feed_body)
    code = main(["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"])
    assert code == EXIT_OK

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 5
    log = conn.execute("SELECT source, status, items_found, items_new FROM fetch_log").fetchone()
    assert (log[0], log[1], log[2], log[3]) == ("Example Wire", "ok", 5, 5)
    conn.close()


@respx.mock
def test_fetch_is_idempotent_across_runs(
    config_path: Path, db_path: Path, feed_body: bytes
) -> None:
    mock_feed(feed_body)
    args = ["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"]
    assert main(args) == EXIT_OK
    assert main(args) == EXIT_OK

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 5
    new_counts = [row[0] for row in conn.execute("SELECT items_new FROM fetch_log ORDER BY id")]
    assert new_counts == [5, 0]
    conn.close()


@respx.mock
def test_dry_run_writes_nothing(
    config_path: Path, db_path: Path, feed_body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_feed(feed_body)
    code = main(
        ["fetch", "--sources", str(config_path), "--db", str(db_path), "--dry-run", "--quiet"]
    )
    assert code == EXIT_OK
    assert not db_path.exists()
    # Parsed headlines still go to stdout so the run can be eyeballed.
    assert "Parliament passes long-delayed housing bill" in capsys.readouterr().out


@respx.mock
def test_failing_source_exits_one_but_still_logs(config_path: Path, db_path: Path) -> None:
    respx.get(ROBOTS_URL).mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))

    code = main(["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"])
    assert code == EXIT_PARTIAL_FAILURE

    conn = connect(db_path)
    status, error = conn.execute("SELECT status, error FROM fetch_log").fetchone()
    assert status == "error"
    assert error
    conn.close()


@respx.mock
def test_only_flag_selects_a_single_source(
    config_path: Path, db_path: Path, feed_body: bytes
) -> None:
    mock_feed(feed_body)
    code = main(
        [
            "fetch",
            "--sources",
            str(config_path),
            "--db",
            str(db_path),
            "--only",
            "example wire",
            "--quiet",
        ]
    )
    assert code == EXIT_OK


def test_unknown_only_name_is_a_config_error(config_path: Path, db_path: Path) -> None:
    code = main(
        ["fetch", "--sources", str(config_path), "--db", str(db_path), "--only", "Nope", "--quiet"]
    )
    assert code == EXIT_FATAL


def test_missing_config_exits_two(tmp_path: Path) -> None:
    assert main(["fetch", "--sources", str(tmp_path / "nope.yaml"), "--quiet"]) == EXIT_FATAL


def test_malformed_config_exits_two(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("sources:\n  - name: A\n    type: rss\n", encoding="utf-8")
    assert main(["fetch", "--sources", str(path), "--quiet"]) == EXIT_FATAL


def test_list_table_output(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    assert main(["list", "--sources", str(config_path), "--db", str(db_path), "--quiet"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "SOURCE" in out and "PUBLISHED" in out
    assert "Ferry service restored following repairs" in out


def test_list_json_output(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    main(
        [
            "list",
            "--sources",
            str(config_path),
            "--db",
            str(db_path),
            "--format",
            "json",
            "--quiet",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 2
    assert {"source", "title", "url", "published_at", "content_hash"} <= set(payload[0])


def test_list_csv_output(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    main(
        ["list", "--sources", str(config_path), "--db", str(db_path), "--format", "csv", "--quiet"]
    )
    rows = list(csv.reader(io.StringIO(capsys.readouterr().out)))
    assert rows[0] == ["source", "published_at", "title", "url", "summary", "is_live"]
    assert len(rows) == 3


def test_list_since_filters_by_age(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    main(
        [
            "list",
            "--sources",
            str(config_path),
            "--db",
            str(db_path),
            "--since",
            "24h",
            "--format",
            "json",
            "--quiet",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert [item["source"] for item in payload] == ["Example Wire"]


def test_list_empty_database_is_not_an_error(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["list", "--sources", str(config_path), "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert "No headlines found." in capsys.readouterr().out


def test_bad_limit_exits_two(config_path: Path, db_path: Path) -> None:
    code = main(
        ["list", "--sources", str(config_path), "--db", str(db_path), "--limit", "0", "--quiet"]
    )
    assert code == EXIT_FATAL


def test_search_needs_no_config_file(
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed(db_path)
    # Run from a directory with no sources.yaml at all.
    monkeypatch.chdir(tmp_path / "..")
    code = main(["search", "ferry", "--db", str(db_path), "--format", "json", "--quiet"])
    assert code == EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert [item["title"] for item in payload] == ["Ferry service restored following repairs"]


def test_search_with_no_match_is_not_an_error(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    assert main(["search", "zeppelin", "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert "No headlines found." in capsys.readouterr().out


@respx.mock
def test_sources_command_reports_last_fetch(
    config_path: Path, db_path: Path, feed_body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_feed(feed_body)
    main(["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"])
    capsys.readouterr()

    assert (
        main(
            [
                "sources",
                "--sources",
                str(config_path),
                "--db",
                str(db_path),
                "--format",
                "json",
                "--quiet",
            ]
        )
        == EXIT_OK
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "Example Wire"
    assert payload[0]["items"] == 5
    assert payload[0]["last_status"] == "ok"
    assert payload[0]["last_success"] is not None


def test_sources_table_before_any_fetch(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        main(["sources", "--sources", str(config_path), "--db", str(db_path), "--quiet"]) == EXIT_OK
    )
    out = capsys.readouterr().out
    assert "Example Wire" in out
    assert "never fetched" in out


def test_verbose_and_quiet_are_mutually_exclusive(config_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["list", "--sources", str(config_path), "--verbose", "--quiet"])


# --------------------------------------------------------------------------
# headline rewrites and schema upgrades
# --------------------------------------------------------------------------


@respx.mock
def test_retitled_story_is_counted_and_listed_by_changes(
    config_path: Path, db_path: Path, feed_body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"]
    mock_feed(feed_body)
    assert main(args) == EXIT_OK
    rewritten = feed_body.replace(
        b"Parliament passes long-delayed housing bill",
        b"Housing bill finally clears parliament after delays",
    )
    mock_feed(rewritten)
    assert main(args) == EXIT_OK

    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 5
    log = conn.execute("SELECT items_new, items_changed FROM fetch_log ORDER BY id").fetchall()
    assert [tuple(row) for row in log] == [(5, 0), (0, 1)]
    conn.close()

    capsys.readouterr()
    assert main(["changes", "--db", str(db_path), "--format", "json", "--quiet"]) == EXIT_OK
    [change] = json.loads(capsys.readouterr().out)
    assert change["old_title"] == "Parliament passes long-delayed housing bill"
    assert change["new_title"] == "Housing bill finally clears parliament after delays"

    assert main(["changes", "--db", str(db_path), "--quiet"]) == EXIT_OK
    table = capsys.readouterr().out
    assert "OLD TITLE" in table
    assert "Housing bill finally clears parliament" in table


def test_changes_on_an_unchanged_database_is_not_an_error(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(db_path)
    assert main(["changes", "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert "No headline changes found." in capsys.readouterr().out


def make_legacy_db(path: Path) -> None:
    """Two rows for one URL, as schema 0 stored a rewritten headline."""
    conn = connect(path)
    insert_headlines(
        conn,
        [
            Headline.create(
                source="Example Wire",
                title="Ferry service restored following repairs",
                url="https://example.org/ferry",
            ),
        ],
    )
    conn.execute("DROP INDEX idx_headlines_url")
    conn.execute(
        "INSERT INTO headlines (source, title, url, fetched_at, content_hash)"
        " SELECT source, 'Ferry back in service after week of repairs', url, fetched_at,"
        " 'legacy-hash' FROM headlines"
    )
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()


def test_migrate_dry_run_reports_and_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "legacy.db"
    make_legacy_db(path)
    assert main(["migrate", "--dry-run", "--db", str(path), "--quiet"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "schema version 0 -> 3" in out
    assert "would merge 1 row(s) across 1 URL(s)" in out
    assert "would flag 0 live blog(s)" in out
    assert "nothing changed" in out

    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 2
    conn.close()
    assert not list(tmp_path.glob("*.bak"))


def test_migrate_applies_the_upgrade(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "legacy.db"
    make_legacy_db(path)
    assert main(["migrate", "--db", str(path), "--quiet"]) == EXIT_OK
    assert "Done." in capsys.readouterr().out
    assert main(["migrate", "--db", str(path), "--quiet"]) == EXIT_OK
    assert "nothing to do" in capsys.readouterr().out

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM headlines").fetchone()[0] == 1
    conn.close()
    assert (tmp_path / "legacy.db.pre-v1.bak").exists()


def test_migrate_without_a_database_exits_two(tmp_path: Path) -> None:
    assert main(["migrate", "--db", str(tmp_path / "missing.db"), "--quiet"]) == EXIT_FATAL


# --------------------------------------------------------------------------
# time display: local by default, UTC on request, machine formats always UTC
# --------------------------------------------------------------------------


@pytest.fixture
def local_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Switch the process timezone for one test, restoring it afterwards."""

    def use(name: str) -> None:
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def test_time_column_shows_local_time_with_zone(local_tz: Callable[[str], None]) -> None:
    local_tz("Australia/Perth")
    cells, zone = time_column(
        [datetime(2026, 10, 2, 11, 48, tzinfo=UTC), datetime(2026, 10, 2, 20, 0, tzinfo=UTC), None],
        utc=False,
    )
    assert zone == "AWST"
    # 20:00 UTC is the next morning in Perth.
    assert cells == ["2026-10-02 19:48", "2026-10-03 04:00", "-"]


def test_time_column_utc_option(local_tz: Callable[[str], None]) -> None:
    local_tz("Australia/Perth")
    cells, zone = time_column([datetime(2026, 10, 2, 11, 48, tzinfo=UTC)], utc=True)
    assert (cells, zone) == (["2026-10-02 11:48"], "UTC")


def test_time_column_across_daylight_saving_labels_each_cell(
    local_tz: Callable[[str], None],
) -> None:
    local_tz("Australia/Sydney")
    cells, zone = time_column(
        [datetime(2026, 1, 15, 0, 0, tzinfo=UTC), datetime(2026, 7, 15, 0, 0, tzinfo=UTC)],
        utc=False,
    )
    assert zone == "local"
    assert cells == ["2026-01-15 11:00 AEDT", "2026-07-15 10:00 AEST"]


def test_time_column_with_no_dates_still_names_the_zone(
    local_tz: Callable[[str], None],
) -> None:
    local_tz("Australia/Perth")
    assert time_column([None], utc=False) == (["-"], "AWST")


def seed_at(db_path: Path, published_at: datetime) -> None:
    conn = connect(db_path)
    insert_headlines(
        conn,
        [
            Headline.create(
                source="Example Wire",
                title="Ferry service restored following repairs",
                url="https://example.org/ferry",
                published_at=published_at,
            )
        ],
    )
    conn.close()


def test_list_table_is_local_by_default_and_utc_on_request(
    config_path: Path,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    local_tz: Callable[[str], None],
) -> None:
    local_tz("Australia/Perth")
    seed_at(db_path, datetime.now(UTC).replace(hour=11, minute=48, second=0, microsecond=0))
    base = ["list", "--sources", str(config_path), "--db", str(db_path), "--quiet"]

    assert main(base) == EXIT_OK
    out = capsys.readouterr().out
    assert "PUBLISHED (AWST)" in out
    assert " 19:48 " in out

    assert main([*base, "--utc"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "PUBLISHED (UTC)" in out
    assert " 11:48 " in out


def test_machine_formats_stay_utc_in_any_timezone(
    config_path: Path,
    db_path: Path,
    capsys: pytest.CaptureFixture[str],
    local_tz: Callable[[str], None],
) -> None:
    local_tz("Australia/Perth")
    published = datetime(2026, 10, 2, 11, 48, tzinfo=UTC)
    seed_at(db_path, published)
    base = ["list", "--sources", str(config_path), "--db", str(db_path), "--quiet"]

    assert main([*base, "--format", "json"]) == EXIT_OK
    [item] = json.loads(capsys.readouterr().out)
    assert item["published_at"] == "2026-10-02T11:48:00+00:00"

    assert main([*base, "--format", "csv"]) == EXIT_OK
    [row] = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert row["published_at"] == "2026-10-02T11:48:00+00:00"


@respx.mock
def test_changes_and_sources_tables_label_the_zone(
    config_path: Path,
    db_path: Path,
    feed_body: bytes,
    capsys: pytest.CaptureFixture[str],
    local_tz: Callable[[str], None],
) -> None:
    local_tz("Australia/Perth")
    args = ["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet"]
    mock_feed(feed_body)
    main(args)
    mock_feed(feed_body.replace(b"long-delayed housing bill", b"housing bill after long delay"))
    main(args)
    capsys.readouterr()

    assert main(["changes", "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert "CHANGED (AWST)" in capsys.readouterr().out
    assert main(["changes", "--db", str(db_path), "--quiet", "--utc"]) == EXIT_OK
    assert "CHANGED (UTC)" in capsys.readouterr().out

    sources = ["sources", "--sources", str(config_path), "--db", str(db_path), "--quiet"]
    assert main(sources) == EXIT_OK
    assert "LAST SUCCESS (AWST)" in capsys.readouterr().out


# --------------------------------------------------------------------------
# live blogs in the CLI
# --------------------------------------------------------------------------


def seed_live(db_path: Path) -> None:
    conn = connect(db_path)
    live_url = "https://example.org/world/live/2026/oct/02/storm"
    base = datetime.now(UTC) - timedelta(hours=3)
    for hours, title in enumerate(
        ["Storm live: winds reach 120km/h", "Storm live: power cut to 40,000 homes"]
    ):
        store_headlines(
            conn,
            [
                Headline.create(
                    source="Example Wire",
                    title=title,
                    url=live_url,
                    fetched_at=base + timedelta(hours=hours),
                )
            ],
        )
    for hours, title in enumerate(["Ferry service suspended by storm", "Ferry back after storm"]):
        store_headlines(
            conn,
            [
                Headline.create(
                    source="Example Wire",
                    title=title,
                    url="https://example.org/ferry",
                    fetched_at=base + timedelta(hours=hours),
                )
            ],
        )
    conn.close()


def test_changes_hides_live_blogs_by_default(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_live(db_path)
    assert main(["changes", "--db", str(db_path), "--quiet"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Ferry back after storm" in out
    assert "Storm live" not in out
    # One live rewrite; the blog's first headline is not counted as a change.
    assert "1 live-blog headline(s) hidden; use --include-live or --live-only" in out

    assert main(["changes", "--db", str(db_path), "--quiet", "--include-live"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Storm live: power cut" in out
    assert "hidden" not in out


def test_live_only_is_a_timeline_with_the_first_headline(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_live(db_path)
    args = ["changes", "--db", str(db_path), "--quiet", "--live-only", "--oldest-first"]
    assert main([*args, "--format", "json"]) == EXIT_OK
    timeline = json.loads(capsys.readouterr().out)
    assert [(c["old_title"], c["new_title"], c["is_live"]) for c in timeline] == [
        (None, "Storm live: winds reach 120km/h", True),
        ("Storm live: winds reach 120km/h", "Storm live: power cut to 40,000 homes", True),
    ]
    assert main(args) == EXIT_OK
    assert "(first seen)" in capsys.readouterr().out


def test_include_and_only_live_are_mutually_exclusive(db_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["changes", "--db", str(db_path), "--include-live", "--live-only"])


def test_list_tags_live_blogs(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_live(db_path)
    base = ["list", "--sources", str(config_path), "--db", str(db_path), "--quiet"]
    assert main(base) == EXIT_OK
    out = capsys.readouterr().out
    assert "[LIVE] Storm live: power cut" in out
    assert "[LIVE] Ferry" not in out
    assert main([*base, "--format", "json"]) == EXIT_OK
    flags = {item["title"]: item["is_live"] for item in json.loads(capsys.readouterr().out)}
    assert flags == {"Storm live: power cut to 40,000 homes": True, "Ferry back after storm": False}


@respx.mock
def test_fetch_summary_reports_live_retitles(
    config_path: Path, db_path: Path, feed_body: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    live_feed = feed_body.replace(
        b"Parliament passes long-delayed housing bill", b"Parliament live: housing bill debate"
    )
    args = ["fetch", "--sources", str(config_path), "--db", str(db_path)]
    mock_feed(live_feed)
    assert main(args) == EXIT_OK
    mock_feed(live_feed.replace(b"housing bill debate", b"housing bill passes third reading"))
    assert main(args) == EXIT_OK
    assert "1 retitled (1 live)" in capsys.readouterr().err


def test_unchanged_live_blog_hides_nothing(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conn = connect(db_path)
    store_headlines(
        conn,
        [
            Headline.create(
                source="Example Wire",
                title="Storm live: winds reach 120km/h",
                url="https://example.org/world/live/2026/oct/02/storm",
            )
        ],
    )
    conn.close()
    assert main(["changes", "--db", str(db_path), "--quiet"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "No headline changes found." in out
    assert "hidden" not in out


# --------------------------------------------------------------------------
# search --history
# --------------------------------------------------------------------------


def seed_retitled(db_path: Path) -> None:
    conn = connect(db_path)
    for hours, title in enumerate(
        ["Quarantine centre to become a prison", "White elephant may become WA's newest prison"]
    ):
        store_headlines(
            conn,
            [
                Headline.create(
                    source="ABC News AU",
                    title=title,
                    url="https://example.org/quarantine-centre",
                    fetched_at=datetime.now(UTC) - timedelta(hours=5 - hours),
                )
            ],
        )
    conn.close()


def test_search_is_current_only_by_default(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_retitled(db_path)
    assert main(["search", "quarantine", "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert "No headlines found." in capsys.readouterr().out


def test_search_history_shows_the_matched_earlier_title(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_retitled(db_path)
    base = ["search", "quarantine", "--history", "--db", str(db_path), "--quiet"]

    assert main(base) == EXIT_OK
    out = capsys.readouterr().out
    assert "MATCHED EARLIER TITLE" in out
    assert "White elephant may become WA's newest prison" in out
    assert "Quarantine centre to become a prison" in out

    assert main([*base, "--format", "json"]) == EXIT_OK
    [item] = json.loads(capsys.readouterr().out)
    assert item["title"] == "White elephant may become WA's newest prison"
    assert item["matched_title"] == "Quarantine centre to become a prison"

    assert main([*base, "--format", "csv"]) == EXIT_OK
    [row] = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert row["matched_title"] == "Quarantine centre to become a prison"


def test_search_history_on_current_words_adds_no_column(
    db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_retitled(db_path)
    args = ["search", "elephant", "--history", "--db", str(db_path), "--quiet"]
    assert main(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "White elephant" in out
    assert "MATCHED EARLIER TITLE" not in out
