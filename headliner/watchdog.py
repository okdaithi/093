"""Health checks for the machine headliner runs on, and the state that decides when to speak up.

`headliner watchdog` runs from a root systemd timer every 10 minutes. It probes
DNS (system resolver and the VPN's own), raw internet reachability, the VPN
tunnel, the last fetch run, systemd units, the web viewer, backups, disk and the
database; compares with the previous run in a small JSON state file; and turns
lasting changes into events (alert, reminder, recovery) for the notifier.

Every probe is injected through `Probes`, so tests never touch the network, and
the state machine (`update_state`) is a pure function of its inputs.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import urllib.request
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from headliner import network, notify
from headliner.backup import default_dir, list_backups
from headliner.models import utcnow
from headliner.store import connect_readonly, recent_runs

logger = logging.getLogger(__name__)

OK: Final = "ok"
WARN: Final = "warn"
FAIL: Final = "fail"
_RANK: Final = {OK: 0, WARN: 1, FAIL: 2}

# How long a problem must last before anyone is told, and how often to repeat.
GRACE: Final = {FAIL: timedelta(minutes=9), WARN: timedelta(minutes=55)}
REMIND_EVERY: Final = timedelta(hours=6)
# Self-repair: flush DNS caches and restart the resolver once a lookup outage
# has lasted this long, then leave it alone for the cooldown.
HEAL_AFTER: Final = timedelta(minutes=9)
HEAL_COOLDOWN: Final = timedelta(hours=1)

RUN_LATE_AFTER: Final = timedelta(hours=7)
RUN_FAILED_SHARE: Final = 0.5
BACKUP_LATE_AFTER: Final = timedelta(hours=36)
DISK_WARN_PERCENT: Final = 90
HANDSHAKE_STALE: Final = timedelta(minutes=3)
SOURCE_STREAK: Final = 3
VPN_DNS_SERVER: Final = "10.2.0.1"
VPN_INTERFACE: Final = "proton0"
# Addresses reached without DNS, to tell "no routing" from "no name resolution".
RAW_ENDPOINTS: Final = (("1.1.1.1", 443), ("9.9.9.9", 443))
FALLBACK_HOSTS: Final = ("github.com", "www.bbc.co.uk")
UNITS: Final = ("headliner.timer", "headliner-web.service", "headliner-backup.timer")
PENDING_LIMIT: Final = 50


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    level: str
    detail: str


@dataclass(slots=True)
class Probes:
    """Everything the checks touch outside this process. Tests replace these."""

    now: Callable[[], datetime] = utcnow
    resolve: Callable[[str], bool] = network.resolves
    resolve_via: Callable[[str, str], bool] = lambda server, host: dns_query(server, host)
    tcp: Callable[[str, int], bool] = lambda host, port: tcp_connect(host, port)
    run: Callable[[Sequence[str]], tuple[int, str]] = lambda cmd: run_command(cmd)
    http_ok: Callable[[str], bool] = lambda url: http_healthy(url)
    disk_percent: Callable[[Path], float] = lambda path: disk_used_percent(path)


@dataclass(slots=True)
class Context:
    db_path: Path
    hosts: list[str]
    probes: Probes = field(default_factory=Probes)
    viewer_url: str = "http://127.0.0.1:8090/healthz"
    vpn_interface: str = VPN_INTERFACE
    vpn_dns: str = VPN_DNS_SERVER
    data_dir: Path | None = None


# -- Real probes ---------------------------------------------------------------


def dns_query(server: str, host: str, timeout: float = 3.0) -> bool:
    """Whether `server` answers an A query for `host` (asked directly, over UDP)."""
    query_id = os.urandom(2)
    header = query_id + struct.pack(">HHHHH", 0x0100, 1, 0, 0, 0)
    name = b"".join(bytes([len(part)]) + part.encode("ascii") for part in host.split("."))
    packet = header + name + b"\x00" + struct.pack(">HH", 1, 1)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(packet, (server, 53))
            reply, _ = sock.recvfrom(1500)
    except (OSError, UnicodeError):
        return False
    return len(reply) >= 12 and reply[:2] == query_id and reply[3] & 0x0F == 0 and reply[7] > 0


def tcp_connect(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run_command(cmd: Sequence[str], timeout: float = 20.0) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return done.returncode, (done.stdout or done.stderr).strip()


def http_healthy(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return bool(json.load(response).get("status") == "ok")
    except (OSError, ValueError):
        return False


def disk_used_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return 100 * usage.used / usage.total


# -- Checks --------------------------------------------------------------------


def check_dns(ctx: Context) -> Check:
    """System resolver, and (to say where it broke) the VPN's resolver on its own."""
    system = [host for host in ctx.hosts if ctx.probes.resolve(host)]
    if system:
        return Check("dns", OK, f"{len(system)}/{len(ctx.hosts)} test hosts resolve")
    via_vpn = ctx.probes.resolve_via(ctx.vpn_dns, ctx.hosts[0])
    where = (
        f"the VPN resolver {ctx.vpn_dns} answers, so the break is between the system and it"
        if via_vpn
        else f"the VPN resolver {ctx.vpn_dns} does not answer either"
    )
    return Check("dns", FAIL, f"no test host resolves ({', '.join(ctx.hosts)}); {where}")


