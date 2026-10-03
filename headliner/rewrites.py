"""What kind of change a headline rewrite is, judged from the two titles alone.

A heuristic, not a verdict: the viewer says "likely" and shows the rules.

- numbers: the figures changed and little else did ("60 ibises" to "59 ibises").
- label: a lead-in such as "Breaking:" or "Watch:" was added or dropped.
- new angle: most of the words are new; the story moved on or was re-pitched.
- reworded: everything else, the same story told differently.
"""

from __future__ import annotations

import re
from typing import Final

from headliner.stories import tokens

KINDS: Final = ("numbers", "label", "reworded", "new angle")
KIND_NOTES: Final = {
    "numbers": "the figures changed and little else did",
    "label": "a lead-in such as “Breaking:” or “Watch:” was added or dropped",
    "reworded": "the same story told in different words",
    "new angle": "most words are new: the story moved on or was re-pitched",
}
# Share of content words two titles must share to count as the same telling.
NEW_ANGLE_BELOW: Final = 0.25
NUMBERS_ABOVE: Final = 0.7
_DIGITS: Final = re.compile(r"\d+(?:[.,]\d+)*")
_LABEL: Final = re.compile(r"^\s*[A-Za-z][\w ]{0,20}?\s*[:|\N{EN DASH}\N{EM DASH}]\s+")


def _roots(title: str) -> set[str]:
    # Word starts, so "ban"/"banned" and "scooters"/"e-scooter" meet.
    return {word[:5] for word in tokens(title) if not word.isdigit()}


def _overlap(old: set[str], new: set[str]) -> float:
    union = old | new
    return len(old & new) / len(union) if union else 1.0


def _without_label(title: str) -> str:
    return _LABEL.sub("", title, count=1).strip()


def classify(old: str, new: str) -> str:
    """One of KINDS for a rewrite from `old` to `new`."""
    old_label, new_label = _without_label(old), _without_label(new)
    if (old_label != old.strip() or new_label != new.strip()) and (
        old_label.casefold() == new.strip().casefold()
        or new_label.casefold() == old.strip().casefold()
    ):
        return "label"
    overlap = _overlap(_roots(old), _roots(new))
    if _DIGITS.findall(old) != _DIGITS.findall(new) and overlap >= NUMBERS_ABOVE:
        return "numbers"
    if overlap < NEW_ANGLE_BELOW:
        return "new angle"
    return "reworded"
