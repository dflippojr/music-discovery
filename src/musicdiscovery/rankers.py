"""The ranker registry: every ranker by name, and the two reference rankers.

Adding a ranker means adding one entry to `RANKERS`. The CLI's `recommend
--ranker` choices are the entries marked as arms, `evaluate --ranker` offers
every entry, and `evaluate` scores the entries marked `evaluated` when no
ranker is named.

`EvaluationError` lives here, not in `musicdiscovery.evaluation`, because the
evaluation package imports this registry and `make_ranker` raises it.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np

from musicdiscovery.audio_embedding import DEFAULT_VECTORS, AudioEmbeddingRanker
from musicdiscovery.catalog import Catalog
from musicdiscovery.hybrid import HybridRanker
from musicdiscovery.preference import Preference
from musicdiscovery.profile_ranker import ProfileDiverseRanker, ProfileRanker
from musicdiscovery.ranking import (
    BaselineRanker,
    Ranker,
    Recommendation,
    allowed_rows,
    display_title,
)


class EvaluationError(Exception):
    """Raised when queries or rankers cannot be used."""


# --- reference rankers -------------------------------------------------------


def _bare(catalog: Catalog, rows: Sequence[int]) -> list[Recommendation]:
    frame = catalog.frame
    return [
        Recommendation(
            track_id=int(catalog.track_ids[row]),
            title=display_title(frame, row, int(catalog.track_ids[row])),
            artist=str(frame["artist"].iloc[row]),
            score=0.0,
            terms=(),
            explanations=(),
        )
        for row in rows
    ]


class RandomRanker:
    """Uniformly random tracks; the same preference always gives the same list."""

    def __init__(self, seed: int = 0):
        self.seed = seed

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        rng = np.random.default_rng([self.seed, *preference.seeds])
        candidates = np.flatnonzero(allowed_rows(preference, catalog))
        picked = rng.permutation(candidates)[: preference.k]
        return _bare(catalog, picked.tolist())


class PopularRanker:
    """The most listened-to tracks in the first seed's top genre."""

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        allowed = allowed_rows(preference, catalog)
        genre = catalog.frame["genre_top"].iloc[catalog.position[preference.seeds[0]]]
        if isinstance(genre, str):
            allowed &= catalog.has_genre(genre)
        candidates = np.flatnonzero(allowed)
        listens = catalog.frame["listens"].to_numpy(dtype=np.float64)[candidates]
        order = np.lexsort((catalog.track_ids[candidates], -listens))
        return _bare(catalog, candidates[order[: preference.k]].tolist())


def _audio_embedding(
    vectors: Path | None = None,
    description: tuple[str, np.ndarray] | None = None,
) -> AudioEmbeddingRanker:
    """The audio-embedding ranker on the vector cache at `vectors`, if given,
    with an optional description vector (from `describe_vector`)."""
    return AudioEmbeddingRanker(
        path=DEFAULT_VECTORS if vectors is None else vectors, description=description
    )


# --- the registry ------------------------------------------------------------


@dataclass(frozen=True)
class RankerEntry:
    """How to build one ranker, and where it is offered.

    An `arm` is a ranker a listener can use (`recommend --ranker`); a
    `reference` ranker exists only to put the arms' numbers in context.
    `evaluated` entries are the ones `evaluate` scores by default. `options`
    names the keyword options the factory takes; `make_ranker` passes only
    those, so a caller can offer every option to every entry.
    """

    factory: Callable[..., Ranker]
    kind: Literal["arm", "reference"]
    evaluated: bool
    options: tuple[str, ...] = ()

    def __call__(self, **options: object) -> Ranker:
        return self.factory(
            **{name: value for name, value in options.items() if name in self.options}
        )


RANKERS: dict[str, RankerEntry] = {
    "baseline": RankerEntry(BaselineRanker, "arm", evaluated=True),
    "hybrid": RankerEntry(HybridRanker, "arm", evaluated=True),
    "profile": RankerEntry(ProfileRanker, "arm", evaluated=True),
    "profile-diverse": RankerEntry(ProfileDiverseRanker, "arm", evaluated=False),
    "random": RankerEntry(RandomRanker, "reference", evaluated=True),
    "popular": RankerEntry(PopularRanker, "reference", evaluated=True),
    # Not evaluated by default: it needs the vector cache from `embed-audio`.
    "audio-embedding": RankerEntry(
        _audio_embedding,
        "arm",
        evaluated=False,
        options=("vectors", "description"),
    ),
}

ARMS: tuple[str, ...] = tuple(
    name for name, entry in RANKERS.items() if entry.kind == "arm"
)
REFERENCE_RANKERS: tuple[str, ...] = tuple(
    name for name, entry in RANKERS.items() if entry.kind == "reference"
)
EVALUATED: tuple[str, ...] = tuple(
    name for name, entry in RANKERS.items() if entry.evaluated
)


def make_ranker(name: str, **options: object) -> Ranker:
    """A fresh ranker by registry name.

    `options` (for example `vectors` and `description`) go only to the entries
    that take them, so a caller may pass them whichever ranker it names.
    """
    if name not in RANKERS:
        raise EvaluationError(f"unknown ranker {name!r}; choose from {sorted(RANKERS)}")
    return RANKERS[name](**options)
