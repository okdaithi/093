"""Show the deployed Git revision and its associated GitHub pull request."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

GITHUB_API = "https://api.github.com"
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
MERGE_PATTERN = re.compile(r"^Merge pull request #(\d+)\b")


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    state: str
    url: str
    updated_at: str
    merged_at: str | None
    merge_commit_sha: str | None


def _git_output(repo_dir: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            # The installer runs as root on a checkout owned by someone else.
            ["git", "-c", f"safe.directory={repo_dir}", "-C", str(repo_dir), *args],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def _github_repository(remote: str | None) -> str | None:
    if not remote:
        return None
    if remote.startswith("git@github.com:"):
        remote = f"https://github.com/{remote.removeprefix('git@github.com:')}"
    parsed = urlsplit(remote)
    if parsed.hostname != "github.com":
        return None
    path = parsed.path.strip("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) != 2 or not all(parts):
        return None
    return "/".join(parts)


def _parse_pull_requests(payload: object) -> list[PullRequest]:
    if not isinstance(payload, list):
        raise ValueError("GitHub returned an unexpected response")

    pull_requests: list[PullRequest] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        title = item.get("title")
        state = item.get("state")
        url = item.get("html_url")
        updated_at = item.get("updated_at")
        merged_at = item.get("merged_at")
        merge_commit_sha = item.get("merge_commit_sha")
        if (
            type(number) is not int
            or not isinstance(title, str)
            or not isinstance(state, str)
            or not isinstance(url, str)
            or not isinstance(updated_at, str)
            or (merged_at is not None and not isinstance(merged_at, str))
            or (merge_commit_sha is not None and not isinstance(merge_commit_sha, str))
        ):
            continue
        pull_requests.append(
            PullRequest(number, title, state, url, updated_at, merged_at, merge_commit_sha)
        )
    return pull_requests


def _select_pull_request(payload: object, commit: str) -> PullRequest | None:
    pull_requests = _parse_pull_requests(payload)
    if not pull_requests:
        return None
    return max(
        pull_requests,
        key=lambda pr: (
            pr.merge_commit_sha is not None and pr.merge_commit_sha.lower() == commit.lower(),
            pr.merged_at is not None,
            pr.merged_at or "",
            pr.updated_at,
        ),
    )


def _fetch_pull_request(repository: str, commit: str) -> PullRequest | None:
    request = urllib.request.Request(
        f"{GITHUB_API}/repos/{repository}/commits/{commit}/pulls",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "headliner-deployer",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.load(response)
    return _select_pull_request(payload, commit)


def _latest_merge(repo_dir: Path) -> dict[str, Any] | None:
    """The newest merge commit reachable from HEAD, read from local Git only."""
    out = _git_output(repo_dir, "log", "--merges", "-1", "--format=%H%x1f%cI%x1f%s%x1f%b")
    if not out:
        return None
    commit, date, subject, body = [*out.split("\x1f"), "", "", ""][:4]
    match = MERGE_PATTERN.match(subject)
    title = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return {
        "commit": commit,
        "date": date,
        "subject": subject,
        "pr": int(match.group(1)) if match else None,
        "title": title or None,
    }


def _lookup(repository: str | None, commit: str) -> PullRequest | None:
    if repository is None:
        return None
    try:
        return _fetch_pull_request(repository, commit)
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def build_record(repo_dir: Path, *, now: datetime | None = None) -> dict[str, Any]:
    """What the web viewer shows about the installed code (saved as _build.json)."""
    commit = _git_output(repo_dir, "rev-parse", "HEAD")
    if commit is None or not COMMIT_PATTERN.fullmatch(commit):
        commit = None
    dirty = _git_output(repo_dir, "status", "--porcelain")
    repository = _github_repository(_git_output(repo_dir, "config", "--get", "remote.origin.url"))
    pull_request = _lookup(repository, commit) if commit else None
    return {
        "commit": commit,
        "committed_at": _git_output(repo_dir, "show", "-s", "--format=%cI", "HEAD")
        if commit
        else None,
        "branch": _git_output(repo_dir, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(dirty) if dirty is not None else None,
        "repository": repository,
        "built_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "merge": _latest_merge(repo_dir),
        "pr": asdict(pull_request) if pull_request else None,
    }


def show_deployment_info(repo_dir: Path) -> None:
    print("Deployment code:")
    commit = _git_output(repo_dir, "rev-parse", "HEAD")
    if commit is None or not COMMIT_PATTERN.fullmatch(commit):
        print("  Commit: unavailable (could not read Git revision)")
        print("  GitHub PR: unavailable (no deployed commit to look up)")
        return

    print(f"  Commit: {commit}")
    dirty_state = _git_output(repo_dir, "status", "--porcelain")
    if dirty_state is None:
        print("  Working tree: status unavailable")
    elif dirty_state:
        print("  Working tree: modified (PR details describe committed code only)")
    else:
        print("  Working tree: clean")

    repository = _github_repository(_git_output(repo_dir, "config", "--get", "remote.origin.url"))
    if repository is None:
        print("  GitHub PR: unavailable (origin is not a GitHub repository)")
        return

    try:
        pull_request = _fetch_pull_request(repository, commit)
    except urllib.error.HTTPError as exc:
        print(f"  GitHub PR: lookup unavailable (GitHub API returned HTTP {exc.code})")
        return
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        detail = str(exc) or type(exc).__name__
        print(f"  GitHub PR: lookup unavailable ({detail})")
        return

    if pull_request is None:
        print("  GitHub PR: none associated with this commit")
        return

    title = " ".join(pull_request.title.split())
    status = "merged" if pull_request.merged_at else pull_request.state
    print(f"  GitHub PR: #{pull_request.number} - {title}")
    print(f"  Status: {status}")
    print(f"  Updated (UTC): {pull_request.updated_at}")
    if pull_request.merged_at:
        print(f"  Merged (UTC): {pull_request.merged_at}")
    print(f"  URL: {pull_request.url}")


def main(argv: list[str]) -> None:
    """`deployment_info.py REPO` prints; `deployment_info.py REPO --json PATH` saves."""
    repo_dir = Path(argv[0])
    if len(argv) == 3 and argv[1] == "--json":
        Path(argv[2]).write_text(json.dumps(build_record(repo_dir), indent=2) + "\n")
        return
    show_deployment_info(repo_dir)


if __name__ == "__main__":
    main(sys.argv[1:])
