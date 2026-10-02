"""Group headlines from different outlets that report the same story.

Each headline becomes a TF-IDF vector over the words of its title (weight 1)
and the start of its summary (lower weight); rare shared words such as names
and places count most. Headlines are taken oldest first, and each joins the
existing story whose centroid it is most similar to (cosine >= threshold,
last item within `window`), or starts a new one. Comparing against the
centroid rather than any single member keeps chains of loosely related items
from merging into one giant story.

Everything is computed on demand from stored headlines: no schema, no state.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from headliner.models import Headline

DEFAULT_THRESHOLD: Final = 0.3
DEFAULT_MEMBER_THRESHOLD: Final = 0.4
DEFAULT_WINDOW: Final = timedelta(hours=36)
SUMMARY_WORDS: Final = 30
SUMMARY_WEIGHT: Final = 0.35
# A safety cap on how many headlines one clustering pass reads.
MAX_STORY_HEADLINES: Final = 20_000

# Common English words plus newsroom boilerplate that says nothing about the story.
_STOPWORDS_TEXT: Final = """
    a about above after again against all also am an and any are as at be because been
    before being below between both but by can could did do does doing down during each
    few for from further had has have having he her here hers herself him himself his how
    i if in into is it its itself just me more most my no nor not now of off on once only
    or other our ours out over own same she should so some such than that the their them
    then there these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your yours
    says say said new news live latest update updates first last one two three after over
    get gets got make makes made may might must amid via year years day days week weeks
    time back people man woman men women told tell video watch read more breaking email
    app podcast follow sign newsletter daily morning
