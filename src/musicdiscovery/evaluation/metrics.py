"""The metrics, the held-out tag split and the bootstrap interval."""

from collections.abc import Sequence

import numpy as np
import pandas as pd

from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation.queries import K, Query
from musicdiscovery.preference import AXES, Preference, Quality
from musicdiscovery.rankers import EvaluationError
from musicdiscovery.ranking import Recommendation

BOOTSTRAP_SEED = 7
BOOTSTRAP_RESAMPLES = 1000
HOLDOUT_FRACTION = 0.5
HOLDOUT_SEED = 20261006

METRICS: dict[str, str] = {
    "genre_precision": "Same-genre rate@10 (genre precision)",
    "genre_ndcg": "Same-genre nDCG@10 (genre nDCG)",
    "tag_overlap": "Tag overlap@10 (mean Jaccard with the seed)",
    "heldout_tags": "Held-out tag overlap@10 (mean Jaccard of hidden tags)",
    "adherence": "Preference adherence@10",
    "artist_diversity": "Artist diversity (distinct artists / list length)",
    "popularity": "Popularity (mean listen percentile of results)",
    "coverage": "Catalog coverage",
}


# Which rankers can see the label each metric is scored against.
METRIC_NOTES: dict[str, str] = {
    "genre_precision": (
        "Scored on `genre_top`: `popular` filters on it, `hybrid` scores on it "
        "through genre ids and tags and `profile` through genre ids, tags and the "
        "genre name, so they see the label; `baseline` (audio features only), "
        "`audio-embedding` (clip vectors only) and `random` do not."
    ),
    "genre_ndcg": (
        "Scored on `genre_top`; the same rankers see the label as for the "
        "same-genre rate."
    ),
    "tag_overlap": (
        "Scored on full tag sets: `hybrid` and `profile` score on tags, and a "
        "liked `tag:` quality can name one of the seed's tags, so these rankers "
        "see the label."
    ),
    "heldout_tags": (
        "Rankers see only a visible half of each track's tags; the score uses "
        "the hidden half, which no ranker sees. Queries whose seed has fewer "
        "than two tags are skipped."
    ),
}

PROFILE_METRICS: dict[str, str] = {
    "mrr": "MRR@10 (reciprocal rank of the first held-out liked track)",
    "ndcg": "nDCG@10 (binary gains over held-out liked tracks)",
}

MIN_TAGS_FOR_HOLDOUT = 2


class HeldOut:
    """A seeded split of every track's tags into visible and held-out halves.

    Each track's split depends only on the seed and its track id, so it does not
    change with row order. Tracks with fewer than two tags keep everything
    visible and have no held-out tags.
    """

    def __init__(
        self,
        catalog: Catalog,
        fraction: float = HOLDOUT_FRACTION,
        seed: int = HOLDOUT_SEED,
    ):
        if not 0 < fraction < 1:
            raise EvaluationError(
                f"hold-out fraction must be in (0, 1), got {fraction}"
            )
        self.fraction = fraction
        self.seed = seed
        self.catalog = catalog
        self.visible: list[frozenset] = []
        self.held: list[frozenset] = []
        for track_id, tags in zip(
            catalog.track_ids, catalog.frame["tags"], strict=True
        ):
            ordered = sorted(tags)
            if len(ordered) < MIN_TAGS_FOR_HOLDOUT:
                self.visible.append(frozenset(ordered))
                self.held.append(frozenset())
                continue
            rng = np.random.default_rng([seed, int(track_id)])
            order = rng.permutation(len(ordered))
            hidden = min(max(round(len(ordered) * fraction), 1), len(ordered) - 1)
            self.held.append(frozenset(ordered[i] for i in order[:hidden]))
            self.visible.append(frozenset(ordered[i] for i in order[hidden:]))

    def view(self) -> Catalog:
        """The catalog as rankers see it: visible tags only."""
        frame = self.catalog.frame.copy()
        frame["tags"] = pd.Series(
            [sorted(tags) for tags in self.visible], index=frame.index, dtype=object
        )
        return Catalog(frame)

    def preference(self, query: Query, k: int) -> Preference:
        """The query's preference without a liked tag the ranker may not see."""
        row = self.catalog.position[query.seed]
        likes = [
            text
            for text in query.likes
            if not text.lower().startswith("tag:")
            or text.partition(":")[2].strip() in self.visible[row]
        ]
        return Preference.from_strings([query.seed], likes, k=k)

    def overlap(self, seed_row: int, rows: np.ndarray) -> float:
        """Mean Jaccard of held-out tags with the seed's; nan if undefined."""
        seed_held = self.held[seed_row]
        if not seed_held or not len(rows):
            return float("nan")
        return float(np.mean([_jaccard(seed_held, self.held[r]) for r in rows]))


