"""Escaped HTML building blocks shared by the web viewer and its charts."""

from __future__ import annotations

import html
from collections.abc import Iterable
from typing import Final


class Markup(str):
    """Text that is already HTML; `esc` passes it through unchanged."""

    __slots__ = ()


EMPTY: Final = Markup("")


def esc(value: object) -> Markup:
    """HTML-escape `value` unless it is already `Markup`."""
    if isinstance(value, Markup):
        return value
    return Markup(html.escape("" if value is None else str(value), quote=True))


def render(template: str, **values: object) -> Markup:
    """`template.format(**values)` with every value escaped (Markup passes through)."""
    return Markup(template.format(**{key: esc(value) for key, value in values.items()}))


def join(parts: Iterable[object], separator: str = "") -> Markup:
    """Concatenate escaped `parts`."""
    return Markup(separator.join(esc(part) for part in parts))
