"""Front pages end to end: configuration, `fetch`, `frontpages`, `sources` and the viewer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote
from wsgiref.util import setup_testing_defaults

import httpx
import pytest
import respx
from headliner.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL_FAILURE, main
from headliner.config import ConfigError, parse_config
from headliner.store import connect
from headliner.web import WebApp

FIXTURES = Path(__file__).parent / "fixtures"

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE = "sources:\n  - name: A\n    url: https://a.example/feed.xml\n    type: rss\n"


def test_rss_only_source_has_no_front_page() -> None:
    [source] = parse_config(BASE).sources
    assert source.front_page is None
    assert source.front_page_url is None
    assert source.has_feed


def test_front_page_with_url_only_uses_generic_extraction() -> None:
    [source] = parse_config(BASE + "    front_page:\n      url: https://a.example/\n").sources
    assert source.front_page is not None
    assert source.front_page_url == "https://a.example/"
    assert source.front_page.article_selector is None


def test_front_page_with_selectors() -> None:
    text = BASE + (
        "    front_page:\n"
        "      enabled: true\n"
        "      url: https://a.example/\n"
        '      article_selector: "article.story"\n'
        '      title_selector: "h2 a"\n'
        '      link_selector: "h2 a"\n'
        '      section_selector: ".kicker"\n'
        '      published_selector: "time"\n'
        '      image_selector: "img"\n'
    )
    [source] = parse_config(text).sources
    assert source.front_page is not None
    assert source.front_page.article_selector == "article.story"
    assert source.front_page.section_selector == ".kicker"


def test_disabled_front_page_needs_no_url_and_is_not_read() -> None:
    [source] = parse_config(BASE + "    front_page:\n      enabled: false\n").sources
    assert source.front_page is not None and not source.front_page.enabled
    assert source.front_page_url is None


def test_front_page_only_source() -> None:
    text = "sources:\n  - name: P\n    type: front_page\n    url: https://p.example/\n"
    [source] = parse_config(text).sources
    assert not source.has_feed
    assert source.url == source.front_page_url == "https://p.example/"
    nested = "sources:\n  - name: P\n    type: front_page\n    front_page:\n      url: https://p.example/\n"
    assert parse_config(nested).sources[0].front_page_url == "https://p.example/"


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (
            "    front_page:\n      url: https://a.example/\n      article_selector: 'div >>> p'\n",
            "'article_selector' is not a valid CSS selector",
        ),
        (
            "    front_page:\n      url: https://a.example/\n      title_selector: h2\n",
            "apply inside each 'article_selector' match",
        ),
        ("    front_page:\n      enabled: true\n", "'url' is required"),
        ("    front_page:\n      url: ftp://a.example/\n", "must be http(s)"),
        (
            "    front_page:\n      url: https://a.example/\n      headline_selector: h2\n",
            "unknown key(s) headline_selector",
        ),
        (
            "    front_page:\n      url: https://a.example/\n      enabled: yes please\n",
            "true or false",
        ),
        ("    front_page: https://a.example/\n", "expected a mapping"),
    ],
)
def test_invalid_front_page_configuration_is_rejected(block: str, message: str) -> None:
    with pytest.raises(ConfigError, match="front_page") as excinfo:
        parse_config(BASE + block)
    assert message in str(excinfo.value)


def test_front_page_only_source_rules() -> None:
    no_url = "sources:\n  - name: P\n    type: front_page\n"
    with pytest.raises(ConfigError, match="'url' is required"):
        parse_config(no_url)
    differ = (
        "sources:\n  - name: P\n    type: front_page\n    url: https://p.example/\n"
        "    front_page:\n      url: https://q.example/\n"
    )
    with pytest.raises(ConfigError, match="differ"):
        parse_config(differ)
    selectors_on_top = (
        "sources:\n  - name: P\n    type: front_page\n    url: https://p.example/\n"
        "    article_selector: article\n"
    )
    with pytest.raises(ConfigError, match="only valid for type 'html'"):
        parse_config(selectors_on_top)


def test_shipped_sources_with_front_pages_are_valid() -> None:
    for path in (Path("sources.yaml"), Path("deploy/sources.yaml")):
        config = parse_config(path.read_text(encoding="utf-8"), path=path)
        enabled = [source for source in config.sources if source.front_page_url]
        assert enabled, f"{path}: expected some front pages to be enabled"
        assert all(source.has_feed for source in enabled)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

HOST = "news.example.com"
CONFIG = f"""
settings:
  request_timeout: 5
  rate_limit_seconds: 0
  user_agent: "headliner-tests/0.1 (+contact: tests@example.org)"
  max_items_per_source: 50
  concurrency: 2
