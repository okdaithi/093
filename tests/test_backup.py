"""Verified backups and their daily/weekly rotation."""

from __future__ import annotations

import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from headliner.backup import Backup, BackupError, list_backups, prune, run_backup, take_backup
from headliner.cli import EXIT_FATAL, EXIT_OK, main
from headliner.models import Headline
from headliner.store import connect, store_headlines


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "headlines.db"
    with closing(connect(path)) as conn:
        store_headlines(
            conn,
            [Headline.create(source="Wire", title="A story worth keeping", url="https://e.org/a")],
        )
    return path


def test_backup_is_a_verified_private_copy(db_path: Path, tmp_path: Path) -> None:
    made = take_backup(db_path, tmp_path / "backups", now=datetime(2026, 10, 3, 3, 30, tzinfo=UTC))
    assert made.path.name == "headlines-20261003T033000Z.db"
    assert stat.S_IMODE(made.path.stat().st_mode) == 0o600
    with closing(sqlite3.connect(made.path)) as copy:
        assert copy.execute("SELECT title FROM headlines").fetchone()[0] == "A story worth keeping"
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert not list((tmp_path / "backups").glob("*.partial"))


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no database"):
        take_backup(tmp_path / "absent.db", tmp_path / "backups")


def fake(day: int, hour: int = 3) -> Backup:
    taken = datetime(2026, 9, 1, hour, tzinfo=UTC) + timedelta(days=day)
    return Backup(path=Path(f"b-{day}-{hour}.db"), taken_at=taken, size=1)


def test_rotation_keeps_daily_and_weekly() -> None:
    backups = sorted(
        [fake(day) for day in range(40)] + [fake(39, 1)], key=lambda b: b.taken_at, reverse=True
    )
    kept, removable = prune(backups, keep_daily=7, keep_weekly=4)
    kept_days = {backup.taken_at.date() for backup in kept}
    # The last 7 days, plus the newest backup of each of the last 4 ISO weeks.
    assert {fake(day).taken_at.date() for day in range(33, 40)} <= kept_days
    assert len(kept) <= 7 + 4
    weeks = {tuple(b.taken_at.isocalendar()[:2]) for b in kept}
    assert len(weeks) == 4
    assert fake(39, 1).path in {b.path for b in removable}  # older one of the same day
    assert backups[0] in kept


def test_run_backup_prunes_old_files(db_path: Path, tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    start = datetime(2026, 9, 1, 3, 30, tzinfo=UTC)
    for day in range(12):
        run_backup(db_path, directory, keep_daily=3, keep_weekly=1, now=start + timedelta(days=day))
    remaining = list_backups(directory, "headlines")
    assert len(remaining) == 3
    assert remaining[0].taken_at == start + timedelta(days=11)


def test_cli_backup(db_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert main(["backup", "--db", str(db_path), "--dir", str(out), "--quiet"]) == EXIT_OK
    assert len(list_backups(out, "headlines")) == 1
    assert main(["backup", "--db", str(tmp_path / "nope.db"), "--quiet"]) == EXIT_FATAL
    # Default location: backups/ next to the database.
    assert main(["backup", "--db", str(db_path), "--quiet"]) == EXIT_OK
    assert len(list_backups(db_path.parent / "backups", "headlines")) == 1
