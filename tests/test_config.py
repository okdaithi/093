"""Config loading and validation. Errors must name the file and the key."""

from __future__ import annotations

from pathlib import Path

import pytest
from headliner.config import ConfigError, load_config, parse_config

VALID = """
settings:
  request_timeout: 10
  rate_limit_seconds: 0.5
  user_agent: "headliner-tests/0.1 (+contact: tests@example.org)"
  max_items_per_source: 20
  concurrency: 3
sources:
  - name: Example Feed
    url: https://example.org/feed.xml
    type: rss
  - name: Example Listing
    url: https://text.example.org/
    type: html
    article_selector: ".topic-container li"
    title_selector: "a"
    link_selector: "a"
    date_selector: "time"
"""


def test_valid_config_round_trips() -> None:
    config = parse_config(VALID)
    assert config.settings.request_timeout == 10.0
    assert config.settings.concurrency == 3
    assert [source.name for source in config.sources] == ["Example Feed", "Example Listing"]
    assert config.sources[1].article_selector == ".topic-container li"
    assert config.sources[0].domain == "example.org"


def test_settings_fall_back_to_defaults() -> None:
    config = parse_config("sources:\n  - {name: A, url: 'https://a.example/f', type: rss}\n")
    assert config.settings.request_timeout == 15.0
    assert "contact" in config.settings.user_agent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("sources: [\n", "invalid YAML"),
        ("", "file is empty"),
        ("settings: {}\n", "missing required top-level key 'sources'"),
        ("sources: {}\n", "'sources' must be a list"),
        ("sources: []\n", "'sources' is empty"),
        ("sources:\n  - url: https://a.example/f\n    type: rss\n", "missing required key 'name'"),
        ("sources:\n  - name: A\n    type: rss\n", "missing required key 'url'"),
        ("sources:\n  - name: A\n    url: ftp://a.example/f\n    type: rss\n", "must be http(s)"),
        (
            "sources:\n  - name: A\n    url: https://a.example/f\n    type: gopher\n",
            "'type' must be one of html, rss",
        ),
        (
            "sources:\n  - name: A\n    url: https://a.example/f\n    type: html\n",
            "html sources require 'article_selector'",
        ),
        (
            "sources:\n  - name: A\n    url: https://a.example/f\n    type: rss\n"
            "    title_selector: h2\n",
            "only valid for type 'html'",
        ),
        (
            "sources:\n  - name: A\n    url: https://a.example/f\n    type: rss\n"
            "  - name: a\n    url: https://b.example/f\n    type: rss\n",
            "duplicate source name",
        ),
        (
            "settings:\n  concurrency: 0\nsources:\n"
            "  - {name: A, url: 'https://a.example/f', type: rss}\n",
            "'concurrency' must be greater than 0",
        ),
        (
            "settings:\n  request_timeout: fast\nsources:\n"
            "  - {name: A, url: 'https://a.example/f', type: rss}\n",
            "'request_timeout' must be a number",
        ),
        (
            "settings:\n  nope: 1\nsources:\n"
            "  - {name: A, url: 'https://a.example/f', type: rss}\n",
            "unknown key(s) nope",
        ),
        ("sources:\n  - not-a-mapping\n", "expected a mapping"),
        ("- a\n- b\n", "expected a mapping"),
    ],
)
def test_malformed_config_raises_readable_error(text: str, expected: str) -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_config(text, path=Path("sources.yaml"))
    message = str(excinfo.value)
    assert expected in message
    assert "sources.yaml" in message


def test_missing_file_names_the_path(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(missing)


def test_load_config_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "sources.yaml"
    path.write_text(VALID, encoding="utf-8")
    config = load_config(path)
    assert config.path == path
    assert len(config.sources) == 2


def test_select_filters_by_name_case_insensitively() -> None:
    config = parse_config(VALID)
    chosen = config.select(["example feed"])
    assert [source.name for source in chosen] == ["Example Feed"]


def test_select_rejects_unknown_name_and_lists_options() -> None:
    config = parse_config(VALID)
    with pytest.raises(ConfigError, match="unknown source 'Nope'"):
        config.select(["Nope"])


def test_select_skips_disabled_sources() -> None:
    text = VALID.replace("    type: rss\n", "    type: rss\n    enabled: false\n", 1)
    config = parse_config(text)
    assert [source.name for source in config.select(None)] == ["Example Listing"]
    # An explicit --only still reaches a disabled source.
    assert [source.name for source in config.select(["Example Feed"])] == ["Example Feed"]


def test_shipped_sources_yaml_is_valid() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "sources.yaml")
    assert len(config.sources) >= 8
    assert any(source.type == "html" for source in config.sources)
    assert sum(1 for source in config.sources if source.type == "rss") >= 8
    assert "contact" in config.settings.user_agent


