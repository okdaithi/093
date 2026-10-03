"""GitHub notifications: delivery order, issue lifecycle, heartbeat, and the viewer banner."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from headliner import notify, watchdog
from headliner.cli import main
from headliner.watchdog import FAIL, OK
from tests.test_watchdog import make_ctx

T0 = datetime(2026, 10, 3, 5, 24, tzinfo=UTC)
PERTH = datetime.now(UTC).astimezone().tzinfo


class FakeGitHub:
    """Records calls and plays the part of the issues API."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.issues: dict[int, dict[str, Any]] = {}
        self.down = False
        self.status = 200
        self.next_number = 100

    def __call__(
        self, method: str, url: str, token: str, payload: dict[str, Any] | None
    ) -> tuple[int, Any]:
        assert token == "secret-token"
        path = url.split("/repos/okdaithi/093", 1)[1]
        self.calls.append((method, path, payload))
        if self.down:
            raise notify.NotifyError("cannot reach GitHub (OSError)")
        if self.status >= 300:
            return self.status, None
        if method == "POST" and path == "/issues":
            self.next_number += 1
            self.issues[self.next_number] = {
                "number": self.next_number,
                "title": payload["title"],  # type: ignore[index]
                "body": payload["body"],  # type: ignore[index]
                "state": "open",
                "comments": [],
            }
            return 201, {"number": self.next_number}
        if method == "GET":
            return 200, [
                {"number": n, "title": i["title"]}
                for n, i in self.issues.items()
                if i["state"] == "open"
            ] + [{"number": 7, "title": notify.HEARTBEAT_TITLE, "pull_request": {}}]
        number = int(path.split("/")[2])
        if path.endswith("/comments"):
            self.issues[number]["comments"].append(payload["body"])  # type: ignore[index]
        elif payload:
            self.issues[number].update({k: v for k, v in payload.items() if k in {"state", "body"}})
        return 200, {}


@pytest.fixture
def github() -> FakeGitHub:
    return FakeGitHub()


def client(fake: FakeGitHub) -> notify.GitHub:
    return notify.GitHub("secret-token", "okdaithi/093", transport=fake)


def event(kind: str, check: str = "dns", level: str = FAIL, at_minutes: int = 0) -> dict[str, str]:
    at = T0 + timedelta(minutes=at_minutes)
    return {
        "kind": kind,
        "check": check,
        "level": level,
        "detail": "no test host resolves",
        "since": T0.isoformat(),
        "at": at.isoformat(),
    }


def test_alert_opens_one_issue_then_reminders_and_recovery_use_it(github: FakeGitHub) -> None:
    state: dict[str, Any] = {"pending": [event("alert"), event("remind", at_minutes=360)]}
    assert notify.deliver(state, client(github), PERTH) == 2
    assert state["pending"] == []
    (number,) = state["issues"].values()
    issue = github.issues[number]
    assert issue["title"] == "headliner health: DNS not resolving"
    assert "no test host resolves" in issue["body"]
    assert "wg show proton0" in issue["body"]  # the runbook hint for DNS
    assert issue["comments"] == [
        "Still fail at "
        + notify._when(event("remind", at_minutes=360)["at"], PERTH)
        + ": no test host resolves"
    ]

    state["pending"] = [event("recover", level=OK, at_minutes=450)]
    notify.deliver(state, client(github), PERTH)
    assert github.issues[number]["state"] == "closed"
    assert "Recovered at" in github.issues[number]["comments"][-1]
    assert "7.5 h" in github.issues[number]["comments"][-1]
    assert state["issues"] == {}


def test_a_recovery_with_no_open_issue_records_the_whole_outage(github: FakeGitHub) -> None:
    state: dict[str, Any] = {
        "pending": [event("alert"), event("recover", level=OK, at_minutes=328)]
    }
    notify.deliver(state, client(github), PERTH)
    (issue,) = github.issues.values()
    assert issue["state"] == "closed"
    assert "Recovered at" in issue["comments"][0]
    assert "5.5 h" in issue["comments"][0]


def test_events_are_kept_in_order_when_github_is_unreachable(github: FakeGitHub) -> None:
    github.down = True
    state: dict[str, Any] = {"pending": [event("alert"), event("remind", at_minutes=360)]}
    with pytest.raises(notify.NotifyError):
        notify.deliver(state, client(github), PERTH)
    assert [e["kind"] for e in state["pending"]] == ["alert", "remind"]
    github.down = False
    assert notify.deliver(state, client(github), PERTH) == 2


