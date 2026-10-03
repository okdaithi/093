"""Is the network (DNS, mostly) working? Used before a fetch run and when reporting one.

When the machine's DNS or VPN path is down, every feed fails the same way. That
is one problem, not 66 broken feeds, and fetching through it only wastes a run.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Final
from urllib.parse import urlsplit

PROBE_HOSTS: Final = 3
PROBE_TIMEOUT: Final = 8.0
# Pauses between probes while the network is down: 30 s doubling to 4 min, then 4 min each.
WAIT_DELAYS: Final = (30, 60, 120, 240)
NETWORK_ERROR: Final = "network down: no host name could be resolved"

# What an httpx/anyio connection failure says when DNS or routing is broken.
_NETWORK_ERROR_TEXT: Final = re.compile(
    r"name or service not known|temporary failure in name resolution|"
    r"nodename nor servname|errno -[235]\b|network is unreachable|no route to host|"
    r"network down:",
    re.I,
)

Resolver = Callable[[str], bool]


def is_network_error(message: str | None) -> bool:
    """Whether a stored fetch error is a DNS or routing failure rather than a feed's fault."""
    return bool(message and _NETWORK_ERROR_TEXT.search(message))


def probe_hosts(urls: Iterable[str], count: int = PROBE_HOSTS) -> list[str]:
    """Up to `count` distinct host names from `urls`, spread across the list."""
    hosts = list(dict.fromkeys(host for url in urls if (host := urlsplit(url).hostname)))
    if len(hosts) <= count:
        return hosts
    step = (len(hosts) - 1) / (count - 1) if count > 1 else 0
    return [hosts[round(index * step)] for index in range(count)]


def resolves(host: str) -> bool:
    """Whether the system resolver returns an IPv4 address for `host`."""
    try:
        return bool(socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM))
    except OSError:
        return False


def is_up(hosts: Sequence[str], resolver: Resolver | None = None) -> bool:
    """True when at least one of `hosts` resolves (an empty list cannot be judged: up)."""
    if not hosts:
        return True
    resolver = resolver or resolves
    # getaddrinfo can block for a long time, so each lookup runs in its own thread
    # and is abandoned (not cancelled) after PROBE_TIMEOUT.
    pool = ThreadPoolExecutor(max_workers=len(hosts))
    try:
        futures = [pool.submit(resolver, host) for host in hosts]
        deadline = time.monotonic() + PROBE_TIMEOUT
        for future in futures:
            try:
                if future.result(timeout=max(0.0, deadline - time.monotonic())):
                    return True
            except Exception:  # noqa: BLE001 - a timeout or crashed lookup is "down"
                continue
        return False
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def wait_until_up(
    hosts: Sequence[str],
    *,
    patience: float,
    resolver: Resolver | None = None,
    sleep: Callable[[float], None] = time.sleep,
    delays: Sequence[float] = WAIT_DELAYS,
) -> bool:
    """Probe now, then keep probing with growing pauses for up to `patience` seconds."""
    if is_up(hosts, resolver):
        return True
    waited = 0.0
    attempt = 0
    while delays:
        # The last pause repeats until patience runs out.
        delay = delays[min(attempt, len(delays) - 1)]
        if waited + delay > patience:
            return False
        sleep(delay)
        waited += delay
        attempt += 1
        if is_up(hosts, resolver):
            return True
    return False