def test_live_url_pattern_is_accepted_and_compiled() -> None:
    config = parse_config(
        "sources:\n"
        "  - {name: A, url: 'https://a.example/f', type: rss,"
        " live_url_pattern: '/as-it-happened/'}\n"
    )
    [source] = config.sources
    assert source.live_url_pattern == "/as-it-happened/"
    assert source.live_regex is not None
    assert source.live_regex.search("https://a.example/AS-IT-HAPPENED/x")


def test_invalid_live_url_pattern_is_a_config_error() -> None:
    with pytest.raises(ConfigError, match=r"live_url_pattern.*not a valid regular expression"):
        parse_config(
            "sources:\n  - {name: A, url: 'https://a.example/f', type: rss,"
            " live_url_pattern: '(unclosed'}\n"
        )


TAGGED = """
sources:
  - {name: Alpha, url: 'https://a.example/f', type: rss, tags: [AU, business]}
  - {name: Beta, url: 'https://b.example/f', type: rss, tags: au}
  - {name: Gamma, url: 'https://c.example/f', type: rss, tags: [IE]}
  - {name: Delta, url: 'https://d.example/f', type: rss, tags: [IE], enabled: false}
  - {name: Plain, url: 'https://e.example/f', type: rss}
"""


def test_tags_parse_as_a_list_or_single_string() -> None:
    config = parse_config(TAGGED)
    by_name = {s.name: s for s in config.sources}
    assert by_name["Alpha"].tags == ("AU", "business")
    assert by_name["Beta"].tags == ("au",)
    assert by_name["Plain"].tags == ()
    # Case-insensitive de-duplication; the first spelling wins.
    assert config.all_tags == ["AU", "business", "IE"]


def test_duplicate_tags_on_one_source_collapse() -> None:
    config = parse_config(
        "sources:\n  - {name: A, url: 'https://a.example/f', type: rss, tags: [AU, au, AU]}\n"
    )
    assert config.sources[0].tags == ("AU",)


@pytest.mark.parametrize("bad", ["'two words'", "[ok, '']", "{a: 1}", "[-leading]"])
def test_invalid_tags_are_config_errors(bad: str) -> None:
    with pytest.raises(ConfigError, match="tag"):
        parse_config(
            f"sources:\n  - {{name: A, url: 'https://a.example/f', type: rss, tags: {bad}}}\n"
        )


def test_select_by_tag_is_case_insensitive_and_skips_disabled() -> None:
    config = parse_config(TAGGED)
    assert [s.name for s in config.select(None, ["au"])] == ["Alpha", "Beta"]
    assert [s.name for s in config.select(None, ["IE"])] == ["Gamma"]
    assert [s.name for s in config.select(None, ["IE", "business"])] == ["Alpha", "Gamma"]
    # Names and tags together: the named sources that also carry the tag.
    assert [s.name for s in config.select(["Alpha", "Gamma"], ["AU"])] == ["Alpha"]
    # A name still reaches a disabled source.
    assert [s.name for s in config.select(["Delta"])] == ["Delta"]


def test_tagged_includes_disabled_sources_for_stored_data() -> None:
    config = parse_config(TAGGED)
    assert [s.name for s in config.tagged(["ie"])] == ["Gamma", "Delta"]


def test_unknown_tag_lists_the_tags_in_use() -> None:
    config = parse_config(TAGGED)
    with pytest.raises(ConfigError, match="unknown tag 'NZ'; tags in use: AU, business, IE"):
        config.select(None, ["NZ"])


def test_include_url_pattern_is_validated_and_compiled() -> None:
    config = parse_config(
        "sources:\n  - {name: A, url: 'https://a.example/f', type: rss,"
        " include_url_pattern: '/news/'}\n"
    )
    regex = config.sources[0].include_regex
    assert regex is not None and regex.search("https://a.example/NEWS/x")
    with pytest.raises(ConfigError, match=r"include_url_pattern.*not a valid regular"):
        parse_config(
            "sources:\n  - {name: A, url: 'https://a.example/f', type: rss,"
            " include_url_pattern: '(open'}\n"
        )


@pytest.mark.parametrize("path", ["sources.yaml", "deploy/sources.yaml"])
def test_shipped_configs_load_with_tags(path: str) -> None:
    config = load_config(Path(__file__).parent.parent / path)
    assert {"AU", "IE"} <= set(config.all_tags)
    assert all(source.tags for source in config.sources), "every shipped source is tagged"


def test_shipped_and_development_source_lists_match() -> None:
    """sources.yaml and deploy/sources.yaml differ only in settings and comments."""
    root = Path(__file__).resolve().parent.parent
    development = load_config(root / "sources.yaml")
    shipped = load_config(root / "deploy" / "sources.yaml")
    assert development.sources == shipped.sources