def check_internet(ctx: Context) -> Check:
    """TCP to fixed addresses: separates 'no routing' from 'no name resolution'."""
    reached = [f"{host}" for host, port in RAW_ENDPOINTS if ctx.probes.tcp(host, port)]
    if reached:
        return Check("internet", OK, f"reached {', '.join(reached)} without DNS")
    return Check("internet", FAIL, "cannot connect to 1.1.1.1 or 9.9.9.9 on 443: no routing")


def check_vpn(ctx: Context) -> Check:
    """The WireGuard tunnel's last handshake (needs root; skipped where there is no tunnel)."""
    code, out = ctx.probes.run(["wg", "show", ctx.vpn_interface, "latest-handshakes"])
    if code != 0:
        if "permission" in out.lower() or "not permitted" in out.lower():
            return Check("vpn", WARN, "cannot read WireGuard state (the watchdog needs root)")
        return Check("vpn", OK, f"no WireGuard interface {ctx.vpn_interface} to check")
    stamps = [int(part) for line in out.splitlines() if (part := line.split()[-1]).isdigit()]
    latest = max(stamps, default=0)
    if latest == 0:
        return Check("vpn", FAIL, f"{ctx.vpn_interface} has never completed a handshake")
    age = ctx.probes.now().timestamp() - latest
    if age > HANDSHAKE_STALE.total_seconds():
        return Check(
            "vpn",
            FAIL,
            f"{ctx.vpn_interface} last handshake {int(age // 60)} min ago (tunnel stalled?)",
        )
    return Check("vpn", OK, f"{ctx.vpn_interface} handshake {int(age)} s ago")


def check_fetch(ctx: Context) -> Check:
    if not ctx.db_path.exists():
        return Check("fetch", WARN, f"no database at {ctx.db_path} yet")
    try:
        with closing(connect_readonly(ctx.db_path)) as conn:
            runs = recent_runs(conn, limit=1)
    except sqlite3.Error as exc:
        return Check("fetch", FAIL, f"cannot read the database: {exc}")
    if not runs:
        return Check("fetch", WARN, "no fetch runs logged yet")
    run = runs[0]
    age = ctx.probes.now() - run.finished_at
    if age > RUN_LATE_AFTER:
        return Check("fetch", FAIL, f"last run finished {int(age.total_seconds() // 3600)} h ago")
    total = run.ok + run.skipped + run.failed
    if total and run.failed / total >= RUN_FAILED_SHARE:
        return Check("fetch", FAIL, f"last run failed {run.failed} of {total} sources")
    return Check("fetch", OK, f"last run {run.ok} ok, {run.failed} failed")


def check_sources(ctx: Context) -> Check:
    """Sources whose last few runs all failed (a dead or changed feed)."""
    if not ctx.db_path.exists():
        return Check("sources", OK, "no database yet")
    try:
        with closing(connect_readonly(ctx.db_path)) as conn:
            rows = conn.execute(
                "SELECT source, status FROM fetch_log ORDER BY started_at DESC, id DESC LIMIT 4000"
            ).fetchall()
    except sqlite3.Error as exc:
        return Check("sources", WARN, f"cannot read the database: {exc}")
    recent: dict[str, list[str]] = {}
    for source, status in rows:
        statuses = recent.setdefault(source, [])
        if len(statuses) < SOURCE_STREAK:
            statuses.append(status)
    broken = sorted(
        source
        for source, statuses in recent.items()
        if len(statuses) == SOURCE_STREAK and all(status == "error" for status in statuses)
    )
    # A whole-network outage fails everything at once; fetch/dns report that.
    if broken and len(broken) < max(5, len(recent) * 0.5):
        return Check(
            "sources",
            WARN,
            f"{len(broken)} failing {SOURCE_STREAK} runs running: {', '.join(broken)}",
        )
    return Check("sources", OK, "no source is failing run after run")


