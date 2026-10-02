"""Consistent database backups with daily/weekly rotation: `headliner backup`.

Title history cannot be re-fetched, so the database is the one thing worth
backing up. SQLite's online backup API copies a consistent snapshot even while
a fetch is writing (WAL mode). Each copy is written to a temporary name, checked
with `PRAGMA integrity_check`, made private (0600) and only then renamed into
place, so a half-written or corrupt file never looks like a backup.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final

from headliner.store import connect_readonly

logger = logging.getLogger(__name__)

DEFAULT_KEEP_DAILY: Final = 7
DEFAULT_KEEP_WEEKLY: Final = 4
_NAME: Final = re.compile(r"^(?P<stem>.+)-(?P<stamp>\d{8}T\d{6}Z)\.db$")


class BackupError(RuntimeError):
    """The copy could not be made or failed its integrity check."""


@dataclass(frozen=True, slots=True)
class Backup:
    path: Path
    taken_at: datetime
    size: int


def default_dir(db_path: Path) -> Path:
    """`backups/` next to the database."""
    return db_path.resolve().parent / "backups"


def list_backups(directory: Path, stem: str) -> list[Backup]:
    """Backups of the database named `stem` in `directory`, newest first."""
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        match = _NAME.match(path.name)
        if match is None or match["stem"] != stem:
            continue
        taken = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        found.append(Backup(path=path, taken_at=taken, size=path.stat().st_size))
    return sorted(found, key=lambda backup: backup.taken_at, reverse=True)


def take_backup(db_path: Path, directory: Path, *, now: datetime | None = None) -> Backup:
    """Copy `db_path` into `directory` as `<stem>-<UTC stamp>.db`, verified, mode 0600."""
    if not db_path.exists():
        raise BackupError(f"no database at {db_path}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    taken = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    target = directory / f"{db_path.stem}-{taken:%Y%m%dT%H%M%SZ}.db"
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        # Private from the first byte, not just once complete.
        partial.touch(mode=0o600)
        with (
            closing(connect_readonly(db_path)) as source,
            closing(sqlite3.connect(partial)) as copy,
        ):
            source.backup(copy)
            result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise BackupError(f"integrity check failed on the copy: {result}")
        partial.chmod(0o600)
        partial.replace(target)
    except (sqlite3.Error, OSError) as exc:
        raise BackupError(f"backup of {db_path} failed: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)
    return Backup(path=target, taken_at=taken, size=target.stat().st_size)


def prune(
    backups: list[Backup], *, keep_daily: int, keep_weekly: int
) -> tuple[list[Backup], list[Backup]]:
    """Split `backups` (newest first) into (kept, removable).

    Kept: the newest backup of each of the `keep_daily` most recent days, and of
    each of the `keep_weekly` most recent ISO weeks. The newest is always kept.
    """
    keep: set[Path] = set()
    days: set[date] = set()
    weeks: set[tuple[int, int]] = set()
    for backup in backups:
        day = backup.taken_at.date()
        iso = backup.taken_at.isocalendar()
        week = (iso.year, iso.week)
        if day not in days and len(days) < keep_daily:
            days.add(day)
            keep.add(backup.path)
        if week not in weeks and len(weeks) < keep_weekly:
            weeks.add(week)
            keep.add(backup.path)
    if backups:
        keep.add(backups[0].path)
    kept = [backup for backup in backups if backup.path in keep]
    removable = [backup for backup in backups if backup.path not in keep]
    return kept, removable


def run_backup(
    db_path: Path,
    directory: Path | None = None,
    *,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    keep_weekly: int = DEFAULT_KEEP_WEEKLY,
    now: datetime | None = None,
) -> tuple[Backup, list[Backup]]:
    """Take a backup, then delete the ones rotation no longer keeps. Returns (new, removed)."""
    target_dir = directory or default_dir(db_path)
    made = take_backup(db_path, target_dir, now=now)
    _, removable = prune(
        list_backups(target_dir, db_path.stem), keep_daily=keep_daily, keep_weekly=keep_weekly
    )
    for old in removable:
        old.path.unlink(missing_ok=True)
    return made, removable