sources:
  - name: Example Daily
    url: https://{HOST}/feed.xml
    type: rss
    front_page:
      url: https://{HOST}/
  - name: Feed Only
    url: https://feed.example.org/rss.xml
    type: rss
"""
FEED = f"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Harbour bridge reopens after week-long repair closure</title>
<link>https://{HOST}/news/2026/10/04/harbour-bridge-reopens</link></item>
<item><title>A feed story that the front page does not show</title>
<link>https://{HOST}/news/2026/10/04/feed-only-story</link></item>
</channel></rss>"""


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "headlines.db"


def mock_sites(front_page: httpx.Response | None = None) -> dict[str, respx.Route]:
    respx.get(f"https://{HOST}/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://feed.example.org/robots.txt").mock(return_value=httpx.Response(404))
    page = front_page or httpx.Response(
        200, content=(FIXTURES / "front_page.html").read_bytes(), headers={"etag": '"v1"'}
    )
    return {
        "feed": respx.get(f"https://{HOST}/feed.xml").mock(
            return_value=httpx.Response(200, text=FEED)
        ),
        "page": respx.get(f"https://{HOST}/").mock(return_value=page),
        "other": respx.get("https://feed.example.org/rss.xml").mock(
            return_value=httpx.Response(200, content=(FIXTURES / "sample_feed.xml").read_bytes())
        ),
    }


def fetch(config_path: Path, db_path: Path, *extra: str) -> int:
    return main(["fetch", "--sources", str(config_path), "--db", str(db_path), *extra])


@respx.mock
def test_fetch_merges_feed_and_front_page(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_sites()
    assert fetch(config_path, db_path) == EXIT_OK
    logged_text = capsys.readouterr().err

    with closing(connect(db_path)) as conn:
        rows = dict(
            conn.execute(
                "SELECT url, acquisition FROM headlines WHERE source = 'Example Daily'"
            ).fetchall()
        )
        logged = conn.execute(
            "SELECT status, headlines, items_merged_feed FROM front_page_log"
        ).fetchall()
    # 2 feed items + 9 front-page headlines, 1 of them the same article.
    assert len(rows) == 10
    assert rows[f"https://{HOST}/news/2026/10/04/harbour-bridge-reopens"] == "front_page,rss"
    assert rows[f"https://{HOST}/news/2026/10/04/feed-only-story"] == "rss"
    assert list(logged[0]) == ["healthy", 9, 1]
    assert (
        'source="Example Daily" rss=2 front_page=9 overlap=1 new_from_front_page=8' in logged_text
    )
    assert "front pages: 1 attempted, 1 healthy; 9 headline(s), 8 new, 1 merged" in logged_text


@respx.mock
def test_second_run_sends_conditional_request(config_path: Path, db_path: Path) -> None:
    routes = mock_sites()
    assert fetch(config_path, db_path) == EXIT_OK
    routes["page"].mock(return_value=httpx.Response(304))
    assert fetch(config_path, db_path) == EXIT_OK
    assert routes["page"].calls.last.request.headers["if-none-match"] == '"v1"'
    with closing(connect(db_path)) as conn:
        statuses = [row[0] for row in conn.execute("SELECT status FROM front_page_log ORDER BY id")]
    assert statuses == ["healthy", "not_modified"]


@respx.mock
def test_a_blocked_front_page_leaves_the_feed_and_exit_code_alone(
    config_path: Path, db_path: Path
) -> None:
    mock_sites(front_page=httpx.Response(403))
    assert fetch(config_path, db_path) == EXIT_OK
    with closing(connect(db_path)) as conn:
        feed = conn.execute(
            "SELECT status FROM fetch_log WHERE source = 'Example Daily'"
        ).fetchone()
        page = conn.execute("SELECT status, error FROM front_page_log").fetchone()
        stored = conn.execute(
            "SELECT COUNT(*) FROM headlines WHERE source = 'Example Daily'"
        ).fetchone()
    assert feed[0] == "ok"
    assert tuple(page) == ("blocked", "HTTP 403")
    assert stored[0] == 2


@respx.mock
def test_a_failed_feed_still_gets_front_page_headlines(config_path: Path, db_path: Path) -> None:
    routes = mock_sites()
    routes["feed"].mock(return_value=httpx.Response(404))
    assert fetch(config_path, db_path) == EXIT_PARTIAL_FAILURE
    with closing(connect(db_path)) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) FROM headlines WHERE source = 'Example Daily'"
        ).fetchone()[0]
    assert stored == 9


