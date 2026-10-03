"""The read-only web viewer: pages, filters, escaping, and never writing."""

from __future__ import annotations

import hashlib
import html
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
from headliner.store import SCHEMA_VERSION, connect, record_fetch, store_headlines
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
    status, headers, body = get("/latest")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/html")
    shown = titles(body)
    assert shown[-1] == "Harbour bridge to reopen on Monday"
    assert "Harbour bridge to reopen on Monday" in shown
    assert 'class="badge live"' in body
    assert "2 titles" in body  # the rewritten bridge story links to its history


def test_titles_and_attributes_are_escaped(get: Call) -> None:
    _, _, body = get("/latest")
    assert "Cats &amp; dogs say &quot;1 &lt; 2&quot; in survey" in body
    _, _, body = get("/search?q=%22%3E%3Cb%3Ehi")
    assert "<b>hi" not in body
    assert 'value="&quot;&gt;&lt;b&gt;hi"' in body


def test_tag_and_source_filters(get: Call) -> None:
    _, _, body = get("/latest?tag=ie")
    assert titles(body) == ["Budget surplus forecast for Dublin council"]
    assert 'value="IE" checked' in body
    _, _, body = get("/latest?source=Example+Wire&since=6h")
    assert "Budget surplus" not in body
    assert "Harbour bridge" in body
    _, _, body = get("/latest?tag=XX")
    assert "Unknown tag(s) ignored: XX." in body


def test_since_window_defaults_per_page(get: Call) -> None:
    _, _, body = get("/latest")
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
        _, _, body = get("/latest")
        assert "AWST → UTC" in body
        assert re.search(r'<time datetime="[^"]+\+00:00" title="[^"]+Z">', body)
        _, _, body = get("/latest?utc=1")
        assert "UTC → AWST" in body
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
    _, _, first = get("/latest")
    assert "Older →" in first and "← Newer" not in first
    _, _, second = get("/latest?page=2")
    assert "← Newer" in second and "Older →" not in second
    assert set(titles(first)).isdisjoint(titles(second))
    assert len(titles(first)) + len(titles(second)) == 64


def test_read_only_methods_headers_and_assets(get: Call) -> None:
    status, headers, _ = get("/latest", method="POST")
    assert status == "405 Method Not Allowed"
    assert headers["Allow"] == "GET, HEAD"
    status, headers, body = get("/latest", method="HEAD")
    assert status == "200 OK" and body == "" and int(headers["Content-Length"]) > 0
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["Referrer-Policy"] == "no-referrer"
    status, headers, body = get("/static/app.css")
    assert headers["Content-Type"].startswith("text/css") and "--accent" in body
    assert get("/missing")[0] == "404 Not Found"


def test_external_links_open_safely(get: Call) -> None:
    _, _, body = get("/latest")
    assert 'href="https://example.org/bridge" target="_blank" rel="noopener noreferrer"' in body


def test_healthz(get: Call) -> None:
    status, _, body = get("/healthz")
    assert status == "200 OK"
    payload = json.loads(body)
    assert (
        payload["status"] == "ok"
        and payload["schema"] == SCHEMA_VERSION
        and payload["articles"] == 4
    )


def test_database_is_never_written(db_path: Path, get: Call) -> None:
    def digest() -> str:
        return hashlib.sha256(db_path.read_bytes()).hexdigest()

    before = digest()
    for path in ("/", "/latest", "/rewrites", "/search?q=bridge&history=1", "/sources", "/healthz"):
        assert get(path)[0] == "200 OK"
    assert digest() == before


def test_missing_or_old_database_explains_itself(tmp_path: Path, config_path: Path) -> None:
    get = make_client(WebApp(tmp_path / "absent.db", config_path))
    status, _, body = get("/latest")
    assert status == "503 Service Unavailable"
    assert "appears after the first fetch" in body
    assert json.loads(get("/healthz")[2])["status"] == "error"

    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE headlines (id INTEGER PRIMARY KEY)")
    get = make_client(WebApp(old, config_path))
    status, _, body = get("/latest")
    assert status == "503 Service Unavailable"
    assert "schema version 0" in body and "headliner migrate" in body


def test_works_without_a_config_file(db_path: Path, tmp_path: Path) -> None:
    get = make_client(WebApp(db_path, None))
    status, _, body = get("/latest?tag=AU")
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
    _, _, body = get("/latest?utc=1")
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


