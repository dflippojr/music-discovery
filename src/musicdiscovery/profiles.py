"""Synthetic listener profiles with held-out liked tracks, and their metrics."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.preference import AXES, Preference

PROFILE_SEED = 20261012
PROFILES_PER_SPLIT = 100
INTERACTIONS_PER_PROFILE = 20
HELDOUT_SHARE = 0.3
NOISE_SHARE = 0.2
# Interactions with a play fraction below this are weak signals: dropped from
# history before ranking and never counted as held-out positives.
WEAK_SIGNAL_THRESHOLD = 0.3
SPLITS = ("validation", "test")

# How strongly each part of a hidden taste pulls a track into the interactions.
GENRE_AFFINITY = 3.0
TAG_AFFINITY = 3.0
AXIS_AFFINITY = 1.0
TAG_POOL = 20  # taste tags come from the genre's most common tags
# Deliberately not ranking.Z_CLIP: the profiles are the evaluation's ground
# truth and must not move when a ranker constant is tuned.
_Z_CLIP = 3.0
_SPLIT_ATTEMPTS = 50


class ProfileError(ValueError):
    """Raised when profiles cannot be generated or used."""


@dataclass(frozen=True)
class Interaction:
    """One synthetic listen: a track and how much of it was played (0-1)."""

    track: int
    strength: float


@dataclass(frozen=True)
class Profile:
    """A hidden taste, the history a ranker sees and the tracks it should find."""

    id: int
    split: str
    genre: str
    tags: tuple[str, ...]
    axis: str | None
    history: tuple[Interaction, ...]
    heldout: tuple[Interaction, ...]

    def seeds(self, threshold: float = WEAK_SIGNAL_THRESHOLD) -> tuple[int, ...]:
        """History tracks played at least `threshold`; weak signals are dropped."""
        return tuple(i.track for i in self.history if i.strength >= threshold)

    def positives(self, threshold: float = WEAK_SIGNAL_THRESHOLD) -> frozenset[int]:
        """Held-out tracks played at least `threshold`."""
        return frozenset(i.track for i in self.heldout if i.strength >= threshold)

    def preference(
        self, k: int, threshold: float = WEAK_SIGNAL_THRESHOLD
    ) -> Preference:
        """The filtered history as a multi-seed preference; no liked qualities.

        Each seed carries its play fraction as its strength.
        """
        kept = [i for i in self.history if i.strength >= threshold]
        if not kept:
            raise ProfileError(f"profile {self.id} has no history above {threshold}")
        return Preference(
            tuple(i.track for i in kept),
            k=k,
            strengths=tuple(i.strength for i in kept),
        )


def _taste_scores(
    catalog: Catalog, genre: str, tags: Sequence[str], axis: str | None
) -> np.ndarray:
    scores = GENRE_AFFINITY * catalog.has_genre(genre).astype(np.float64)
    for tag in tags:
        scores += TAG_AFFINITY * catalog.has_tag(tag)
    if axis is not None:
        direction, _, name = axis.partition(":")
        column = catalog.frame[AXES[name].column].to_numpy(dtype=np.float64)
        std = column.std() or 1.0
        z = np.clip((column - column.mean()) / std, -_Z_CLIP, _Z_CLIP)
        scores += AXIS_AFFINITY * (1 if direction == "more" else -1) * z
    return scores


def _genre_tags(catalog: Catalog) -> dict[str, list[str]]:
    """Each genre's most common tags, most common first, ties by name."""
    counts: dict[str, Counter] = {}
    for genre, tags in zip(
        catalog.frame["genre_top"], catalog.frame["tags"], strict=True
    ):
        if isinstance(genre, str):
            counts.setdefault(genre, Counter()).update(tags)
    return {
        genre: [t for t, _ in sorted(c.items(), key=lambda x: (-x[1], x[0]))][:TAG_POOL]
        for genre, c in counts.items()
    }