class Context:
    """Per-catalog lookups shared by every query and ranker."""

    def __init__(self, catalog: Catalog):
        frame = catalog.frame
        self.catalog = catalog
        # A missing top genre is "": it never equals a seed's genre, so it is a miss.
        self.genres = (
            frame["genre_top"].astype("string").fillna("").to_numpy(dtype=object)
        )
        self.tags = [frozenset(t) for t in frame["tags"]]
        self.artists = frame["artist"].astype(str).to_numpy()
        self.percentile = catalog.popularity
        self.relevant_total = {
            g: int(np.sum(self.genres == g)) for g in set(self.genres) if g
        }


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def query_metrics(
    ctx: Context, query: Query, results: Sequence[Recommendation], k: int = K
) -> tuple[dict[str, float], np.ndarray]:
    """Per-query metric values (nan where undefined) and the recommended rows."""
    catalog = ctx.catalog
    rows = np.array([catalog.position[r.track_id] for r in results], dtype=int)
    seed_row = catalog.position[query.seed]
    nan = float("nan")
    out = dict.fromkeys(METRICS, nan)
    out.pop("coverage")
    out.pop("heldout_tags")  # needs the hold-out; scored in `evaluate`
    if not len(rows):
        return out, rows

    seed_genre = ctx.genres[seed_row]
    if seed_genre:
        hits = (ctx.genres[rows] == seed_genre).astype(float)
        out["genre_precision"] = float(hits.mean())
        gains = hits / np.log2(np.arange(2, len(hits) + 2))
        available = min(k, ctx.relevant_total[seed_genre] - 1)
        ideal = (1.0 / np.log2(np.arange(2, available + 2))).sum()
        out["genre_ndcg"] = float(gains.sum() / ideal) if ideal else nan

    seed_tags = ctx.tags[seed_row]
    if seed_tags:
        out["tag_overlap"] = float(
            np.mean([_jaccard(seed_tags, ctx.tags[r]) for r in rows])
        )

    shares = []
    for text in query.likes:
        quality = Quality.parse(text)
        if quality.kind != "axis":
            continue
        column = catalog.frame[AXES[quality.name].column].to_numpy(dtype=np.float64)
        moved = (column[rows] - column[seed_row]) * quality.direction > 0
        shares.append(float(moved.mean()))
    if shares:
        out["adherence"] = float(np.mean(shares))

    out["artist_diversity"] = len(set(ctx.artists[rows])) / len(rows)
    out["popularity"] = float(ctx.percentile[rows].mean())
    return out, rows


def nanmean(values: Sequence[float] | np.ndarray) -> float:
    """Mean of the values that are not nan; nan if none are left."""
    array = np.asarray(values, dtype=np.float64)
    valid = array[~np.isnan(array)]
    return float(valid.mean()) if len(valid) else float("nan")


def bootstrap_interval(
    values: np.ndarray, resamples: np.ndarray
) -> tuple[float, float, float]:
    """Mean and bootstrap 95% interval of per-query values (nan entries skipped)."""
    picked = values[resamples]
    valid = ~np.isnan(picked)
    counts = valid.sum(axis=1)
    sums = np.where(valid, picked, 0.0).sum(axis=1)
    means = sums[counts > 0] / counts[counts > 0]
    if not len(means):
        nan = float("nan")
        return nan, nan, nan
    lo, hi = np.percentile(means, [2.5, 97.5])
    return nanmean(values), float(lo), float(hi)


def rounded(value: float) -> float | None:
    return None if np.isnan(value) else round(float(value), 6)
