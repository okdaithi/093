"""Feed discovery: advertised links, platform fallbacks, robots, and the YAML it prints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from headliner.cli import EXIT_FATAL, EXIT_OK, EXIT_PARTIAL_FAILURE, main
from headliner.config import Settings, parse_config
from headliner.discover import Discovery, discover, render_yaml

SITE = "https://news.example.com/"
ALLOW_ALL = "User-agent: *\nAllow: /\n"
DISALLOW_ALL = "User-agent: *\nDisallow: /\n"


def rss(title: str, links: list[str]) -> bytes:
    items = "".join(
        f"<item><title>Headline number {i} about the harbour</title><link>{link}</link>"
        "<pubDate>Fri, 02 Oct 2026 09:00:00 GMT</pubDate></item>"
        for i, link in enumerate(links)
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>'
        f"{items}</channel></rss>"
    ).encode()


def page(*feeds: str) -> str:
    links = "".join(f'<link rel="alternate" type="application/rss+xml" href="{f}">' for f in feeds)
    return f"<html><head><title>News</title>{links}</head><body>hi</body></html>"


def run(sites: list[str], settings: Settings, config_text: str | None = None) -> list[Discovery]:
    config = parse_config(config_text) if config_text else None
    return asyncio.run(discover(sites, settings, config=config))


@pytest.fixture
def mocked() -> respx.MockRouter:
    """Everything not explicitly mocked is a 404, like a real site."""
    with respx.mock(assert_all_called=False) as router:
        yield router
        # Added last so specific routes registered in the test match first.


def catch_all(router: respx.MockRouter) -> None:
    router.route().mock(return_value=httpx.Response(404))


def test_advertised_feed_is_used_and_named_from_its_title(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(SITE).mock(return_value=httpx.Response(200, text=page("/rss/latest.xml")))
    mocked.get(f"{SITE}rss/latest.xml").mock(
        return_value=httpx.Response(
            200, content=rss("Example News | Latest", [f"{SITE}a", f"{SITE}b", f"{SITE}c"])
        )
    )
    catch_all(mocked)

    [found] = run([SITE], settings)
    assert found.status == "ok"
    assert found.feed_url == f"{SITE}rss/latest.xml"
    assert found.name == "Example News"
    assert found.items == 3
    assert found.newest == datetime(2026, 10, 2, 9, 0, tzinfo=UTC)


def test_falls_back_to_platform_feed_paths(mocked: respx.MockRouter, settings: Settings) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(SITE).mock(return_value=httpx.Response(200, text=page()))
    nine_style = mocked.get(f"{SITE}rss/feed.xml").mock(
        return_value=httpx.Response(200, content=rss("Daily", [f"{SITE}x"]))
    )
    catch_all(mocked)

    [found] = run([SITE], settings)
    assert found.status == "ok"
    assert found.feed_url == f"{SITE}rss/feed.xml"
    assert nine_style.called
    # WordPress's /feed/ is tried first and 404s.
    assert found.tried[0] == f"{SITE}feed/"


def test_robots_blocking_everything_is_reported_plainly(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=DISALLOW_ALL))
    homepage = mocked.get(SITE).mock(return_value=httpx.Response(200, text=page("/feed/")))
    catch_all(mocked)

    [failed] = run([SITE], settings)
    assert failed.status == "failed"
    assert failed.note == "robots.txt blocks crawlers from the homepage and every feed URL tried"
    assert not homepage.called


def test_advertised_feed_that_is_html_is_explained(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(SITE).mock(return_value=httpx.Response(200, text=page("/feed/")))
    mocked.get(f"{SITE}feed/").mock(
        return_value=httpx.Response(200, text=page(), headers={"content-type": "text/html"})
    )
    catch_all(mocked)

    [failed] = run([SITE], settings)
    assert failed.status == "failed"
    assert f"advertised feed {SITE}feed/: not an RSS/Atom feed (got text/html)" in failed.note


def test_blocked_homepage_hints_at_bot_protection(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.route().mock(return_value=httpx.Response(403))

    [failed] = run([SITE], settings)
    assert "homepage: HTTP 403, likely bot protection" in failed.note


def test_already_configured_sites_are_not_fetched(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    catch_all(mocked)
    config = "sources:\n  - {name: Example News, url: 'https://news.example.com/rss', type: rss}\n"

    [known] = run([f"{SITE}world/"], settings, config)
    assert known.status == "configured"
    assert known.existing == "Example News"
    assert not mocked.calls


def test_section_url_suggests_an_include_pattern(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(f"{SITE}news").mock(return_value=httpx.Response(200, text=page("/feed/")))
    mocked.get(f"{SITE}feed/").mock(
        return_value=httpx.Response(
            200,
            content=rss(
                "Radio",
                [f"{SITE}news/a", f"{SITE}win/car", f"{SITE}news/b", f"{SITE}shows/c"],
            ),
        )
    )
    catch_all(mocked)

    [found] = run([f"{SITE}news"], settings)
    assert (found.include_url_pattern, found.include_kept, found.items) == ("/news/", 2, 4)


def test_section_pattern_not_suggested_when_the_feed_already_fits(
    mocked: respx.MockRouter, settings: Settings
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(f"{SITE}au").mock(return_value=httpx.Response(200, text=page("/au/rss")))
    mocked.get(f"{SITE}au/rss").mock(
        return_value=httpx.Response(200, content=rss("AU", [f"{SITE}world/a", f"{SITE}sport/b"]))
    )
    catch_all(mocked)

    [found] = run([f"{SITE}au"], settings)
    assert found.status == "ok"
    assert found.include_url_pattern is None


def test_rendered_yaml_pastes_into_a_valid_config() -> None:
    results = [
        Discovery(
            site=SITE,
            status="ok",
            feed_url="https://news.example.com/?service=rss",
            name="Example: News",
            items=3,
        ),
        Discovery(
            site="https://radio.example/news",
            status="ok",
            feed_url="https://radio.example/feed/",
            name="Radio",
            items=4,
            include_url_pattern="/news/",
            include_kept=2,
        ),
        Discovery(site="https://known.example/", status="configured", existing="Known"),
        Discovery(site="https://blocked.example/", status="failed", note="robots.txt blocks"),
    ]
    snippet = render_yaml(results, ["AU", "business"], generated=datetime(2026, 10, 2, tzinfo=UTC))
    assert "# CONFIGURED https://known.example/: as 'Known'; add tags [AU, business] there" in (
        snippet
    )
    assert "# FAILED https://blocked.example/: robots.txt blocks" in snippet

    config = parse_config("sources:\n" + snippet)
    by_name = {source.name: source for source in config.sources}
    assert set(by_name) == {"Example: News", "Radio"}
    assert by_name["Example: News"].url == "https://news.example.com/?service=rss"
    assert by_name["Radio"].include_url_pattern == "/news/"
    assert all(source.tags == ("AU", "business") for source in config.sources)


@pytest.fixture
def discover_config(tmp_path: Path) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text(
        'settings:\n  rate_limit_seconds: 0\n  user_agent: "tests (+contact: t@example.org)"\n'
        "sources:\n  - {name: Existing, url: 'https://known.example/rss', type: rss}\n",
        encoding="utf-8",
    )
    return path


def test_cli_discover_prints_yaml_and_signals_failures(
    mocked: respx.MockRouter, discover_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mocked.get(f"{SITE}robots.txt").mock(return_value=httpx.Response(200, text=ALLOW_ALL))
    mocked.get(SITE).mock(return_value=httpx.Response(200, text=page("/feed/")))
    mocked.get(f"{SITE}feed/").mock(
        return_value=httpx.Response(200, content=rss("Example News", [f"{SITE}a"]))
    )
    mocked.get("https://blocked.example/robots.txt").mock(
        return_value=httpx.Response(200, text=DISALLOW_ALL)
    )
    catch_all(mocked)
    base = ["discover", "--sources", str(discover_config), "--quiet", "--tag", "AU"]

    assert main([*base, SITE]) == EXIT_OK
    out = capsys.readouterr().out
    assert "  - name: Example News\n    url: https://news.example.com/feed/" in out
    assert "    tags: [AU]" in out

    assert main([*base, SITE, "https://blocked.example/"]) == EXIT_PARTIAL_FAILURE
    assert "# FAILED https://blocked.example/" in capsys.readouterr().out


def test_cli_discover_rejects_non_http_urls(discover_config: Path) -> None:
    args = ["discover", "--sources", str(discover_config), "--quiet", "example.com"]
    assert main(args) == EXIT_FATAL


def test_cli_discover_rejects_bad_tags(discover_config: Path) -> None:
    args = ["discover", "--sources", str(discover_config), "--quiet", "--tag", "two words", SITE]
    assert main(args) == EXIT_FATAL
