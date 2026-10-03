"""Tell the owner about watchdog events through GitHub issues (GitHub and the NUC only).

Two jobs, both through the GitHub REST API with a token that can only touch
issues on one repository (a fine-grained token, kept in a root-only file):

- Deliver the watchdog's pending events: one issue per problem, opened when it
  is announced, commented on for reminders, commented on and closed on
  recovery. GitHub emails the owner. While the network is down nothing can be
  sent, so events stay pending and arrive, in order, once it is back.
- Keep a heartbeat: one machine-written issue whose body records when the
  watchdog last ran. A scheduled cloud routine reads it and raises the alarm
  if it goes stale, which covers the case where this machine cannot speak.

The token is never logged or put in an error message.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Final

from headliner import local_timezone, tz_abbrev

logger = logging.getLogger(__name__)

API: Final = "https://api.github.com"
DEFAULT_REPO: Final = "okdaithi/093"
DEFAULT_TOKEN_FILE: Final = Path("/etc/headliner/github-token")
HEARTBEAT_TITLE: Final = "headliner heartbeat (machine-written, do not close)"
TITLE_PREFIX: Final = "headliner health: "
TIMEOUT: Final = 15.0

LABELS: Final = {
    "dns": "DNS not resolving",
    "internet": "no internet routing",
    "vpn": "VPN tunnel stalled",
    "fetch": "fetch runs failing or late",
    "sources": "feeds failing run after run",
    "units": "services not running",
    "viewer": "web viewer not answering",
    "backups": "backups stale",
    "disk": "disk nearly full",
    "drill": "DRILL: test alert (ignore)",
}
HINTS: Final = {
    "dns": "Check `resolvectl status` and ProtonVPN. Before reconnecting the VPN, run "
    "`sudo wg show proton0 latest-handshakes` to see whether the tunnel stalled.",
    "internet": "No route to 1.1.1.1/9.9.9.9. Check the ProtonVPN connection and `ip route`.",
    "vpn": "`sudo wg show proton0` shows the last handshake. Reconnect with `nuc-protonvpn` "
    "if it is stale.",
    "fetch": "`journalctl -u headliner.service -n 50`, then `/sources` in the viewer.",
    "sources": "See the Sources page; the feed may have moved or be blocking us.",
    "units": "`systemctl status headliner.timer headliner-web.service headliner-backup.timer`",
    "viewer": "`journalctl -u headliner-web.service -n 50`",
    "backups": "`systemctl status headliner-backup.service`",
    "disk": "`df -h /var/lib/headliner`",
    "drill": "This is a test from `deploy/drill.sh notify`; it should close itself within seconds.",
}


class NotifyError(RuntimeError):
    """A GitHub call failed. The message never contains the token."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


Transport = Callable[[str, str, str, dict[str, Any] | None], tuple[int, Any]]


