"""Grouping headlines from different outlets into stories."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from headliner.models import Headline
from headliner.stories import by_url, cluster, tokens

BASE = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)


def item(
    source: str, title: str, slug: str, hours: float = 0, summary: str | None = None
) -> Headline:
    return Headline.create(
        source=source,
        title=title,
        url=f"https://{source.lower().replace(' ', '')}.example/{slug}",
        published_at=BASE + timedelta(hours=hours),
        fetched_at=BASE + timedelta(hours=hours),
        summary=summary,
    )


# Real headlines from 2 October 2026, plus unrelated ones around them.
PIKE = [
    item("RTE News", "Woman in US who survived execution in critical condition", "p1"),
    item(
        "BBC News",
        "Christa Pike in critical condition after surviving two lethal injections",
        "p2",
        2,
    ),
    item(
        "The Age", "Christa Pike fighting for life after surviving two lethal injections", "p3", 9
    ),
    item(
        "NPR News",
        "Christa Pike receiving 'life-saving medical care' after surviving execution",
        "p4",
        10,
    ),
]
BAIRD = [
    item("ABC News AU", "Former Liberal politician Bruce Baird dies aged 84", "b1", 1),
    item(
        "The Guardian AU",
        "Bruce Baird, former federal Liberal MP and NSW minister, dies aged 84",
        "b2",
        2,
    ),
    item(
        "The West Australian",
        "Bruce Baird, the Liberal stalwart who fought for refugees, has died aged 84",
        "b3",
        4,
    ),
]
NOISE = [
    item("RTE News", "Invasive Asian hornet nest located in Cork", "n1", 3),
    item(
        "Sky News",
        "Hundreds of French schools to stay closed after violent student protests",
        "n2",
        3,
    ),
    item("Al Jazeera", "Spain housing protests rage ahead of key government vote", "n3", 5),
    item("BBC News", "Alison Hammond has heart check-up in hospital", "n4", 6),
]


def groups() -> list[set[str]]:
    stories = cluster([*NOISE, *PIKE, *BAIRD])
    return [{headline.url for headline in story.headlines} for story in stories]


def test_same_story_from_different_outlets_is_grouped() -> None:
    found = groups()
    assert {headline.url for headline in PIKE} in found
    assert {headline.url for headline in BAIRD} in found


def test_unrelated_headlines_stay_alone() -> None:
    found = groups()
    for headline in NOISE:
        assert {headline.url} in found
    assert len(found) == len(NOISE) + 2


def test_story_properties() -> None:
    story = by_url(cluster([*PIKE, *NOISE]))[PIKE[0].url]
    assert story.sources == ["RTE News", "BBC News", "The Age", "NPR News"]
    assert story.first_seen == BASE
    assert story.last_seen == BASE + timedelta(hours=10)
    assert story.lead in PIKE
    assert story.title == story.lead.title  # type: ignore[union-attr]


def test_items_far_apart_in_time_start_a_new_story() -> None:
    later = item("Sky News", "Former Liberal politician Bruce Baird dies aged 84", "late", 60)
    stories = cluster([*BAIRD, later])
    assert [len(story.headlines) for story in stories] == [1, 3]


def test_stories_are_newest_first_and_cover_every_headline() -> None:
    stories = cluster([*PIKE, *BAIRD, *NOISE])
    assert sum(len(story.headlines) for story in stories) == len(PIKE) + len(BAIRD) + len(NOISE)
    lasts = [story.last_seen for story in stories]
    assert lasts == sorted(lasts, reverse=True)
    assert cluster([]) == []


def test_tokens_fold_case_possessives_plurals_and_stopwords() -> None:
    assert tokens("Christa Pike’s lawyers say the injections FAILED") == [
        "christa",
        "pike",
        "lawyer",
        "injection",
        "fail",
    ]
    assert tokens("G7 to release 100 million barrels") == [
        "g7",
        "releas",
        "100",
        "million",
        "barrel",
    ]
    assert tokens("survived") == tokens("surviving") == tokens("survives")