def _interactions(
    rng: np.random.Generator, scores: np.ndarray, count: int
) -> list[Interaction]:
    """Taste draws (mostly strong plays) mixed with noise (mostly weak plays)."""
    noise = int(round(count * NOISE_SHARE))
    weights = np.exp(scores - scores.max())
    taste = rng.choice(
        len(scores), size=count - noise, replace=False, p=weights / weights.sum()
    )
    rest = np.setdiff1d(np.arange(len(scores)), taste)
    random = rng.choice(rest, size=noise, replace=False)
    rows = [(int(r), float(rng.beta(5, 2))) for r in taste]
    rows += [(int(r), float(rng.beta(2, 5))) for r in random]
    return [Interaction(row, round(strength, 3)) for row, strength in rows]


def _split(
    rng: np.random.Generator, interactions: list[Interaction]
) -> tuple[list[Interaction], list[Interaction]]:
    """Hold out a share of interactions, keeping a strong one on each side."""
    held = max(1, int(round(len(interactions) * HELDOUT_SHARE)))
    for _ in range(_SPLIT_ATTEMPTS):
        order = rng.permutation(len(interactions))
        heldout = [interactions[i] for i in sorted(order[:held])]
        history = [interactions[i] for i in sorted(order[held:])]
        strong = [
            any(i.strength >= WEAK_SIGNAL_THRESHOLD for i in side)
            for side in (history, heldout)
        ]
        if all(strong):
            return history, heldout
    raise ProfileError("could not split interactions with a strong play on each side")


def generate_profiles(
    catalog: Catalog,
    per_split: int = PROFILES_PER_SPLIT,
    seed: int = PROFILE_SEED,
    interactions: int = INTERACTIONS_PER_PROFILE,
) -> list[Profile]:
    """Sample tastes and interactions with a fixed seed; one catalog gives one list."""
    if interactions < 2 or interactions > len(catalog):
        raise ProfileError(f"catalog of {len(catalog)} tracks is too small")
    rng = np.random.default_rng(seed)
    pools = _genre_tags(catalog)
    genres = sorted(pools)
    if not genres:
        raise ProfileError("catalog has no top genres to build tastes from")
    axes = sorted(AXES)
    profiles = []
    for index in range(per_split * len(SPLITS)):
        genre = genres[int(rng.integers(len(genres)))]
        pool = pools[genre]
        size = min(len(pool), int(rng.integers(2, 4)))
        tags = tuple(sorted(rng.choice(pool, size=size, replace=False).tolist()))
        axis = None
        if rng.random() < 0.5:
            direction = "more" if rng.random() < 0.5 else "less"
            axis = f"{direction}:{axes[int(rng.integers(len(axes)))]}"
        scores = _taste_scores(catalog, genre, tags, axis)
        history, heldout = _split(rng, _interactions(rng, scores, interactions))
        ids = catalog.track_ids
        profiles.append(
            Profile(
                id=index,
                split=SPLITS[index // per_split],
                genre=genre,
                tags=tags,
                axis=axis,
                history=tuple(
                    Interaction(int(ids[i.track]), i.strength) for i in history
                ),
                heldout=tuple(
                    Interaction(int(ids[i.track]), i.strength) for i in heldout
                ),
            )
        )
    return profiles


# --- metrics -----------------------------------------------------------------


def reciprocal_rank(ranked: Sequence[int], positives: frozenset[int], k: int) -> float:
    """1 / rank of the first positive in the top k, or 0 if none is there."""
    for rank, track in enumerate(ranked[:k], start=1):
        if track in positives:
            return 1.0 / rank
    return 0.0


def ndcg(ranked: Sequence[int], positives: frozenset[int], k: int) -> float:
    """nDCG@k with binary gains; the ideal list puts every positive first."""
    if not positives:
        raise ProfileError("nDCG needs at least one positive")
    discounts = 1.0 / np.log2(np.arange(2, k + 2))
    gains = [track in positives for track in ranked[:k]]
    dcg = float(np.dot(gains, discounts[: len(gains)]))
    ideal = float(discounts[: min(k, len(positives))].sum())
    return dcg / ideal