@respx.mock
def test_no_front_pages_flag(config_path: Path, db_path: Path) -> None:
    routes = mock_sites()
    assert fetch(config_path, db_path, "--no-front-pages") == EXIT_OK
    assert not routes["page"].called
    with closing(connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM front_page_log").fetchone()[0] == 0


@respx.mock
def test_front_pages_only_flag(config_path: Path, db_path: Path) -> None:
    routes = mock_sites()
    assert fetch(config_path, db_path, "--front-pages-only") == EXIT_OK
    assert not routes["feed"].called and not routes["other"].called
    with closing(connect(db_path)) as conn:
        # The feeds did not run, so they log nothing.
        assert conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM front_page_log").fetchone()[0] == 1


def test_the_two_flags_exclude_each_other(config_path: Path, db_path: Path) -> None:
    with pytest.raises(SystemExit):
        fetch(config_path, db_path, "--no-front-pages", "--front-pages-only")


@respx.mock
def test_dry_run_prints_front_page_headlines_and_writes_nothing(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_sites()
    assert fetch(config_path, db_path, "--dry-run") == EXIT_OK
    assert "Dockers claim flag in a thriller at the MCG" in capsys.readouterr().out
    assert not db_path.exists()


# --------------------------------------------------------------------------
# frontpages (validation) and sources
# --------------------------------------------------------------------------


def article(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, html=f'<link rel="canonical" href="{request.url}"><h1>x</h1>')


@respx.mock
def test_frontpages_validates_configured_pages(
    config_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("headliner.cli.VALIDATE_MIN_GAP", 0)
    mock_sites()
    respx.get(url__regex=rf"https://{HOST}/.+").mock(side_effect=article)
    code = main(["frontpages", "--sources", str(config_path), "--format", "json"])
    assert code == EXIT_OK
    [report] = json.loads(capsys.readouterr().out)
    assert report["source"] == "Example Daily"
    assert report["valid"] is True
    assert report["unique_headline_count"] == 9
    assert report["accessible_article_count"] == 2
    assert report["headlines"][0]["position"] == 1


@respx.mock
def test_frontpages_checks_an_unconfigured_url_and_reports_failure(
    config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    respx.get("https://blocked.example.net/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://blocked.example.net/").mock(return_value=httpx.Response(429))
    code = main(
        [
            "frontpages",
            "--sources",
            str(config_path),
            "--url",
            "https://blocked.example.net/",
            "--follow",
            "0",
        ]
    )
    assert code == EXIT_PARTIAL_FAILURE
    out = capsys.readouterr().out
    assert "blocked" in out and "INVALID" in out
    assert "blocked: HTTP 429" in out


def test_frontpages_needs_something_to_check(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(BASE, encoding="utf-8")
    assert main(["frontpages", "--sources", str(path)]) == EXIT_FATAL
    assert main(["frontpages", "--sources", str(path), "--only", "A"]) == EXIT_FATAL
    assert main(["frontpages", "--sources", str(path), "--url", "ftp://x.example/"]) == EXIT_FATAL


@respx.mock
def test_sources_json_reports_both_paths(
    config_path: Path, db_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mock_sites()
    fetch(config_path, db_path)
    capsys.readouterr()
    assert (
        main(["sources", "--sources", str(config_path), "--db", str(db_path), "--format", "json"])
        == 0
    )
    payload = {item["name"]: item for item in json.loads(capsys.readouterr().out)}
    daily = payload["Example Daily"]
    assert daily["rss"]["status"] == "ok"
    assert daily["front_page"]["status"] == "healthy"
    assert daily["front_page"]["headlines"] == 9
    assert daily["front_page"]["merged_with_feed"] == 1
    assert payload["Feed Only"]["front_page"] is None
    # The existing fields are still there.
    assert daily["last_status"] == "ok" and daily["items"] == 10

    assert main(["sources", "--sources", str(config_path), "--db", str(db_path)]) == 0
    table = capsys.readouterr().out
    assert "FRONT PAGE" in table and "healthy (9)" in table


# --------------------------------------------------------------------------
# Viewer
# --------------------------------------------------------------------------


def call(app: WebApp, path: str) -> tuple[str, str]:
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    route, _, query = path.partition("?")
    environ.update(REQUEST_METHOD="GET", PATH_INFO=route, QUERY_STRING=query)
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status

    body = b"".join(app(environ, start_response)).decode("utf-8")
    return captured["status"], body


@respx.mock
def test_viewer_shows_provenance_without_duplicates(config_path: Path, db_path: Path) -> None:
    mock_sites()
    fetch(config_path, db_path)
    app = WebApp(db_path, config_path)

    status, body = call(app, "/latest")
    assert status == "200 OK"
    assert body.count(">Harbour bridge reopens after week-long repair closure<") == 1
    assert body.count('class="badge fp"') == 9
    assert "Found on the feed and the front page (#1 on the front page" in body
    # Feed-only articles carry no front-page badge.
    feed_only = body.split("A feed story that the front page does not show")[0].rsplit("<li", 1)[1]
    assert "badge fp" not in feed_only

    url = quote(f"https://{HOST}/news/2026/10/04/harbour-bridge-reopens", safe="")
    status, body = call(app, f"/article?url={url}")
    assert status == "200 OK"
    assert "Found on the feed and the front page" in body

    status, body = call(app, "/sources")
    assert "<th>Front page</th>" in body
    assert "9 headlines" in body

    payload = json.loads(call(app, "/api/status")[1])
    assert payload["front_pages"] == {
        "configured": 1,
        "states": {"healthy": 1},
        "headlines": 9,
        "problems": [],
    }


@respx.mock
def test_front_page_problems_never_set_attention(config_path: Path, db_path: Path) -> None:
    mock_sites(front_page=httpx.Response(403))
    fetch(config_path, db_path)
    payload = json.loads(call(WebApp(db_path, config_path), "/api/status")[1])
    assert payload["front_pages"]["problems"][0]["state"] == "blocked"
    assert not any("front" in check for check in payload["checks"])


def test_schema_has_front_page_log(db_path: Path) -> None:
    with closing(connect(db_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(front_page_log)")}
    assert {"status", "http_status", "final_url", "response_ms", "etag"} <= columns
    with closing(sqlite3.connect(db_path)) as raw:
        assert raw.execute("PRAGMA user_version").fetchone()[0] == 5
