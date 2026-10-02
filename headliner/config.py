"""Load and validate `sources.yaml` with errors that name the offending key."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml


@functools.lru_cache(maxsize=64)
def _compile_cached(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


DEFAULT_CONFIG_PATH: Final = Path("sources.yaml")

DEFAULT_USER_AGENT: Final = (
    "headliner/0.1 (+https://github.com/okdaithi/093; contact: you@example.com)"
)

_SOURCE_TYPES: Final = frozenset({"rss", "html"})
_HTML_REQUIRED: Final = ("article_selector", "title_selector", "link_selector")
_KNOWN_SOURCE_KEYS: Final = frozenset(
    {
        "name",
        "url",
        "type",
        "enabled",
        "date_selector",
        "summary_selector",
        "live_url_pattern",
        *_HTML_REQUIRED,
    }
)
_KNOWN_SETTINGS_KEYS: Final = frozenset(
    {
        "request_timeout",
        "rate_limit_seconds",
        "user_agent",
        "max_items_per_source",
        "concurrency",
    }
)


class ConfigError(ValueError):
    """Raised for any unreadable, malformed or invalid configuration file.

    The message is written to be shown directly to the user, so it always says
    which file and which key is at fault.
    """


@dataclass(frozen=True, slots=True)
class Settings:
    """Global knobs shared by every source."""

    request_timeout: float = 15.0
    rate_limit_seconds: float = 1.0
    user_agent: str = DEFAULT_USER_AGENT
    max_items_per_source: int = 50
    concurrency: int = 5


@dataclass(frozen=True, slots=True)
class Source:
    """One configured site. HTML selector fields are unused for `rss`."""

    name: str
    url: str
    type: str
    enabled: bool = True
    article_selector: str | None = None
    title_selector: str | None = None
    link_selector: str | None = None
    date_selector: str | None = None
    summary_selector: str | None = None
    live_url_pattern: str | None = None

    @property
    def live_regex(self) -> re.Pattern[str] | None:
        """`live_url_pattern` compiled (validated at load, so this cannot fail)."""
        return _compile_cached(self.live_url_pattern) if self.live_url_pattern else None

    @property
    def domain(self) -> str:
        """Lowercased hostname, used as the robots/rate-limit bucket key."""
        return (urlsplit(self.url).hostname or "").lower()


@dataclass(frozen=True, slots=True)
class Config:
    """A validated `sources.yaml`."""

    settings: Settings
    sources: list[Source] = field(default_factory=list)
    path: Path | None = None

    def select(self, names: list[str] | None) -> list[Source]:
        """Return enabled sources, optionally filtered by name (case-insensitive).

        Raises `ConfigError` when a requested name matches nothing, listing the
        names that are available.
        """
        enabled = [source for source in self.sources if source.enabled]
        if not names:
            return enabled

        by_key = {source.name.casefold(): source for source in self.sources}
        chosen: list[Source] = []
        for name in names:
            source = by_key.get(name.casefold())
            if source is None:
                available = ", ".join(sorted(s.name for s in self.sources))
                raise ConfigError(f"unknown source {name!r}; configured sources: {available}")
            if source not in chosen:
                chosen.append(source)
        return chosen


def _require_mapping(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where}: expected a mapping, got {type(value).__name__}")
    return value


def _require_str(mapping: dict[str, Any], key: str, where: str) -> str:
    value = mapping.get(key)
    if value is None:
        raise ConfigError(f"{where}: missing required key {key!r}")
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: {key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _optional_str(mapping: dict[str, Any], key: str, where: str) -> str | None:
    if key not in mapping or mapping[key] is None:
        return None
    value = mapping[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(
            f"{where}: {key!r} must be a non-empty string when present, got {value!r}"
        )
    return value.strip()


def _positive_number(mapping: dict[str, Any], key: str, default: float, where: str) -> float:
    if key not in mapping or mapping[key] is None:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key!r} must be a number, got {value!r}")
    if value <= 0:
        raise ConfigError(f"{where}: {key!r} must be greater than 0, got {value!r}")
    return float(value)


def _non_negative_number(mapping: dict[str, Any], key: str, default: float, where: str) -> float:
    if key not in mapping or mapping[key] is None:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: {key!r} must be a number, got {value!r}")
    if value < 0:
        raise ConfigError(f"{where}: {key!r} must be 0 or greater, got {value!r}")
    return float(value)


def _positive_int(mapping: dict[str, Any], key: str, default: int, where: str) -> int:
    if key not in mapping or mapping[key] is None:
        return default
    value = mapping[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: {key!r} must be an integer, got {value!r}")
    if value <= 0:
        raise ConfigError(f"{where}: {key!r} must be greater than 0, got {value!r}")
    return value


def _warn_unknown(mapping: dict[str, Any], known: frozenset[str], where: str) -> None:
    unknown = sorted(set(mapping) - known)
    if unknown:
        allowed = ", ".join(sorted(known))
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; allowed keys: {allowed}")


def _parse_settings(raw: Any, where: str) -> Settings:
    if raw is None:
        return Settings()
    mapping = _require_mapping(raw, where)
    _warn_unknown(mapping, _KNOWN_SETTINGS_KEYS, where)
    user_agent = _optional_str(mapping, "user_agent", where) or DEFAULT_USER_AGENT
    return Settings(
        request_timeout=_positive_number(mapping, "request_timeout", 15.0, where),
        rate_limit_seconds=_non_negative_number(mapping, "rate_limit_seconds", 1.0, where),
        user_agent=user_agent,
        max_items_per_source=_positive_int(mapping, "max_items_per_source", 50, where),
        concurrency=_positive_int(mapping, "concurrency", 5, where),
    )


def _parse_source(raw: Any, index: int, file_label: str) -> Source:
    where = f"{file_label}: sources[{index}]"
    mapping = _require_mapping(raw, where)

    name = _require_str(mapping, "name", where)
    where = f"{file_label}: source {name!r}"
    _warn_unknown(mapping, _KNOWN_SOURCE_KEYS, where)

    url = _require_str(mapping, "url", where)
    scheme = urlsplit(url).scheme.lower()
    if scheme not in {"http", "https"}:
        raise ConfigError(f"{where}: 'url' must be http(s), got {url!r}")
    if not urlsplit(url).hostname:
        raise ConfigError(f"{where}: 'url' has no hostname, got {url!r}")

    source_type = _require_str(mapping, "type", where).lower()
    if source_type not in _SOURCE_TYPES:
        allowed = ", ".join(sorted(_SOURCE_TYPES))
        raise ConfigError(f"{where}: 'type' must be one of {allowed}; got {source_type!r}")

    enabled = mapping.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"{where}: 'enabled' must be true or false, got {enabled!r}")

    live_url_pattern = _optional_str(mapping, "live_url_pattern", where)
    if live_url_pattern is not None:
        try:
            re.compile(live_url_pattern)
        except re.error as exc:
            raise ConfigError(
                f"{where}: 'live_url_pattern' is not a valid regular expression: {exc}"
            ) from exc

    selectors: dict[str, str | None] = {}
    if source_type == "html":
        for key in _HTML_REQUIRED:
            if key not in mapping:
                raise ConfigError(f"{where}: html sources require {key!r}")
            selectors[key] = _require_str(mapping, key, where)
    else:
        for key in (*_HTML_REQUIRED, "date_selector", "summary_selector"):
            if mapping.get(key) is not None:
                raise ConfigError(f"{where}: {key!r} is only valid for type 'html'")

    return Source(
        name=name,
        url=url,
        type=source_type,
        enabled=enabled,
        article_selector=selectors.get("article_selector"),
        title_selector=selectors.get("title_selector"),
        link_selector=selectors.get("link_selector"),
        date_selector=_optional_str(mapping, "date_selector", where)
        if source_type == "html"
        else None,
        summary_selector=_optional_str(mapping, "summary_selector", where)
        if source_type == "html"
        else None,
        live_url_pattern=live_url_pattern,
    )


def parse_config(text: str, *, path: Path | None = None) -> Config:
    """Parse YAML text into a validated `Config`."""
    file_label = str(path) if path else "<config>"
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{file_label}: invalid YAML: {exc}") from exc

    if raw is None:
        raise ConfigError(f"{file_label}: file is empty; expected a 'sources' list")
    document = _require_mapping(raw, file_label)
    _warn_unknown(document, frozenset({"settings", "sources"}), file_label)

    raw_sources = document.get("sources")
    if raw_sources is None:
        raise ConfigError(f"{file_label}: missing required top-level key 'sources'")
    if not isinstance(raw_sources, list):
        raise ConfigError(
            f"{file_label}: 'sources' must be a list, got {type(raw_sources).__name__}"
        )
    if not raw_sources:
        raise ConfigError(f"{file_label}: 'sources' is empty; add at least one source")

    settings = _parse_settings(document.get("settings"), f"{file_label}: settings")
    sources = [_parse_source(item, index, file_label) for index, item in enumerate(raw_sources)]

    seen: dict[str, int] = {}
    for index, source in enumerate(sources):
        key = source.name.casefold()
        if key in seen:
            raise ConfigError(
                f"{file_label}: duplicate source name {source.name!r} "
                f"at sources[{seen[key]}] and sources[{index}]"
            )
        seen[key] = index

    return Config(settings=settings, sources=sources, path=path)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    """Read and validate a config file from disk."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f"config file not found: {config_path}. "
            "Pass --sources PATH or create sources.yaml in the working directory."
        ) from exc
    except OSError as exc:
        raise ConfigError(f"could not read config file {config_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"config file {config_path} is not valid UTF-8: {exc}") from exc
    return parse_config(text, path=config_path)
