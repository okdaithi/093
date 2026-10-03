"""Likely kinds of headline rewrite, on real pairs seen in testing."""

from __future__ import annotations

import pytest
from headliner.rewrites import KINDS, classify


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        (
            "Bird flu tests underway after 60 ibises found dead in Bendigo",
            "Bird flu tests underway after 59 ibises found dead in Bendigo",
            "numbers",
        ),
        (
            "Breaking: Former Liberal politician Bruce Baird dies aged 84",
            "Former Liberal politician Bruce Baird dies aged 84",
            "label",
        ),
        (
            "Stripe to create 200 new jobs in Dublin",
            "Stripe announces plans to create 200 jobs in Dublin",
            "reworded",
        ),
        (
            "US presses Europe to release 'immediately' diesel reserves as Trump threatens ban",
            "US presses Europe to release diesel reserves 'immediately' as Trump threatens ban",
            "reworded",
        ),
        (
            "How to see the Orionid meteor shower from Friday",
            "How to see the Orionid meteor shower this weekend",
            "reworded",
        ),
        (
            "Man City verdict has 'significant implications for game's integrity' - FA",
            "Man City confirm appeal against guilty verdict",
            "new angle",
        ),
        (
            "Leclerc tops Bahrain GP in Malaysia second practice",
            "Russell says Red Bull strongest after Bahrain practice",
            "new angle",
        ),
        (
            "Talks to resolve Drumcree dispute end without agreement",
            "Taoiseach urges parties not to inflame Drumcree dispute",
            "new angle",
        ),
    ],
)
def test_classify(old: str, new: str, kind: str) -> None:
    assert classify(old, new) == kind


def test_every_answer_is_a_known_kind() -> None:
    assert classify("", "") in KINDS
    assert classify("Watch: x", "Watch: y") in KINDS