def check_units(ctx: Context) -> Check:
    down = [
        unit for unit in UNITS if ctx.probes.run(["systemctl", "is-active", unit])[1] != "active"
    ]
    _, failed = ctx.probes.run(["systemctl", "--failed", "--plain", "--no-legend"])
    failed_units = [line.split()[0] for line in failed.splitlines() if line.startswith("headliner")]
    if down or failed_units:
        return Check(
            "units", FAIL, "not active/failed: " + ", ".join(sorted({*down, *failed_units}))
        )
    return Check("units", OK, "headliner timers and services are active")


def check_viewer(ctx: Context) -> Check:
    if ctx.probes.http_ok(ctx.viewer_url):
        return Check("viewer", OK, "healthz ok")
    return Check("viewer", FAIL, f"{ctx.viewer_url} did not answer ok")


def check_backups(ctx: Context) -> Check:
    backups = list_backups(default_dir(ctx.db_path), ctx.db_path.stem)
    if not backups:
        return Check("backups", WARN, "no backups")
    age = ctx.probes.now() - backups[0].taken_at
    if age > BACKUP_LATE_AFTER:
        return Check("backups", WARN, f"latest backup is {int(age.total_seconds() // 3600)} h old")
    return Check("backups", OK, f"latest backup {int(age.total_seconds() // 3600)} h old")


def check_disk(ctx: Context) -> Check:
    path = ctx.data_dir or ctx.db_path.parent
    percent = ctx.probes.disk_percent(path)
    if percent >= DISK_WARN_PERCENT:
        return Check("disk", WARN, f"{path} is {percent:.0f}% full")
    return Check("disk", OK, f"{percent:.0f}% used")


CHECKS: Final = (
    check_dns,
    check_internet,
    check_vpn,
    check_fetch,
    check_sources,
    check_units,
    check_viewer,
    check_backups,
    check_disk,
)


def run_checks(ctx: Context) -> list[Check]:
    results = []
    for check in CHECKS:
        try:
            results.append(check(ctx))
        except Exception as exc:  # noqa: BLE001 - one broken probe must not hide the rest
            results.append(
                Check(check.__name__.removeprefix("check_"), WARN, f"check crashed: {exc}")
            )
    return results


# -- State and events ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    kind: str  # "alert", "remind" or "recover"
    check: str
    level: str
    detail: str
    since: str
    at: str

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "check": self.check,
            "level": self.level,
            "detail": self.detail,
            "since": self.since,
            "at": self.at,
        }


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def update_state(
    state: dict[str, Any], checks: Sequence[Check], now: datetime
) -> tuple[dict[str, Any], list[Event]]:
    """Fold a run's `checks` into `state`; return the new state and events to tell someone.

    A problem is announced once it has lasted GRACE for its level, repeated every
    REMIND_EVERY while it lasts, and a recovery is announced after any alert.
    """
    previous: dict[str, Any] = state.get("checks", {})
    current: dict[str, Any] = {}
    events: list[Event] = []
    for check in checks:
        old = previous.get(check.name, {})
        old_level = old.get("level", OK)
        since = _parse(old.get("since"))
        notified_level = old.get("notified_level", OK)
        notified_at = _parse(old.get("notified_at"))
        if check.level == OK:
            if notified_level != OK:
                events.append(
                    Event("recover", check.name, OK, check.detail, old.get("since", ""), _iso(now))
                )
            current[check.name] = {"level": OK, "detail": check.detail, "since": _iso(now)}
            continue
        began = since if old_level == check.level and since is not None else now
        entry = {
            "level": check.level,
            "detail": check.detail,
            "since": _iso(began),
            "notified_level": notified_level,
        }
        if notified_at is not None:
            entry["notified_at"] = _iso(notified_at)
        lasted = now - began
        worse = _RANK[check.level] > _RANK[notified_level]
        if lasted >= GRACE[check.level] and worse:
            kind = "alert"
        elif notified_level != OK and notified_at is not None and now - notified_at >= REMIND_EVERY:
            kind = "remind"
        else:
            kind = ""
        if kind:
            events.append(
                Event(kind, check.name, check.level, check.detail, _iso(began), _iso(now))
            )
            entry["notified_level"] = check.level
            entry["notified_at"] = _iso(now)
        current[check.name] = entry
    pending = [*state.get("pending", []), *(event.as_dict() for event in events)]
    new_state = {
        "updated_at": _iso(now),
        "checks": current,
        "pending": pending[-PENDING_LIMIT:],
        "healed_at": state.get("healed_at"),
    }
    return new_state, events