def seed_story(db_path: Path) -> None:
    with connect(db_path) as conn:
        store_headlines(
            conn,
            [
                headline(
                    "Harbour bridge reopening date set for Monday",
                    "https://other.example/bridge",
                    source="Other Daily",
                ),
            ],
        )
        conn.commit()


def test_stories_page_groups_outlets(db_path: Path, config_path: Path) -> None:
    seed_story(db_path)
    get = make_client(WebApp(db_path, config_path))
    status, _, body = get("/stories")
    assert status == "200 OK"
    assert "1 stories reported by 2 or more outlets in the last 24h" in body
    assert ">2 outlets</strong>" in body
    assert '<span class="chip">AU 1</span>' in body and '<span class="chip">IE 1</span>' in body
    assert "Budget surplus" not in body
    _, _, body = get("/stories?tag=IE&min=3")
    assert "No stories match." in body
    _, _, body = get("/stories?since=bogus&min=x&sort=newest")
    assert '<option value="newest" selected>' in body


def test_story_page_and_latest_badge(db_path: Path, config_path: Path) -> None:
    seed_story(db_path)
    get = make_client(WebApp(db_path, config_path))
    _, _, body = get("/latest")
    assert body.count('class="badge outlets"') == 2
    status, _, body = get("/story?url=https%3A%2F%2Fexample.org%2Fbridge")
    assert status == "200 OK"
    assert "2 headline(s) with times" in body and "Other Daily" in body
    assert "How each outlet put it" in body and '<span class="after">first</span>' in body
    assert '<span class="shared">Harbour</span>' in body
    status, _, body = get("/story?url=https%3A%2F%2Fexample.org%2Fxss")
    assert status == "200 OK" and "Only one outlet has reported this so far." in body
    assert get("/story?url=https%3A%2F%2Fnope.example%2F")[0] == "404 Not Found"


def test_trends_page(get: Call) -> None:
    status, _, body = get("/trends")
    assert status == "200 OK"
    assert "Articles per day" in body and 'class="heat h' in body
    assert "Rewrites by outlet" in body and "Feed turnover" in body
    assert re.search(
        r'<td class="num">2</td>\s*<td class="num">1</td>', body
    )  # 2 articles, 1 rewritten
    _, _, body = get("/trends?tag=IE&since=30d")
    assert '<option value="30d" selected>' in body
    assert ">Example Wire</a></th>" not in body
    assert get("/trends?since=junk")[0] == "200 OK"


def test_grouped_mastheads_count_once(db_path: Path, tmp_path: Path) -> None:
    config = tmp_path / "grouped.yaml"
    config.write_text(
        CONFIG.replace("tags: [IE, business]}", "tags: [IE, business], group: wire}").replace(
            "tags: [AU]}", "tags: [AU], group: wire}"
        ),
        encoding="utf-8",
    )
    seed_story(db_path)
    get = make_client(WebApp(db_path, config))
    _, _, body = get("/stories?min=2")
    assert "No stories match." in body  # two mastheads, one publisher
    _, _, body = get("/story?url=https%3A%2F%2Fexample.org%2Fbridge")
    assert ">1 outlet(s)</strong>" in body and "2 mastheads" in body
    _, _, body = get("/sources")
    assert '<span class="chip small">wire</span>' in body


def test_source_profile(db_path: Path, config_path: Path) -> None:
    seed_story(db_path)
    get = make_client(WebApp(db_path, config_path))
    status, _, body = get("/source?name=example+wire")
    assert status == "200 OK"
    assert "<h1>Example Wire</h1>" in body and "When it publishes" in body
    assert 'class="heatmap hours"' in body and "Stories it reported first" in body
    assert get("/source?name=Nobody")[0] == "404 Not Found"
    _, _, body = get("/sources")
    assert 'href="/source?name=Example+Wire"' in body
    _, _, body = get("/trends")
    assert "By hour of day" in body and 'href="/source?name=' in body