def test_a_failed_comment_does_not_open_a_second_issue(github: FakeGitHub) -> None:
    state: dict[str, Any] = {"pending": [event("recover", level=OK)]}
    real = github.__call__

    def flaky(method: str, url: str, token: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        if url.endswith("/comments"):
            return 500, None
        return real(method, url, token, payload)

    with pytest.raises(notify.NotifyError):
        notify.deliver(state, notify.GitHub("secret-token", "okdaithi/093", transport=flaky), PERTH)
    assert len(github.issues) == 1
    notify.deliver(state, client(github), PERTH)
    assert len(github.issues) == 1  # the retry reused the issue it had already opened


def test_permission_errors_say_what_the_token_needs(github: FakeGitHub) -> None:
    github.status = 403
    with pytest.raises(notify.NotifyError, match="Issues read/write") as caught:
        client(github).create_issue("t", "b")
    assert "secret-token" not in str(caught.value)


def test_heartbeat_is_created_once_then_edited(github: FakeGitHub) -> None:
    state: dict[str, Any] = {}
    body = notify.heartbeat_body(T0, "nuc", "abc1234 · PR #35", ["dns"])
    notify.beat(state, client(github), body)
    number = state["heartbeat_issue"]
    assert github.issues[number]["title"] == notify.HEARTBEAT_TITLE
    assert "last_ok: 2026-10-03T05:24:00+00:00" in body and "failing: dns" in body
    later = notify.heartbeat_body(T0 + timedelta(minutes=10), "nuc", "abc1234", [])
    notify.beat(state, client(github), later)
    assert github.issues[number]["body"] == later
    assert [c[0] for c in github.calls].count("POST") == 1


def test_heartbeat_adopts_an_existing_issue_and_survives_a_deleted_one(
    github: FakeGitHub,
) -> None:
    existing = client(github).create_issue(notify.HEARTBEAT_TITLE, "old")
    state: dict[str, Any] = {}
    notify.beat(state, client(github), "new")
    assert state["heartbeat_issue"] == existing and github.issues[existing]["body"] == "new"
    state["heartbeat_issue"] = 999  # deleted on GitHub: the PATCH 404s
    github.issues.pop(existing)
    real = github.__call__

    def gone(method: str, url: str, token: str, payload: dict[str, Any] | None) -> tuple[int, Any]:
        if url.endswith("/issues/999"):
            return 404, None
        return real(method, url, token, payload)

    notify.beat(state, notify.GitHub("secret-token", "okdaithi/093", transport=gone), "again")
    assert state["heartbeat_issue"] != 999


def test_token_file(tmp_path: Path) -> None:
    path = tmp_path / "token"
    assert notify.load_token(path) is None
    path.write_text("  \n")
    assert notify.load_token(path) is None
    path.write_text("secret-token\n")
    assert notify.load_token(path) == "secret-token"


def test_an_outage_is_reported_once_the_network_is_back(tmp_path: Path, github: FakeGitHub) -> None:
    """The 2026-10-03 story: down for a while (nothing can be sent), then recovered."""
    state_path = tmp_path / "watchdog.json"
    notifier = notify.Notifier(client(github), host="nuc", build="test")
    up = {"dns": True}
    ctx_args = {"resolve": lambda _host: up["dns"]}

    def tick(minutes: int) -> None:
        ctx = make_ctx(tmp_path, now=T0 + timedelta(minutes=minutes), **ctx_args)
        watchdog.run_once(ctx, state_path, self_heal=False, notifier=notifier)

    tick(0)  # healthy
    up["dns"] = False
    tick(10)
    github.down = True  # the same outage that stops DNS stops GitHub too
    tick(20)  # alert is raised but cannot be sent
    tick(30)
    assert json.loads(state_path.read_text())["pending"][0]["kind"] == "alert"
    assert [i["title"] for i in github.issues.values()] == [notify.HEARTBEAT_TITLE]
    up["dns"] = True
    github.down = False
    tick(40)

    saved = json.loads(state_path.read_text())
    assert saved["pending"] == []
    titles = [i["title"] for i in github.issues.values()]
    assert "headliner health: DNS not resolving" in titles
    dns = next(i for i in github.issues.values() if "DNS" in i["title"])
    assert dns["state"] == "closed" and "Recovered at" in dns["comments"][-1]
    heartbeat = next(i for i in github.issues.values() if i["title"] == notify.HEARTBEAT_TITLE)
    assert "last_ok: 2026-10-03T06:04:00+00:00" in heartbeat["body"]


def test_watchdog_command_stays_quiet_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(watchdog, "tcp_connect", lambda _h, _p: True)
    monkeypatch.setattr(watchdog, "run_command", lambda _cmd: (0, "active"))
    monkeypatch.setattr(watchdog, "http_healthy", lambda _u: True)
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        'settings:\n  user_agent: "t/0.1 (+contact: t@example.org)"\n'
        "sources:\n  - {name: A, url: 'https://a.example/rss', type: rss}\n",
        encoding="utf-8",
    )
    code = main(
        [
            "watchdog", "--sources", str(sources), "--db", str(tmp_path / "h.db"),
            "--state", str(tmp_path / "s.json"), "--github-token-file", str(tmp_path / "none"),
            "--no-heal", "--quiet",
        ]
    )  # fmt: skip
    assert code == 0
    assert "heartbeat_issue" not in json.loads((tmp_path / "s.json").read_text())


# -- Viewer banner -----------------------------------------------------------------


