from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
from deploy import deployment_info

COMMIT = "a" * 40


def pull_request(
    number: int,
    *,
    merge_commit_sha: str | None = None,
    merged_at: str | None = None,
    updated_at: str = "2026-10-01T00:00:00Z",
) -> dict[str, object]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "closed" if merged_at else "open",
        "html_url": f"https://github.com/owner/repo/pull/{number}",
        "updated_at": updated_at,
        "merged_at": merged_at,
        "merge_commit_sha": merge_commit_sha,
    }


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("https://example.com/owner/repo.git", None),
        (None, None),
    ],
)
def test_github_repository(remote: str | None, expected: str | None) -> None:
    assert deployment_info._github_repository(remote) == expected


def test_selects_pr_for_exact_merge_commit() -> None:
    payload = [
        pull_request(1, merged_at="2026-09-30T00:00:00Z"),
        pull_request(2, merge_commit_sha=COMMIT, merged_at="2026-09-01T00:00:00Z"),
    ]

    result = deployment_info._select_pull_request(payload, COMMIT)

    assert result is not None
    assert result.number == 2


def test_prints_pr_details_for_deployed_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr = deployment_info.PullRequest(
        number=17,
        title=" Improve installer output ",
        state="closed",
        url="https://github.com/owner/repo/pull/17",
        updated_at="2026-10-01T12:00:00Z",
        merged_at="2026-10-01T11:00:00Z",
        merge_commit_sha=COMMIT,
    )

    def git_output(_repo: Path, *args: str) -> str | None:
        if args == ("rev-parse", "HEAD"):
            return COMMIT
        if args == ("status", "--porcelain"):
            return ""
        if args == ("config", "--get", "remote.origin.url"):
            return "https://github.com/owner/repo.git"
        raise AssertionError(f"Unexpected git command: {args}")

    monkeypatch.setattr(deployment_info, "_git_output", git_output)
    monkeypatch.setattr(deployment_info, "_fetch_pull_request", lambda *_: pr)

    deployment_info.show_deployment_info(Path())

    output = capsys.readouterr().out
    assert f"Commit: {COMMIT}" in output
    assert "GitHub PR: #17 - Improve installer output" in output
    assert "Updated (UTC): 2026-10-01T12:00:00Z" in output
    assert "Merged (UTC): 2026-10-01T11:00:00Z" in output
    assert "https://github.com/owner/repo/pull/17" in output


def test_github_failure_does_not_hide_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def git_output(_repo: Path, *args: str) -> str | None:
        if args == ("rev-parse", "HEAD"):
            return COMMIT
        if args == ("status", "--porcelain"):
            return ""
        if args == ("config", "--get", "remote.origin.url"):
            return "https://github.com/owner/repo.git"
        raise AssertionError(f"Unexpected git command: {args}")

    def fail_lookup(*_args: str) -> None:
        raise urllib.error.URLError("network unavailable")

    monkeypatch.setattr(deployment_info, "_git_output", git_output)
    monkeypatch.setattr(deployment_info, "_fetch_pull_request", fail_lookup)

    deployment_info.show_deployment_info(Path())

    output = capsys.readouterr().out
    assert f"Commit: {COMMIT}" in output
    assert "GitHub PR: lookup unavailable" in output
    assert "network unavailable" in output


def test_build_record_names_the_latest_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    def git_output(_repo: Path, *args: str) -> str | None:
        answers = {
            ("rev-parse", "HEAD"): COMMIT,
            ("status", "--porcelain"): " M README.md",
            ("config", "--get", "remote.origin.url"): "git@github.com:owner/repo.git",
            ("show", "-s", "--format=%cI", "HEAD"): "2026-10-03T05:06:04+08:00",
            ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        }
        if args[:2] == ("log", "--merges"):
            fields = (COMMIT, "2026-10-03T05:06:04+08:00", "Merge pull request #28 from o/b")
            return "\x1f".join((*fields, "\nShow PR details\n"))
        return answers[args]

    def fail_lookup(*_args: str) -> None:
        raise urllib.error.URLError("network unavailable")

    monkeypatch.setattr(deployment_info, "_git_output", git_output)
    monkeypatch.setattr(deployment_info, "_fetch_pull_request", fail_lookup)

    record = deployment_info.build_record(Path())

    assert record["commit"] == COMMIT
    assert record["dirty"] is True
    assert record["repository"] == "owner/repo"
    assert record["pr"] is None
    assert record["merge"]["pr"] == 28
    assert record["merge"]["title"] == "Show PR details"