def test_trends_rising_topics_and_day_links(db_path: Path, config_path: Path) -> None:
    now = utcnow()
    with connect(db_path) as conn:
        store_headlines(
            conn,
            [
                headline(f"Volcano erupts near town {i}", f"https://example.org/v{i}", source=src)
                for i, src in enumerate(["Example Wire", "Other Daily", "Quiet Times"])
            ]
            + [
                headline(
                    "Budget talks continue",
                    f"https://example.org/b{i}",
                    fetched_at=now - timedelta(days=3, hours=i),
                )
                for i in range(3)
            ],
        )
    get = make_client(WebApp(db_path, config_path))
    status, _, body = get("/trends")
    assert status == "200 OK"
    assert "Rising topics" in body and ">volcano</a>" in body and "new</span>" in body
    assert ">budget</a>" not in body  # only seen days ago
    assert '<svg class="spark"' in body and "Biggest stories" in body
    match = re.search(r'href="(/latest\?[^"]*day=(\d{4}-\d{2}-\d{2})[^"]*)"', body)
    assert match
    _, _, latest = get(html.unescape(match.group(1)))
    assert f"First fetched on {match.group(2)}" in latest
    assert get("/latest?day=nonsense")[0] == "200 OK"


def test_trends_low_sample_greyed(get: Call) -> None:
    _, _, body = get("/trends")
    assert 'class="lowsample"' in body and "(n&lt;10)" in body
    assert "All outlets" in body


def test_frozen_feed_shows_content_stale(db_path: Path, config_path: Path) -> None:
    with connect(db_path) as conn:
        now = utcnow()
        record_fetch(
            conn,
            source="Example Wire",
            started_at=now,
            finished_at=now,
            status="ok",
            items_found=12,
            items_new=0,
            newest_item=datetime(2023, 4, 18, tzinfo=UTC),
        )
        conn.commit()
    _, _, body = make_client(WebApp(db_path, config_path))("/sources")
    assert (
        '<span class="state warn" title="newest item 2023-04-18: feed frozen?">content stale'
        in body
    )
    assert "<strong>0</strong> of 3 enabled source(s) healthy" in body


def test_header_totals_are_cached_until_data_changes(db_path: Path, config_path: Path) -> None:
    app = WebApp(db_path, config_path)
    get = make_client(app)
    assert "4 articles" in get("/latest")[2]
    first = app._totals
    get("/rewrites")
    assert app._totals is first  # nothing written: reused
    with connect(db_path) as conn:
        store_headlines(
            conn, [headline("A brand new story for the cache", "https://example.org/n")]
        )
        conn.commit()
    assert "5 articles" in get("/latest")[2]


def test_api_status_reports_runs_sources_and_backups(db_path: Path, config_path: Path) -> None:
    get = make_client(WebApp(db_path, config_path))
    status, headers, body = get("/api/status")
    assert status == "200 OK" and headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["status"] == "attention"
    assert "no backups" in payload["checks"]
    assert "last run: 1 source(s) failed" in payload["checks"]
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["database"]["articles"] == 4
    assert payload["runs"][0]["failed_sources"] == ["Other Daily"]
    assert payload["sources"]["configured"] == 3
    assert {p["name"]: p["state"] for p in payload["problems"]} == {
        "Other Daily": "failed",
        "Quiet Times": "never fetched",
    }

    from headliner.backup import take_backup

    take_backup(db_path, db_path.parent / "backups")
    payload = json.loads(get("/api/status")[2])
    assert payload["backups"]["count"] == 1
    assert "no backups" not in payload["checks"]


def test_api_status_without_database(tmp_path: Path, config_path: Path) -> None:
    status, _, body = make_client(WebApp(tmp_path / "absent.db", config_path))("/api/status")
    assert status == "503 Service Unavailable"
    assert json.loads(body)["status"] == "error"


def test_briefing_shows_health_top_stories_and_rewrites(get: Call, db_path: Path) -> None:
    with connect(db_path) as conn:
        store_headlines(
            conn,
            [
                headline(
                    "Kalbarri wildfire forces evacuation of coastal town",
                    "https://example.org/kalbarri",
                ),
                headline(
                    "Wildfire evacuation ordered for coastal town of Kalbarri",
                    "https://other.example/kalbarri",
                    source="Other Daily",
                ),
            ],
        )
        conn.commit()
    status, _, body = get("/")
    assert status == "200 OK"
    assert "<h1>Briefing</h1>" in body
    assert 'aria-current="page">Briefing' in body
    # Other Daily's last fetch failed and there are no backups.
    assert "Needs attention" in body
    assert "no backups" in body
    assert "Top stories" in body
    assert "2 outlets" in body
    assert "Kalbarri" in body
    # Per-country sections only show stories not already at the top.
    assert "By country" not in body
    assert "<ins>to reopen on Monday</ins>" in body
    assert 'href="/rewrites"' in body


def test_briefing_with_no_stories_says_so(get: Call) -> None:
    _, _, body = get("/")
    assert "No story has been reported by two or more outlets yet." in body