def urllib_transport(
    method: str, url: str, token: str, payload: dict[str, Any] | None
) -> tuple[int, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "headliner-watchdog",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
            return response.status, json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except (OSError, ValueError) as exc:
        raise NotifyError(f"cannot reach GitHub ({type(exc).__name__})") from exc


class GitHub:
    """The few issue calls the watchdog needs."""

    def __init__(self, token: str, repo: str, transport: Transport = urllib_transport) -> None:
        self._token = token
        self.repo = repo
        self._transport = transport

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        status, body = self._transport(
            method, f"{API}/repos/{self.repo}{path}", self._token, payload
        )
        if status >= 300:
            hint = (
                " (the token needs Issues read/write on this repository)"
                if status in {401, 403, 404}
                else ""
            )
            raise NotifyError(
                f"GitHub answered HTTP {status} for {method} {path}{hint}", status=status
            )
        return body

    def create_issue(self, title: str, body: str) -> int:
        return int(self._call("POST", "/issues", {"title": title, "body": body})["number"])

    def comment(self, number: int, body: str) -> None:
        self._call("POST", f"/issues/{number}/comments", {"body": body})

    def close(self, number: int) -> None:
        self._call("PATCH", f"/issues/{number}", {"state": "closed", "state_reason": "completed"})

    def set_body(self, number: int, body: str) -> None:
        self._call("PATCH", f"/issues/{number}", {"body": body})

    def find_open(self, title: str) -> int | None:
        """The number of the open issue (not pull request) titled `title`, if any."""
        for item in self._call("GET", "/issues?state=open&per_page=100") or []:
            if item.get("title") == title and "pull_request" not in item:
                return int(item["number"])
        return None


def load_token(path: Path) -> str | None:
    """The token in `path`, or None when there is no usable file (notifications stay off)."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


# -- Text ------------------------------------------------------------------------


def _when(value: str, zone: tzinfo) -> str:
    try:
        moment = datetime.fromisoformat(value).astimezone(zone)
    except ValueError:
        return value or "unknown"
    return f"{moment:%a %-d %b %H:%M} {tz_abbrev(moment)}"


def _title(check: str) -> str:
    return f"{TITLE_PREFIX}{LABELS.get(check, check)}"


def _duration(start: str, end: str) -> str:
    try:
        seconds = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return "an unknown time"
    if seconds < 90 * 60:
        return f"{max(1, seconds // 60)} min"
    return f"{seconds / 3600:.1f} h"


def issue_body(event: dict[str, str], zone: tzinfo) -> str:
    check = event["check"]
    since = _when(event["since"], zone)
    return (
        f"**{LABELS.get(check, check)}** ({event['level']}) since {since}.\n\n"
        f"> {event['detail']}\n\n"
        f"{HINTS.get(check, '')}\n\n"
        "_Opened by headliner-watchdog on the NUC. It comments here if the problem lasts "
        "and closes this issue when the check passes again._"
    )


def comment_body(event: dict[str, str], zone: tzinfo) -> str:
    when = _when(event["at"], zone)
    if event["kind"] == "recover":
        return (
            f"Recovered at {when} after {_duration(event['since'], event['at'])} "
            f"(since {_when(event['since'], zone)})."
        )
    if event["kind"] == "remind":
        return f"Still {event['level']} at {when}: {event['detail']}"
    return f"{event['level'].upper()} at {when}: {event['detail']}"


# -- Delivery --------------------------------------------------------------------


def deliver(state: dict[str, Any], client: GitHub, zone: tzinfo | None = None) -> int:
    """Send `state["pending"]` in order; keep what could not be sent. Returns how many went.

    `state["issues"]` maps each check to its open issue number, so reminders and
    the recovery land on the issue the alert opened.
    """
    zone = zone or local_timezone()
    issues: dict[str, int] = state.setdefault("issues", {})
    pending: list[dict[str, str]] = list(state.get("pending", []))
    sent = 0
    while pending:
        event = pending[0]
        check = event["check"]
        number = issues.get(check)
        if event["kind"] == "recover":
            if number is None:
                # The alert never got out (or its issue is gone): record the whole story.
                number = client.create_issue(_title(check), issue_body(event, zone))
                issues[check] = number
            client.comment(number, comment_body(event, zone))
            client.close(number)
            issues.pop(check, None)
        elif number is None:
            number = client.create_issue(_title(check), issue_body(event, zone))
            issues[check] = number
        else:
            client.comment(number, comment_body(event, zone))
        pending.pop(0)
        state["pending"] = pending
        sent += 1
    return sent


def heartbeat_body(now: datetime, host: str, build: str, failing: Sequence[str]) -> str:
    """The heartbeat issue text. `last_ok:` is what the cloud routine reads."""
    return (
        "Written by the NUC's health watchdog every 10 minutes (edits send no email). "
        "If `last_ok` is more than 45 minutes old, the machine, its network or the "
        "watchdog is down.\n\n"
        "```\n"
        f"last_ok: {now.isoformat(timespec='seconds')}\n"
        f"host: {host}\n"
        f"build: {build}\n"
        f"failing: {', '.join(failing) if failing else 'none'}\n"
        "```\n"
    )


def beat(state: dict[str, Any], client: GitHub, body: str) -> None:
    """Update (creating if needed) the heartbeat issue."""
    number = state.get("heartbeat_issue")
    if number is not None:
        try:
            client.set_body(int(number), body)
            return
        except NotifyError as exc:
            if exc.status != 404:  # the issue was deleted: make a new one
                raise
    found = client.find_open(HEARTBEAT_TITLE)
    if found is None:
        state["heartbeat_issue"] = client.create_issue(HEARTBEAT_TITLE, body)
        return
    client.set_body(found, body)
    state["heartbeat_issue"] = found


@dataclass(frozen=True, slots=True)
class Notifier:
    client: GitHub
    host: str
    build: str
