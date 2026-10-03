"""Watchdog: every check with fake probes, and the alert/remind/recover state machine."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from headliner import watchdog
from headliner.cli import EXIT_FATAL, EXIT_OK, main
from headliner.store import connect, record_fetch
from headliner.watchdog import FAIL, OK, WARN, Check, Context, Probes

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


def fake_run(answers: dict[str, tuple[int, str]]) -> Callable[[Sequence[str]], tuple[int, str]]:
    def run(cmd: Sequence[str]) -> tuple[int, str]:
        return answers.get(" ".join(cmd), (0, "active"))

    return run


def make_ctx(tmp_path: Path, now: datetime = T0, **overrides: object) -> Context:
    probes = Probes(
        now=lambda: now,
        resolve=lambda _host: True,
        resolve_via=lambda _server, _host: True,
        tcp=lambda _host, _port: True,
        run=fake_run(
            {"wg show proton0 latest-handshakes": (0, f"KEY\t{int(now.timestamp()) - 20}")}
        ),
        http_ok=lambda _url: True,
        disk_percent=lambda _path: 40.0,
    )
    for name, value in overrides.items():
        setattr(probes, name, value)
    return Context(
        db_path=tmp_path / "headlines.db", hosts=["a.example", "b.example"], probes=probes
    )


def log_run(db: Path, finished: datetime, statuses: Sequence[str]) -> None:
    with connect(db) as conn:
        for index, status in enumerate(statuses):
            record_fetch(
                conn,
                source=f"Feed {index}",
                started_at=finished - timedelta(seconds=30),
                finished_at=finished,
                status=status,
                items_found=1,
                items_new=1,
                items_changed=0,
                error="HTTP 500" if status == "error" else None,
            )
        conn.commit()


def level(check: Check) -> str:
    return check.level


# -- Individual checks ------------------------------------------------------------


def test_dns_ok_when_any_test_host_resolves(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, resolve=lambda host: host == "b.example")
    assert watchdog.check_dns(ctx).level == OK


def test_dns_failure_says_where_it_broke(tmp_path: Path) -> None:
    down = make_ctx(tmp_path, resolve=lambda _host: False, resolve_via=lambda _s, _h: False)
    check = watchdog.check_dns(down)
    assert check.level == FAIL and "does not answer either" in check.detail
    between = make_ctx(tmp_path, resolve=lambda _host: False)
    assert "break is between the system" in watchdog.check_dns(between).detail


def test_internet_check_uses_addresses_not_names(tmp_path: Path) -> None:
    seen: list[str] = []

    def tcp(host: str, _port: int) -> bool:
        seen.append(host)
        return host == "9.9.9.9"

    assert watchdog.check_internet(make_ctx(tmp_path, tcp=tcp)).level == OK
    assert seen == ["1.1.1.1", "9.9.9.9"]
    assert watchdog.check_internet(make_ctx(tmp_path, tcp=lambda _h, _p: False)).level == FAIL


def test_vpn_handshake_age(tmp_path: Path) -> None:
    ok = watchdog.check_vpn(make_ctx(tmp_path))
    assert ok.level == OK
    stale = int(T0.timestamp()) - 600
    ctx = make_ctx(
        tmp_path, run=fake_run({"wg show proton0 latest-handshakes": (0, f"K\t{stale}")})
    )
    assert watchdog.check_vpn(ctx).level == FAIL
    never = make_ctx(tmp_path, run=fake_run({"wg show proton0 latest-handshakes": (0, "K\t0")}))
    assert "never completed" in watchdog.check_vpn(never).detail


def test_no_vpn_interface_is_not_a_problem(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, run=fake_run({"wg show proton0 latest-handshakes": (1, "no such")}))
    assert watchdog.check_vpn(ctx).level == OK


def test_vpn_check_that_cannot_read_wireguard_says_so(tmp_path: Path) -> None:
    denied = make_ctx(
        tmp_path,
        run=fake_run(
            {
                "wg show proton0 latest-handshakes": (
                    1,
                    "Unable to access interface: Operation not permitted",
                )
            }
        ),
    )
    assert watchdog.check_vpn(denied).level == WARN


def test_fetch_run_checks(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert watchdog.check_fetch(ctx).level == WARN  # no database yet reads as no runs
    log_run(ctx.db_path, T0 - timedelta(hours=1), ["ok"] * 9 + ["error"])
    assert watchdog.check_fetch(ctx).level == OK
    late = make_ctx(tmp_path, now=T0 + timedelta(hours=8))
    assert watchdog.check_fetch(late).level == FAIL


def test_fetch_run_mostly_failed(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    log_run(ctx.db_path, T0 - timedelta(minutes=10), ["error"] * 6 + ["ok"] * 4)
    check = watchdog.check_fetch(ctx)
    assert check.level == FAIL and "6 of 10" in check.detail


def test_failing_sources_streak(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    for hours in (3, 2, 1):
        log_run(ctx.db_path, T0 - timedelta(hours=hours), ["error"] + ["ok"] * 11)
    check = watchdog.check_sources(ctx)
    assert check.level == WARN and "Feed 0" in check.detail


def test_everything_failing_is_not_a_source_streak(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    for hours in (3, 2, 1):
        log_run(ctx.db_path, T0 - timedelta(hours=hours), ["error"] * 12)
    assert watchdog.check_sources(ctx).level == OK


def test_units_and_viewer(tmp_path: Path) -> None:
    assert watchdog.check_units(make_ctx(tmp_path)).level == OK
    down = make_ctx(
        tmp_path, run=fake_run({"systemctl is-active headliner-web.service": (3, "inactive")})
    )
    check = watchdog.check_units(down)
    assert check.level == FAIL and "headliner-web.service" in check.detail
    failed = make_ctx(
        tmp_path,
        run=fake_run(
            {"systemctl --failed --plain --no-legend": (0, "headliner.service loaded failed")}
        ),
    )
    assert "headliner.service" in watchdog.check_units(failed).detail
    assert watchdog.check_viewer(make_ctx(tmp_path, http_ok=lambda _u: False)).level == FAIL


def test_backups_and_disk(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert watchdog.check_backups(ctx).level == WARN
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "headlines-20261003T100000Z.db").write_bytes(b"x")
    assert watchdog.check_backups(ctx).level == OK
    assert watchdog.check_disk(make_ctx(tmp_path, disk_percent=lambda _p: 95.0)).level == WARN


def test_a_crashing_check_is_reported_not_fatal(tmp_path: Path) -> None:
    def boom(_host: str) -> bool:
        raise RuntimeError("probe broke")

    results = watchdog.run_checks(make_ctx(tmp_path, resolve=boom))
    crashed = next(check for check in results if check.name == "dns")
    assert crashed.level == WARN and "probe broke" in crashed.detail
    assert len(results) == len(watchdog.CHECKS)


# -- State machine ----------------------------------------------------------------


def step(state: dict, level: str, now: datetime, name: str = "dns") -> tuple[dict, list]:
    return watchdog.update_state(state, [Check(name, level, f"{level} detail")], now)


def test_failure_is_announced_only_after_the_grace_period() -> None:
    state, events = step({}, FAIL, T0)
    assert events == []
    state, events = step(state, FAIL, T0 + timedelta(minutes=10))
    assert [(e.kind, e.check, e.level) for e in events] == [("alert", "dns", FAIL)]
    assert events[0].since == "2026-10-03T12:00:00+00:00"
    # Still failing: no repeat until the reminder interval.
    state, events = step(state, FAIL, T0 + timedelta(minutes=20))
    assert events == []
    state, events = step(state, FAIL, T0 + timedelta(hours=6, minutes=11))
    assert [e.kind for e in events] == ["remind"]


def test_a_blip_never_alerts_and_recovery_needs_a_prior_alert() -> None:
    state, _ = step({}, FAIL, T0)
    state, events = step(state, OK, T0 + timedelta(minutes=10))
    assert events == []


def test_recovery_is_announced_after_an_alert() -> None:
    state, _ = step({}, FAIL, T0)
    state, _ = step(state, FAIL, T0 + timedelta(minutes=10))
    state, events = step(state, OK, T0 + timedelta(minutes=40))
    assert [(e.kind, e.check) for e in events] == [("recover", "dns")]
    assert events[0].since == "2026-10-03T12:00:00+00:00"
    state, events = step(state, OK, T0 + timedelta(minutes=50))
    assert events == []


def test_warnings_wait_longer_and_escalation_alerts_again() -> None:
    state, _ = step({}, WARN, T0)
    state, events = step(state, WARN, T0 + timedelta(minutes=30))
    assert events == []
    state, events = step(state, WARN, T0 + timedelta(minutes=60))
    assert [(e.kind, e.level) for e in events] == [("alert", WARN)]
    state, _ = step(state, FAIL, T0 + timedelta(minutes=70))
    state, events = step(state, FAIL, T0 + timedelta(minutes=80))
    assert [(e.kind, e.level) for e in events] == [("alert", FAIL)]


def test_pending_events_accumulate_until_a_notifier_takes_them() -> None:
    state, _ = step({}, FAIL, T0)
    state, _ = step(state, FAIL, T0 + timedelta(minutes=10))
    state, _ = step(state, OK, T0 + timedelta(minutes=20))
    assert [e["kind"] for e in state["pending"]] == ["alert", "recover"]
    assert json.loads(json.dumps(state)) == state


def test_checks_are_tracked_independently() -> None:
    state, _ = watchdog.update_state({}, [Check("dns", FAIL, "x"), Check("disk", OK, "y")], T0)
    state, events = watchdog.update_state(
        state, [Check("dns", FAIL, "x"), Check("disk", WARN, "z")], T0 + timedelta(minutes=10)
    )
    assert [(e.check) for e in events] == ["dns"]


# -- Self-repair ------------------------------------------------------------------


def dns_down_state(since: datetime) -> dict:
    return {"checks": {"dns": {"level": FAIL, "since": since.isoformat()}}}


def test_heal_waits_for_a_lasting_outage_and_respects_the_cooldown() -> None:
    failing = [Check("dns", FAIL, "x")]
    assert not watchdog.needs_heal({}, failing, T0)
    assert not watchdog.needs_heal(dns_down_state(T0), failing, T0 + timedelta(minutes=5))
    assert watchdog.needs_heal(dns_down_state(T0), failing, T0 + timedelta(minutes=10))
    state = dns_down_state(T0) | {"healed_at": (T0 + timedelta(minutes=10)).isoformat()}
    assert not watchdog.needs_heal(state, failing, T0 + timedelta(minutes=40))
    assert watchdog.needs_heal(state, failing, T0 + timedelta(minutes=75))
    assert not watchdog.needs_heal(
        dns_down_state(T0), [Check("dns", OK, "x")], T0 + timedelta(hours=1)
    )


def test_heal_only_touches_the_resolver() -> None:
    ran: list[str] = []

    def run(cmd: Sequence[str]) -> tuple[int, str]:
        ran.append(" ".join(cmd))
        return 0, ""

    watchdog.heal(Probes(run=run))
    assert ran == ["resolvectl flush-caches", "systemctl restart systemd-resolved"]


def test_run_once_heals_rechecks_and_saves(tmp_path: Path) -> None:
    ran: list[str] = []
    answers = iter([False, False])

    def run(cmd: Sequence[str]) -> tuple[int, str]:
        ran.append(" ".join(cmd))
        return (0, "active")

    ctx = make_ctx(tmp_path, resolve=lambda _host: next(answers, True), run=run)
    state_path = tmp_path / "watchdog.json"
    state_path.write_text(json.dumps(dns_down_state(T0 - timedelta(minutes=30))))

    checks, events = watchdog.run_once(ctx, state_path)

    assert "systemctl restart systemd-resolved" in ran
    assert next(check for check in checks if check.name == "dns").level == OK
    saved = json.loads(state_path.read_text())
    assert saved["healed_at"] == T0.isoformat(timespec="seconds")
    assert state_path.stat().st_mode & 0o777 == 0o644
    assert isinstance(events, list)


def test_run_once_does_not_heal_when_disabled(tmp_path: Path) -> None:
    ran: list[str] = []

    def run(cmd: Sequence[str]) -> tuple[int, str]:
        ran.append(" ".join(cmd))
        return 0, "active"

    ctx = make_ctx(tmp_path, resolve=lambda _host: False, run=run)
    state_path = tmp_path / "watchdog.json"
    state_path.write_text(json.dumps(dns_down_state(T0 - timedelta(hours=1))))
    watchdog.run_once(ctx, state_path, self_heal=False)
    assert "systemctl restart systemd-resolved" not in ran


# -- Real probes and the command ---------------------------------------------------


def test_dns_query_packet_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[bytes] = []

    class FakeSocket:
        def __enter__(self) -> FakeSocket:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def settimeout(self, _seconds: float) -> None:
            return None

        def sendto(self, data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)

        def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
            query = sent[0]
            # Same id, response flags with rcode 0, one answer.
            return query[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00", ("10.2.0.1", 53)

    monkeypatch.setattr(watchdog.socket, "socket", lambda *_a: FakeSocket())
    assert watchdog.dns_query("10.2.0.1", "www.bbc.co.uk")
    assert b"\x03www\x03bbc\x02co\x02uk\x00" in sent[0]


def test_watchdog_command_prints_checks_and_saves_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        'settings:\n  user_agent: "t/0.1 (+contact: t@example.org)"\n'
        "sources:\n  - {name: A, url: 'https://a.example/rss', type: rss}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(watchdog, "tcp_connect", lambda _h, _p: True)
    monkeypatch.setattr(watchdog, "run_command", lambda _cmd: (0, "active"))
    monkeypatch.setattr(watchdog, "http_healthy", lambda _u: True)
    state = tmp_path / "state.json"
    args = [
        "watchdog",
        "--sources", str(sources),
        "--db", str(tmp_path / "headlines.db"),
        "--state", str(state),
        "--no-heal",
        "--quiet",
    ]  # fmt: skip
    assert main(args) == EXIT_OK
    out = capsys.readouterr().out
    assert "dns" in out and "internet" in out and "viewer" in out
    assert json.loads(state.read_text())["checks"]["dns"]["level"] == OK
    assert main([*args, "--exit-status"]) in {EXIT_OK, 1, EXIT_FATAL}


def test_the_state_the_drills_seed_produces_an_alert() -> None:
    """deploy/drill.sh seeds 'failing for 20 minutes' state in exactly this shape."""
    since = (T0 - timedelta(minutes=20)).isoformat()
    seeded = {
        "checks": {
            name: {"level": "fail", "detail": "drill", "since": since, "notified_level": "ok"}
            for name in ("dns", "internet")
        },
        "pending": [],
    }
    state, events = watchdog.update_state(
        seeded, [Check("dns", FAIL, "x"), Check("internet", FAIL, "y")], T0
    )
    assert [(e.kind, e.check) for e in events] == [("alert", "dns"), ("alert", "internet")]
    assert [e["kind"] for e in state["pending"]] == ["alert", "alert"]
