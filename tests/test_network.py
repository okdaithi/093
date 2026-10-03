"""Network preflight: telling a dead DNS/VPN path from broken feeds."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from headliner import network
from headliner.cli import EXIT_NETWORK, EXIT_PARTIAL_FAILURE, main
from headliner.store import connect
from headliner.web import WebApp
from tests.test_cli import CONFIG, FEED_URL


@pytest.mark.parametrize(
    "message",
    [
        "https://x.example/rss: giving up after 3 attempts (ConnectError: "
        "[Errno -2] Name or service not known)",
        "ConnectError: [Errno -3] Temporary failure in name resolution",
        "OSError: [Errno 101] Network is unreachable",
        network.NETWORK_ERROR,
    ],
)
def test_network_errors_are_recognised(message: str) -> None:
    assert network.is_network_error(message)


@pytest.mark.parametrize(
    "message",
    [None, "", "HTTP 503", "ParseError: not well-formed", "ReadTimeout: timed out"],
)
def test_feed_errors_are_not_network_errors(message: str | None) -> None:
    assert not network.is_network_error(message)


def test_probe_hosts_are_distinct_and_spread() -> None:
    urls = [f"https://h{i}.example/{n}" for i in range(10) for n in range(2)]
    assert network.probe_hosts(urls) == ["h0.example", "h4.example", "h9.example"]
    assert network.probe_hosts(["https://a.example/x", "https://a.example/y"]) == ["a.example"]
    assert network.probe_hosts([]) == []


def test_up_when_any_host_resolves_and_down_when_none_do() -> None:
    assert network.is_up(["a", "b"], resolver=lambda host: host == "b")
    assert not network.is_up(["a", "b"], resolver=lambda _host: False)
    assert network.is_up([], resolver=lambda _host: False)


def test_a_crashing_lookup_counts_as_down() -> None:
    def boom(host: str) -> bool:
        raise RuntimeError(host)

    assert not network.is_up(["a"], resolver=boom)


def test_waiting_stops_when_the_network_returns() -> None:
    answers = iter([False, False, True])
    pauses: list[float] = []
    up = network.wait_until_up(
        ["a"],
        patience=900,
        resolver=lambda _host: next(answers),
        sleep=pauses.append,
    )
    assert up
    assert pauses == [30, 60]


def test_waiting_gives_up_after_the_patience() -> None:
    pauses: list[float] = []
    up = network.wait_until_up(
        ["a"], patience=900, resolver=lambda _host: False, sleep=pauses.append
    )
    assert not up
    assert pauses == [30, 60, 120, 240, 240]


def test_no_patience_means_one_check() -> None:
    pauses: list[float] = []
    assert not network.wait_until_up(
        ["a"], patience=0, resolver=lambda _host: False, sleep=pauses.append
    )
    assert pauses == []


def run_fetch(config_path: Path, db_path: Path, *extra: str) -> int:
    return main(["fetch", "--sources", str(config_path), "--db", str(db_path), "--quiet", *extra])


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@respx.mock
def test_fetch_exits_3_without_fetching_when_dns_is_down(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("headliner.network.resolves", lambda _host: False)
    feed = respx.get(FEED_URL)
    db_path = tmp_path / "headlines.db"

    assert run_fetch(config_path, db_path) == EXIT_NETWORK

    assert not feed.called
    conn = connect(db_path)
    rows = conn.execute("SELECT source, status, error FROM fetch_log").fetchall()
    conn.close()
    assert [tuple(row) for row in rows] == [("Example Wire", "error", network.NETWORK_ERROR)]


@respx.mock
def test_fetch_waits_for_the_network_when_asked(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feed_bytes: bytes
) -> None:
    import httpx

    answers = iter([False, True])
    monkeypatch.setattr("headliner.network.resolves", lambda _host: next(answers))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    respx.get("https://feed.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(200, content=feed_bytes))

    assert run_fetch(config_path, tmp_path / "headlines.db", "--wait-network", "5") == 0


@respx.mock
def test_all_sources_failing_with_dns_errors_exits_3(config_path: Path, tmp_path: Path) -> None:
    import httpx

    respx.get("https://feed.example.org/robots.txt").mock(
        side_effect=httpx.ConnectError("[Errno -2] Name or service not known")
    )
    respx.get(FEED_URL).mock(side_effect=httpx.ConnectError("[Errno -2] Name or service not known"))
    assert run_fetch(config_path, tmp_path / "headlines.db") == EXIT_NETWORK


@respx.mock
def test_other_failures_stay_exit_1(config_path: Path, tmp_path: Path) -> None:
    import httpx

    respx.get("https://feed.example.org/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /\n")
    )
    respx.get(FEED_URL).mock(return_value=httpx.Response(500))
    assert run_fetch(config_path, tmp_path / "headlines.db") == EXIT_PARTIAL_FAILURE


def test_status_names_a_network_outage_instead_of_many_broken_feeds(
    tmp_path: Path,
) -> None:
    from datetime import timedelta

    from headliner.models import utcnow
    from headliner.store import record_fetch

    names = [f"Feed {n}" for n in range(5)]
    config = tmp_path / "sources.yaml"
    config.write_text(
        'settings:\n  user_agent: "t/0.1 (+contact: t@example.org)"\nsources:\n'
        + "".join(
            f'  - {{name: "{n}", url: "https://{i}.example/rss", type: rss}}\n'
            for i, n in enumerate(names)
        ),
        encoding="utf-8",
    )
    db = tmp_path / "headlines.db"
    started = utcnow() - timedelta(minutes=5)
    with connect(db) as conn:
        for name in names:
            record_fetch(
                conn,
                source=name,
                started_at=started,
                finished_at=started + timedelta(seconds=3),
                status="error",
                items_found=0,
                items_new=0,
                items_changed=0,
                error="x: giving up after 3 attempts (ConnectError: [Errno -2] "
                "Name or service not known)",
            )
        conn.commit()
    report = WebApp(db, config).status_report(connect(db))
    assert report["network_down"] is True
    assert any(check.startswith("network down") for check in report["checks"])
    assert not any("need attention" in check for check in report["checks"])
    json.dumps(report)
