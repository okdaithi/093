"""The systemd units shipped in deploy/: shape checks systemd would otherwise find at deploy."""

from __future__ import annotations

from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SERVICES = sorted(DEPLOY.glob("*.service"))


def directives(path: Path, name: str) -> list[str]:
    """Values of `name=` lines that are not comments."""
    prefix = f"{name}="
    return [
        line.strip()[len(prefix) :]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(prefix)
    ]


@pytest.mark.parametrize("path", SERVICES, ids=lambda p: p.name)
def test_each_service_has_exactly_one_exec_start(path: Path) -> None:
    (command,) = directives(path, "ExecStart")
    assert command.startswith("/"), f"ExecStart must be an absolute path, got {command!r}"


def test_the_viewer_listens_on_localhost_only() -> None:
    (command,) = directives(DEPLOY / "headliner-web.service", "ExecStart")
    words = command.split()
    assert words[words.index("--host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in words


def test_the_fetch_service_waits_for_the_network_and_retries_on_exit_3() -> None:
    fetch = DEPLOY / "headliner.service"
    (command,) = directives(fetch, "ExecStart")
    assert "--wait-network" in command
    assert directives(fetch, "OnFailure") == ["headliner-retry.service"]
    assert directives(DEPLOY / "headliner-catchup.service", "OnFailure") == []
