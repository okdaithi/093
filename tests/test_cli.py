"""CLI wiring: exit codes, output formats and the dry-run guarantee."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from headliner.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL_FAILURE, main, parse_duration
from headliner.models import Headline
from headliner.store import connect, insert_headlines

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
    assert rows[0] == ["source", "published_at", "title", "url", "summary"]
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
    assert "schema version 0 -> 1" in out
    assert "would merge 1 row(s) across 1 URL(s)" in out
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