def test_api_new_counts_articles_first_seen_after_since(get: Call) -> None:
    since = (utcnow() - timedelta(hours=2)).isoformat()
    status, headers, body = get("/api/new?since=" + quote(since))
    assert status == "200 OK"
    assert headers["Content-Type"] == "application/json"
    # Budget and Cats are new within 2 hours; Bridge and Election were first seen
    # 3 hours ago and only retitled since.
    assert json.loads(body)["latest"] == 2
    assert get("/api/new?since=yesterday")[0] == "400 Bad Request"
    assert get("/api/new?since=2026-10-01T00:00:00")[0] == "400 Bad Request"


def test_filters_group_tags_and_show_removable_chips(get: Call) -> None:
    _, _, body = get("/latest?tag=IE&source=Other+Daily")
    assert "<legend>Countries</legend>" in body
    assert "<legend>Regions &amp; topics</legend>" in body
    assert body.index(">AU</label>") < body.index("<legend>Regions")
    assert '<optgroup label="IE">' in body
    assert "2 active" in body
    assert 'class="active"' in body
    assert 'href="/latest?source=Other+Daily" title="Remove this filter">IE' in body
    assert 'href="/latest?tag=IE" title="Remove this filter">Other Daily' in body
    _, _, body = get("/latest")
    assert 'class="active"' not in body


def test_items_carry_first_seen_times_for_new_markers(get: Call) -> None:
    _, _, body = get("/latest")
    assert re.search(r'<li class="item" data-seen="\d{4}-\d\d-\d\dT[^"]+\+00:00">', body)


def test_script_is_served_and_allowed_only_from_self(get: Call) -> None:
    status, headers, body = get("/static/app.js")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/javascript")
    assert "lastVisit" in body
    _, headers, page = get("/latest")
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert '<script src="/static/app.js" defer></script>' in page
    assert "<script>" not in page


BUILD_RECORD = {
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "committed_at": "2026-10-03T05:06:04+08:00",
    "branch": "main",
    "dirty": False,
    "repository": "owner/repo",
    "built_at": "2026-10-03T00:00:00+00:00",
    "merge": {
        "commit": "0123456789abcdef0123456789abcdef01234567",
        "date": "2026-10-03T05:06:04+08:00",
        "subject": "Merge pull request #28 from owner/branch",
        "pr": 28,
        "title": "Show <deploy> details",
    },
    "pr": None,
}


def test_footer_shows_a_development_build_without_a_record(get: Call) -> None:
    _, _, body = get("/latest")
    assert "Development build" in body
    _, _, sources = get("/sources")
    assert 'id="build"' in sources
    assert "Database schema" in sources


def test_footer_and_json_show_the_installed_build(db_path: Path, config_path: Path) -> None:
    from headliner import build

    app = WebApp(db_path, config_path)
    app.build = build.parse(BUILD_RECORD)
    get = make_client(app)
    _, _, body = get("/latest")
    footer = body.split("<footer>", 1)[1]
    assert "<code>0123456</code>" in footer
    # The PR comes from the latest merge when GitHub was not reachable.
    assert 'href="https://github.com/owner/repo/pull/28"' in footer
    assert "Show &lt;deploy&gt; details" in footer
    _, _, sources = get("/sources")
    assert "Latest merge" in sources
    _, _, health = get("/healthz")
    assert json.loads(health)["build"]["merge"]["pr"] == 28
    _, _, status = get("/api/status")
    assert json.loads(status)["build"]["commit"] == BUILD_RECORD["commit"]


def test_heatmaps_have_a_shading_key(get: Call) -> None:
    _, _, body = get("/trends")
    assert 'class="legend"' in body


def test_story_page_has_a_timeline(db_path: Path, config_path: Path) -> None:
    with connect(db_path) as conn:
        store_headlines(
            conn,
            [
                headline(
                    "Harbour bridge closure extended for repairs",
                    "https://other.example/bridge",
                    source="Other Daily",
                ),
            ],
        )
        conn.commit()
    get = make_client(WebApp(db_path, config_path))
    _, _, body = get("/story?url=" + quote("https://example.org/bridge", safe=""))
    assert "<h2>Timeline</h2>" in body
    assert body.count('class="c-dot') == 2
    assert 'class="c-dot first"' in body


def test_briefing_shows_today_at_a_glance(get: Call) -> None:
    _, _, body = get("/")
    assert "Today at a glance" in body
    assert body.count('class="c-bar') == 24
    assert "style=" not in body
