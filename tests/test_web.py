"""The read-only web viewer: pages, filters, escaping, and never writing."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from wsgiref.util import setup_testing_defaults

import pytest
from headliner.cli import build_parser
from headliner.models import Headline, utcnow
from headliner.store import connect, record_fetch, store_headlines
from headliner.web import WebApp, highlight, word_diff

CONFIG = """
settings:
  user_agent: "headliner-tests/0.1 (+contact: tests@example.org)"
sources:
  - {name: Example Wire, url: "https://example.org/feed.xml", type: rss, tags: [AU]}
  - {name: Other Daily, url: "https://other.example/rss", type: rss, tags: [IE, business]}
  - {name: Quiet Times, url: "https://quiet.example/rss", type: rss, tags: [IE]}
"""


def headline(
    title: str,
    url: str,
    *,
    source: str = "Example Wire",
    fetched_at: datetime | None = None,
    summary: str | None = None,
) -> Headline:
    when = fetched_at or utcnow() - timedelta(hours=1)
    return Headline.create(
        source=source,
        title=title,
        url=url,
        published_at=when,
        fetched_at=when,
        summary=summary,
    )


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "headlines.db"
    earlier = utcnow() - timedelta(hours=3)
    with connect(path) as conn:
        store_headlines(
            conn,
            [
                headline(
                    "Harbour bridge closed for repairs",
                    "https://example.org/bridge",
                    fetched_at=earlier,
                    summary="Traffic diverted all weekend.",
                ),
                headline(
                    "Budget surplus forecast for Dublin council",
                    "https://other.example/budget",
                    source="Other Daily",
                ),
                headline(
                    "Election results: live updates",
                    "https://example.org/live/election",
                    fetched_at=earlier,
                ),
                headline('Cats & dogs say "1 < 2" in survey', "https://example.org/xss"),
            ],
        )
        store_headlines(
            conn,
            [
                headline("Harbour bridge to reopen on Monday", "https://example.org/bridge"),
                headline(
                    "Election results: live updates as counting continues",
                    "https://example.org/live/election",
                ),
            ],
        )
        started = utcnow() - timedelta(minutes=50)
        for source, status in (("Example Wire", "ok"), ("Other Daily", "error")):
            record_fetch(
                conn,
                source=source,
                started_at=started,
                finished_at=started + timedelta(seconds=4),
                status=status,
                items_found=3,
                items_new=2 if status == "ok" else 0,
                items_changed=1,
                error="HTTP 503" if status == "error" else None,
            )
        conn.commit()
    return path


Call = Callable[..., tuple[str, dict[str, str], str]]


@pytest.fixture
def get(db_path: Path, config_path: Path) -> Call:
    app = WebApp(db_path, config_path)
    return make_client(app)


def make_client(app: WebApp) -> Call:
    def call(path: str, method: str = "GET") -> tuple[str, dict[str, str], str]:
        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        route, _, query = path.partition("?")
        environ.update(REQUEST_METHOD=method, PATH_INFO=route, QUERY_STRING=query)
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"], captured["headers"] = status, dict(headers)

        body = b"".join(app(environ, start_response)).decode("utf-8")
        return captured["status"], captured["headers"], body

    return call


def titles(body: str) -> list[str]:
    return re.findall(r'class="title"[^>]*>(.*?)</a>', body)


def test_latest_lists_newest_first_with_badges(get: Call) -> None:
    status, headers, body = get("/")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")
    shown = titles(body)
    assert shown[-1] == "Harbour bridge to reopen on Monday"
    assert "Harbour bridge to reopen on Monday" in shown
    assert 'class="badge live"' in body
    assert "2 titles" in body  # the rewritten bridge story links to its history


def test_titles_and_attributes_are_escaped(get: Call) -> None:
    _, _, body = get("/")
    assert "Cats &amp; dogs say &quot;1 &lt; 2&quot; in survey" in body
    _, _, body = get("/search?q=%22%3E%3Cb%3Ehi")
    assert "<b>hi" not in body
    assert 'value="&quot;&gt;&lt;b&gt;hi"' in body


def test_tag_and_source_filters(get: Call) -> None:
    _, _, body = get("/?tag=ie")
    assert titles(body) == ["Budget surplus forecast for Dublin council"]
    assert 'value="IE" checked' in body
    _, _, body = get("/?source=Example+Wire&since=6h")
    assert "Budget surplus" not in body
    assert "Harbour bridge" in body
    _, _, body = get("/?tag=XX")
    assert "Unknown tag(s) ignored: XX." in body


def test_since_window_defaults_per_page(get: Call) -> None:
    _, _, body = get("/")
    assert '<option value="all" selected>' in body
    _, _, body = get("/rewrites")
    assert '<option value="7d" selected>' in body
    _, _, body = get("/rewrites?since=bogus")
    assert '<option value="7d" selected>' in body


def test_times_are_local_by_default_and_utc_on_request(
    get: Call, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Australia/Perth")
    import time

    time.tzset()
    try:
        _, _, body = get("/")
        assert "Times: AWST · show UTC" in body
        assert re.search(r'<time datetime="[^"]+\+00:00" title="[^"]+Z">', body)
        _, _, body = get("/?utc=1")
        assert "Times: UTC · show AWST" in body
        assert 'name="utc" value="1"' in body  # the filter form keeps UTC
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_rewrites_hide_live_blogs_by_default(get: Call) -> None:
    _, _, body = get("/rewrites")
    assert "<del>closed for repairs</del>" in body
    assert "<ins>to reopen on Monday</ins>" in body
    assert "was: Harbour bridge closed for repairs" in body
    assert "counting continues" not in body
    assert "1 live-blog title change(s) hidden" in body

    _, _, body = get("/rewrites?live=include")
    assert "counting continues" in body
    assert "title change(s) hidden" not in body

    _, _, body = get("/rewrites?live=only&oldest=1")
    assert "Harbour" not in body
    first = body.index("first seen")
    assert first < body.index("counting continues")


def test_search_current_and_history(get: Call) -> None:
    _, _, body = get("/search?q=repairs")
    assert "No headlines match." in body
    _, _, body = get("/search?q=repairs&history=1")
    assert "Matched earlier title: Harbour bridge closed for <mark>repairs</mark>" in body
    _, _, body = get("/search?q=dublin&source=Other+Daily")
    assert "<mark>Dublin</mark>" in body
    _, _, body = get("/search?q=dublin&source=Example+Wire")
    assert "No headlines match." in body


def test_sources_page_shows_health_and_runs(get: Call) -> None:
    status, _, body = get("/sources")
    assert status == "200 OK"
    assert "<strong>1</strong> of 3 enabled source(s) healthy" in body
    assert 'class="state bad" title="HTTP 503">failed' in body
    assert "never fetched" in body  # Quiet Times
    assert "Recent runs" in body
    run = re.search(r"<td class=\"num\">1</td>.*?bad\">1</td>", body, re.S)
    assert run is not None
    _, _, body = get("/sources?tag=IE")
    assert "Example Wire" not in body.split("<tbody>")[1]


def test_article_history_page(get: Call) -> None:
    status, _, body = get("/article?url=" + quote("https://example.org/bridge", safe=""))
    assert status == "200 OK"
    assert "2 title(s), oldest first" in body
    assert body.index("first seen") < body.index("changed to")
    status, _, _ = get("/article?url=https%3A%2F%2Fnope.example%2F")
    assert status == "404 Not Found"


def test_pagination(db_path: Path, config_path: Path) -> None:
    with connect(db_path) as conn:
        store_headlines(
            conn,
            [
                headline(f"Numbered story {i:03d} for paging", f"https://example.org/n{i}")
                for i in range(60)
            ],
        )
        conn.commit()
    get = make_client(WebApp(db_path, config_path))
    _, _, first = get("/")
    assert "Older →" in first and "← Newer" not in first
    _, _, second = get("/?page=2")
    assert "← Newer" in second and "Older →" not in second
    assert set(titles(first)).isdisjoint(titles(second))
    assert len(titles(first)) + len(titles(second)) == 64


def test_read_only_methods_headers_and_assets(get: Call) -> None:
    status, headers, _ = get("/", method="POST")
    assert status == "405 Method Not Allowed"
    assert headers["Allow"] == "GET, HEAD"
    status, headers, body = get("/", method="HEAD")
    assert status == "200 OK" and body == "" and int(headers["Content-Length"]) > 0
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    status, headers, body = get("/static/app.css")
    assert headers["Content-Type"].startswith("text/css") and "--accent" in body
    assert get("/missing")[0] == "404 Not Found"


def test_external_links_open_safely(get: Call) -> None:
    _, _, body = get("/")
    assert 'href="https://example.org/bridge" target="_blank" rel="noopener noreferrer"' in body


def test_healthz(get: Call) -> None:
    status, _, body = get("/healthz")
    assert status == "200 OK"
    payload = json.loads(body)
    assert payload["status"] == "ok" and payload["schema"] == 3 and payload["articles"] == 4


def test_database_is_never_written(db_path: Path, get: Call) -> None:
    def digest() -> str:
        return hashlib.sha256(db_path.read_bytes()).hexdigest()

    before = digest()
    for path in ("/", "/rewrites", "/search?q=bridge&history=1", "/sources", "/healthz"):
        assert get(path)[0] == "200 OK"
    assert digest() == before


def test_missing_or_old_database_explains_itself(tmp_path: Path, config_path: Path) -> None:
    get = make_client(WebApp(tmp_path / "absent.db", config_path))
    status, _, body = get("/")
    assert status == "503 Service Unavailable"
    assert "appears after the first fetch" in body
    assert json.loads(get("/healthz")[2])["status"] == "error"

    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE headlines (id INTEGER PRIMARY KEY)")
    get = make_client(WebApp(old, config_path))
    status, _, body = get("/")
    assert status == "503 Service Unavailable"
    assert "schema version 0" in body and "headliner migrate" in body


def test_works_without_a_config_file(db_path: Path, tmp_path: Path) -> None:
    get = make_client(WebApp(db_path, None))
    status, _, body = get("/?tag=AU")
    assert status == "200 OK"
    assert "Tag filters need the sources file" in body
    assert get("/sources")[0] == "503 Service Unavailable"


def test_config_reloads_when_the_file_changes(get: Call, config_path: Path) -> None:
    assert "Quiet Times" in get("/sources")[2]
    text = config_path.read_text().replace("Quiet Times", "Loud Times")
    config_path.write_text(text)
    import os

    stat = config_path.stat()
    os.utime(config_path, (stat.st_atime, stat.st_mtime + 5))
    assert "Loud Times" in get("/sources")[2]


def test_word_diff_and_highlight() -> None:
    assert word_diff("a b c", "a x c") == "a <del>b</del> <ins>x</ins> c"
    assert word_diff("<i>", "<b>") == "<del>&lt;i&gt;</del> <ins>&lt;b&gt;</ins>"
    assert highlight("Police probe <x>", "pol") == "<mark>Police</mark> probe &lt;x&gt;"
    assert highlight("nothing", "") == "nothing"


def test_cli_web_defaults_to_localhost() -> None:
    args = build_parser().parse_args(["web"])
    assert (args.host, args.port) == ("127.0.0.1", 8090)


def test_times_carry_utc_iso(get: Call) -> None:
    _, _, body = get("/?utc=1")
    stamps = re.findall(r'<time datetime="([^"]+)"', body)
    assert stamps and all(datetime.fromisoformat(s).tzinfo == UTC for s in stamps)


def test_rewrites_hide_minor_changes(db_path: Path, config_path: Path) -> None:
    with connect(db_path) as conn:
        store_headlines(
            conn, [headline("Harbour bridge to reopen, on Monday", "https://example.org/bridge")]
        )
        conn.commit()
    get = make_client(WebApp(db_path, config_path))
    _, _, body = get("/rewrites")
    assert "1 minor change(s) hidden" in body
    assert "badge minor" not in body
    _, _, body = get("/rewrites?minor=1")
    assert 'class="badge minor"' in body
    assert "minor change(s) hidden" not in body
    _, _, body = get("/article?url=https%3A%2F%2Fexample.org%2Fbridge")
    assert "changed to (punctuation only)" in body
