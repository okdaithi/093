"""Which code is running: the record deploy/install-ubuntu.sh saves at install time.

The installer writes headliner/_build.json (commit, latest merge, GitHub pull
request, build time) into the package it installs. A checkout run in place has
no such file; it is then a development build.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from importlib import resources
from typing import Any

BUILD_FILE = "_build.json"


@dataclass(frozen=True, slots=True)
class Build:
    commit: str | None
    committed_at: datetime | None
    branch: str | None
    dirty: bool
    built_at: datetime | None
    repository: str | None
    merge_commit: str | None
    merge_at: datetime | None
    pr_number: int | None
    pr_title: str | None
    pr_url: str | None
    raw: dict[str, Any]

    @property
    def short(self) -> str:
        return self.commit[:7] if self.commit else "unknown"

    @property
    def label(self) -> str:
        """e.g. "abc1234 · PR #25 Story framing"."""
        parts = [self.short]
        if self.pr_number is not None:
            parts.append(f"PR #{self.pr_number}" + (f" {self.pr_title}" if self.pr_title else ""))
        return " · ".join(parts)


def _when(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def parse(data: Any) -> Build | None:
    if not isinstance(data, dict):
        return None
    merge: dict[str, Any] = data["merge"] if isinstance(data.get("merge"), dict) else {}
    pr: dict[str, Any] = data["pr"] if isinstance(data.get("pr"), dict) else {}
    # The GitHub lookup names the PR for this exact commit; the latest merge in
    # local history is the offline fallback.
    number = pr.get("number") if isinstance(pr.get("number"), int) else merge.get("pr")
    number = number if isinstance(number, int) else None
    title = _text(pr.get("title")) or _text(merge.get("title"))
    repository = _text(data.get("repository"))
    url = _text(pr.get("url"))
    if url is None and number is not None and repository:
        url = f"https://github.com/{repository}/pull/{number}"
    return Build(
        commit=_text(data.get("commit")),
        committed_at=_when(data.get("committed_at")),
        branch=_text(data.get("branch")),
        dirty=data.get("dirty") is True,
        built_at=_when(data.get("built_at")),
        repository=repository,
        merge_commit=_text(merge.get("commit")),
        merge_at=_when(merge.get("date")),
        pr_number=number,
        pr_title=" ".join(title.split()) if title else None,
        pr_url=url,
        raw=data,
    )


def load() -> Build | None:
    """The installed build, or None for a development checkout or an unreadable file."""
    try:
        text = (resources.files("headliner") / BUILD_FILE).read_text(encoding="utf-8")
        return parse(json.loads(text))
    except (OSError, ValueError):
        return None