def write_state(path: Path, **checks: dict[str, str]) -> None:
    now = datetime.now(UTC)
    path.write_text(
        json.dumps(
            {
                "updated_at": now.isoformat(),
                "checks": {
                    name: {"since": (now - timedelta(minutes=30)).isoformat(), **entry}
                    for name, entry in checks.items()
                },
            }
        )
    )


def test_banner_shows_announced_problems_only(tmp_path: Path) -> None:
    from tests.test_web import CONFIG, WebApp, make_client

    sources = tmp_path / "sources.yaml"
    sources.write_text(CONFIG, encoding="utf-8")
    db = tmp_path / "headlines.db"
    from headliner.store import connect

    connect(db).close()
    get = make_client(WebApp(db, sources))
    assert 'role="alert"' not in get("/latest")[2]  # no state file: nothing to show

    write_state(
        tmp_path / "watchdog.json",
        dns={"level": "fail", "detail": "no host resolves", "notified_level": "fail"},
        disk={"level": "warn", "detail": "91% full", "notified_level": "ok"},
    )
    body = get("/latest")[2]
    assert 'role="alert"' in body and "no host resolves" in body
    assert "91% full" not in body  # warned but not yet announced
    assert json.loads(get("/api/status")[2])["watchdog"][0]["check"] == "dns"


def test_banner_notices_a_silent_watchdog(tmp_path: Path) -> None:
    from tests.test_web import CONFIG, WebApp, make_client

    sources = tmp_path / "sources.yaml"
    sources.write_text(CONFIG, encoding="utf-8")
    db = tmp_path / "headlines.db"
    from headliner.store import connect

    connect(db).close()
    stale = datetime.now(UTC) - timedelta(hours=2)
    (tmp_path / "watchdog.json").write_text(
        json.dumps({"updated_at": stale.isoformat(), "checks": {}})
    )
    body = make_client(WebApp(db, sources))("/latest")[2]
    assert "the health watchdog has not reported" in body


def refusing(fake: FakeGitHub, suffix: str, status: int):  # type: ignore[no-untyped-def]
    """A transport that answers `status` to calls whose path ends with `suffix`."""

    def transport(
        method: str, url: str, token: str, payload: dict[str, Any] | None
    ) -> tuple[int, Any]:
        if url.endswith(suffix):
            return status, None
        return fake(method, url, token, payload)

    return notify.GitHub("secret-token", "okdaithi/093", transport=transport)


def test_a_comment_on_a_deleted_issue_opens_a_new_one(github: FakeGitHub) -> None:
    state: dict[str, Any] = {"issues": {"dns": 999}, "pending": [event("remind")]}
    notify.deliver(state, refusing(github, "/issues/999/comments", 404), PERTH)
    assert state["pending"] == []
    assert state["issues"]["dns"] != 999
    assert github.issues[state["issues"]["dns"]]["title"].endswith("DNS not resolving")


def test_a_recovery_for_a_deleted_issue_is_simply_forgotten(github: FakeGitHub) -> None:
    state: dict[str, Any] = {"issues": {"dns": 999}, "pending": [event("recover", level=OK)]}
    notify.deliver(state, refusing(github, "/issues/999/comments", 410), PERTH)
    assert state["pending"] == [] and state["issues"] == {}
    assert github.issues == {}  # nothing new was opened for an already-resolved problem


def test_a_refused_comment_does_not_stop_the_heartbeat(tmp_path: Path, github: FakeGitHub) -> None:
    """A healthy NUC must not look dead because GitHub rejects one comment."""
    state_path = tmp_path / "watchdog.json"
    issue = client(github).create_issue("headliner health: DNS not resolving", "x")
    state_path.write_text(
        json.dumps(
            {
                "issues": {"dns": issue},
                "pending": [event("remind")],
                "heartbeat_issue": None,
            }
        )
    )
    notifier = notify.Notifier(refusing(github, f"/issues/{issue}/comments", 500), "nuc", "test")
    watchdog.run_once(make_ctx(tmp_path, now=T0), state_path, self_heal=False, notifier=notifier)
    heartbeat = [i for i in github.issues.values() if i["title"] == notify.HEARTBEAT_TITLE]
    assert heartbeat and "last_ok: 2026-10-03T05:24:00+00:00" in heartbeat[0]["body"]
    assert json.loads(state_path.read_text())["pending"][0]["kind"] == "remind"  # still queued


def test_banner_survives_a_damaged_state_file(tmp_path: Path) -> None:
    from tests.test_web import CONFIG, WebApp, make_client

    sources = tmp_path / "sources.yaml"
    sources.write_text(CONFIG, encoding="utf-8")
    db = tmp_path / "headlines.db"
    from headliner.store import connect

    connect(db).close()
    write_state(
        tmp_path / "watchdog.json",
        dns={"level": "fail", "detail": "no host", "notified_level": "fail", "since": "garbage"},
    )
    status, _, body = make_client(WebApp(db, sources))("/latest")
    assert status.startswith("200") and "no host" in body