def needs_heal(state: dict[str, Any], checks: Sequence[Check], now: datetime) -> bool:
    """Whether a lookup outage has lasted long enough, and not been 'healed' recently."""
    dns = next((check for check in checks if check.name == "dns"), None)
    if dns is None or dns.level != FAIL:
        return False
    since = _parse(state.get("checks", {}).get("dns", {}).get("since"))
    if state.get("checks", {}).get("dns", {}).get("level") != FAIL or since is None:
        return False
    healed = _parse(state.get("healed_at"))
    return now - since >= HEAL_AFTER and (healed is None or now - healed >= HEAL_COOLDOWN)


def heal(probes: Probes) -> str:
    """Flush DNS caches and restart the resolver. Never touches the VPN."""
    steps = []
    for cmd in (["resolvectl", "flush-caches"], ["systemctl", "restart", "systemd-resolved"]):
        code, out = probes.run(cmd)
        steps.append(f"{' '.join(cmd)} -> {code}" + (f" ({out})" if code and out else ""))
    return "; ".join(steps)


# -- State file ----------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Write atomically and world-readable, so the viewer can show it."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o644)
    temporary.replace(path)


def choose_hosts(sources_urls: Sequence[str]) -> list[str]:
    hosts = network.probe_hosts(sources_urls)
    return [*hosts, *(host for host in FALLBACK_HOSTS if host not in hosts)][:4]


def send_notifications(
    state: dict[str, Any], notifier: notify.Notifier, checks: Sequence[Check], now: datetime
) -> None:
    """Deliver pending events, then record the heartbeat. Failures leave the events pending."""
    try:
        sent = notify.deliver(state, notifier.client)
        if sent:
            logger.info("delivered %d watchdog event(s) to GitHub", sent)
        failing = [check.name for check in checks if check.level != OK]
        body = notify.heartbeat_body(now, notifier.host, notifier.build, failing)
        notify.beat(state, notifier.client, body)
    except notify.NotifyError as exc:
        logger.error(
            "cannot notify via GitHub: %s (%d event(s) pending)",
            exc,
            len(state.get("pending", [])),
        )


def run_once(
    ctx: Context,
    state_path: Path,
    *,
    self_heal: bool = True,
    notifier: notify.Notifier | None = None,
) -> tuple[list[Check], list[Event]]:
    """One watchdog tick: check, maybe repair the resolver, update and save the state."""
    now = ctx.probes.now()
    state = load_state(state_path)
    checks = run_checks(ctx)
    if self_heal and needs_heal(state, checks, now):
        logger.warning(
            "DNS has been down; flushing caches and restarting the resolver: %s", heal(ctx.probes)
        )
        state["healed_at"] = _iso(now)
        # Give the resolver a moment, then look again before judging.
        checks = [
            check_dns(ctx)
            if check.name == "dns"
            else check_internet(ctx)
            if check.name == "internet"
            else check
            for check in checks
        ]
    new_state, events = update_state(state, checks, now)
    # Issues opened so far and the heartbeat issue live in the same file.
    for key in ("issues", "heartbeat_issue"):
        if key in state:
            new_state[key] = state[key]
    save_state(state_path, new_state)
    if notifier is not None:
        send_notifications(new_state, notifier, checks, now)
        save_state(state_path, new_state)
    for event in events:
        log = logger.error if event.kind != "recover" else logger.info
        log("watchdog %s: %s (%s) since %s", event.kind, event.check, event.detail, event.since)
    return checks, events
