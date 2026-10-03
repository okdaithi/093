"""The installed-build record the web viewer shows."""

from __future__ import annotations

from typing import Any

from headliner import build


def record(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "commit": "abcdef0123456789abcdef0123456789abcdef01",
        "built_at": "2026-10-03T01:02:03+00:00",
        "repository": "owner/repo",
        "dirty": False,
        "merge": {"commit": "abc", "date": "2026-10-03T00:00:00Z", "pr": 7, "title": "Old"},
        "pr": {"number": 9, "title": "  New   thing ", "url": "https://github.com/x/y/pull/9"},
    }
    data.update(changes)
    return data


def test_github_pr_wins_over_latest_merge() -> None:
    found = build.parse(record())
    assert found is not None
    assert found.label == "abcdef0 · PR #9 New thing"
    assert found.pr_url == "https://github.com/x/y/pull/9"
    assert found.built_at is not None and found.built_at.tzinfo is not None


def test_latest_merge_is_the_offline_fallback() -> None:
    found = build.parse(record(pr=None))
    assert found is not None
    assert (found.pr_number, found.pr_title) == (7, "Old")
    assert found.pr_url == "https://github.com/owner/repo/pull/7"


def test_bad_records_degrade() -> None:
    assert build.parse([]) is None
    found = build.parse({"commit": 5, "built_at": "yesterday", "merge": "x", "dirty": "yes"})
    assert found is not None
    assert found.label == "unknown"
    assert found.built_at is None
    assert not found.dirty


def test_load_without_a_file_is_a_development_build() -> None:
    assert build.load() is None