"""
STOPWORDS: Final = frozenset(_STOPWORDS_TEXT.split())
_WORD = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)*")


_MEDIA_TITLE: Final = re.compile(
    r"^\s*(?:watch|video|listen|live|podcast|in pictures|pictures|photos|gallery)"
    r"\s*[:|\N{EN DASH}\N{EM DASH}-]",
    re.I,
)
_MEDIA_PATH: Final = re.compile(r"/(?:videos?|av|live|podcasts?|audio|gallery)/", re.I)


def is_media(headline: Headline) -> bool:
    """A video, audio, gallery or live item: a poor name for a whole story."""
    return (
        headline.is_live
        or bool(_MEDIA_TITLE.match(headline.title))
        or bool(_MEDIA_PATH.search(headline.url))
    )


# Crude suffix stripping, enough for "survived"/"surviving"/"survives" to meet.
_SUFFIXES: Final = ("ings", "ing", "edly", "ed", "es", "s", "e")
_KEEP_ENDINGS: Final = ("ss", "us", "is")


def _stem(word: str) -> str:
    if word.isdigit() or word.endswith(_KEEP_ENDINGS):
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> list[str]:
    """Content words of `text`: lowercased, possessives and suffixes folded, stopwords out."""
    out = []
    for raw in _WORD.findall(unicodedata.normalize("NFKC", text).casefold()):
        word = re.sub(r"['\u2019]s$", "", raw).replace("'", "").replace("\u2019", "")
        word = _stem(word)
        if word in STOPWORDS or (len(word) < 3 and not any(c.isdigit() for c in word)):
            continue
        out.append(word)
    return out


@dataclass(slots=True)
class Story:
    """Headlines judged to report the same story, oldest first."""

    headlines: list[Headline] = field(default_factory=list)
    # The member most similar to the rest: its title names the story.
    lead: Headline | None = None

    @property
    def sources(self) -> list[str]:
        """Distinct outlets, in the order they first reported it."""
        return list(dict.fromkeys(headline.source for headline in self.headlines))

    @property
    def first_seen(self) -> datetime:
        return min(_when(headline) for headline in self.headlines)

    @property
    def last_seen(self) -> datetime:
        return max(_when(headline) for headline in self.headlines)

    @property
    def title(self) -> str:
        return (self.lead or self.headlines[0]).title


def _when(headline: Headline) -> datetime:
    return headline.published_at or headline.fetched_at


def _vectors(headlines: Sequence[Headline]) -> list[dict[str, float]]:
    """Unit-length TF-IDF vectors, one per headline."""
    bags: list[defaultdict[str, float]] = []
    for headline in headlines:
        bag: defaultdict[str, float] = defaultdict(float)
        for word in tokens(headline.title):
            bag[word] += 1.0
        if headline.summary:
            for word in tokens(" ".join(headline.summary.split()[:SUMMARY_WORDS])):
                bag[word] += SUMMARY_WEIGHT
        bags.append(bag)
    document_frequency: Counter[str] = Counter()
    for bag in bags:
        document_frequency.update(bag.keys())
    total = len(bags)
    vectors = []
    for bag in bags:
        weighted = {
            word: weight * (1 + math.log((1 + total) / (1 + document_frequency[word])))
            for word, weight in bag.items()
        }
        norm = math.sqrt(sum(value * value for value in weighted.values())) or 1.0
        vectors.append({word: value / norm for word, value in weighted.items() if value > 0})
    return vectors


def _cosine(vector: dict[str, float], centroid: dict[str, float], norm: float) -> float:
    if norm == 0:
        return 0.0
    small, large = (vector, centroid) if len(vector) < len(centroid) else (centroid, vector)
    return sum(value * large.get(word, 0.0) for word, value in small.items()) / norm


@dataclass(slots=True)
class _Cluster:
    members: list[int]
    centroid: dict[str, float]
    last: datetime

    def norm(self) -> float:
        return math.sqrt(sum(value * value for value in self.centroid.values()))

    def add(self, index: int, vector: dict[str, float], when: datetime) -> None:
        self.members.append(index)
        for word, value in vector.items():
            self.centroid[word] = self.centroid.get(word, 0.0) + value
        self.last = max(self.last, when)


def cluster(
    headlines: Iterable[Headline],
    *,
    threshold: float = DEFAULT_THRESHOLD,
    member_threshold: float = DEFAULT_MEMBER_THRESHOLD,
    window: timedelta = DEFAULT_WINDOW,
) -> list[Story]:
    """Group `headlines` into stories, newest story (by last item) first.

    Every headline lands in exactly one story; most stories have one member.
    """
    items = sorted(headlines, key=_when)
    vectors = _vectors(items)
    clusters: list[_Cluster] = []
    by_word: defaultdict[str, set[int]] = defaultdict(set)
    for index, (headline, vector) in enumerate(zip(items, vectors, strict=True)):
        when = _when(headline)
        title_words = set(tokens(headline.title))
        candidates = set().union(*(by_word[word] for word in title_words)) if title_words else set()
        best, best_score = None, 0.0
        for candidate in candidates:
            group = clusters[candidate]
            if when - group.last > window:
                continue
            # The story as a whole, or one member strongly: a follow-up often
            # echoes a single earlier headline more than the story's average.
            score = max(
                _cosine(vector, group.centroid, group.norm()),
                max(_cosine(vector, vectors[m], 1.0) for m in group.members)
                * threshold
                / member_threshold,
            )
            if score >= threshold and score > best_score:
                best, best_score = candidate, score
        if best is None:
            clusters.append(_Cluster(members=[index], centroid=dict(vector), last=when))
            best = len(clusters) - 1
        else:
            clusters[best].add(index, vector, when)
        for word in title_words:
            by_word[word].add(best)

    stories = []
    for group in clusters:
        # The most typical headline names the story (the first, for a pair),
        # but never a "Watch:" clip or live blog when an article is in the group.
        norm = group.norm()
        typical = len(group.members) > 2
        lead_index = max(
            group.members,
            key=lambda index: (
                not is_media(items[index]),
                _cosine(vectors[index], group.centroid, norm) if typical else 0.0,
                -index,
            ),
        )
        stories.append(
            Story(headlines=[items[index] for index in group.members], lead=items[lead_index])
        )
    stories.sort(key=lambda story: story.last_seen, reverse=True)
    return stories


def by_url(stories: Iterable[Story]) -> dict[str, Story]:
    """Each headline's URL mapped to the story it belongs to."""
    return {headline.url: story for story in stories for headline in story.headlines}
