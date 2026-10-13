"""Hybrid ranker: audio similarity, TF-IDF metadata similarity, a popularity
prior, then maximal marginal relevance (MMR) for artist and genre variety."""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.preference import Preference
from musicdiscovery.ranking import BaselineRanker, Recommendation

CONFIG_NAME = "hybrid_config.json"
VARIETY_TEXT = "added for variety: a different artist or genre from the picks so far"


class HybridConfigError(Exception):
    """Raised when the hybrid config file cannot be used."""


@dataclass(frozen=True)
class HybridConfig:
    """Weights and the MMR lambda, fixed in `hybrid_config.json`.

    `scripts/tune_hybrid.py` reproduces them from the validation queries. The
    baseline score (audio similarity plus preference terms) has weight 1; the
    other weights are relative to it.
    """

    metadata_weight: float = 0.5
    popularity_weight: float = 0.05
    mmr_lambda: float = 0.3
    genre_redundancy: float = (
        0.3  # genre's share of the MMR penalty; artist gets the rest
    )
    pool_size: int = 100

    def __post_init__(self):
        if self.metadata_weight < 0 or self.popularity_weight < 0:
            raise HybridConfigError("weights must not be negative")
        if not 0 <= self.mmr_lambda <= 1:
            raise HybridConfigError("mmr_lambda must be between 0 and 1")
        if not 0 <= self.genre_redundancy <= 1:
            raise HybridConfigError("genre_redundancy must be between 0 and 1")
        if self.pool_size < 1:
            raise HybridConfigError("pool_size must be at least 1")

    @classmethod
    def load(cls, path: Path | None = None) -> "HybridConfig":
        """Read a config file; with no path, the one shipped in the package."""
        try:
            if path is None:
                source = resources.files("musicdiscovery").joinpath(CONFIG_NAME)
                text = source.read_text(encoding="utf-8")
            else:
                text = path.read_text(encoding="utf-8")
            return cls(**json.loads(text))
        except (OSError, ValueError, TypeError) as error:
            raise HybridConfigError(f"cannot read hybrid config: {error}") from error

    def dump(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"


class TfIdf:
    """TF-IDF over token sets, stored as flat (row, column, value) arrays.

    Term frequency is binary and idf is smoothed; each row is L2-normalised.
    """

    def __init__(self, documents: list[set[str]]):
        vocabulary = {
            token: i for i, token in enumerate(sorted(set().union(*documents)))
        }
        n = len(documents)
        rows, columns = [], []
        for row, doc in enumerate(documents):
            for token in doc:
                rows.append(row)
                columns.append(vocabulary[token])
        self.rows = np.array(rows, dtype=np.intp)
        self.columns = np.array(columns, dtype=np.intp)
        df = np.bincount(self.columns, minlength=len(vocabulary))
        idf = np.log((1 + n) / (1 + df)) + 1.0
        values = idf[self.columns]
        norms = np.sqrt(np.bincount(self.rows, weights=values**2, minlength=n))
        norms[norms == 0] = 1.0
        self.values = values / norms[self.rows]
        self.tokens = list(vocabulary)
        self.size = len(vocabulary)
        self.count = n
        # Row r's entries are the slice starts[r]:starts[r + 1] of the sorted order.
        self._order = np.argsort(self.rows, kind="stable")
        self._starts = np.searchsorted(self.rows[self._order], np.arange(n + 1))

    def similarity(
        self, seed_rows: list[int], weights: Sequence[float] | None = None
    ) -> np.ndarray:
        """Cosine similarity of every track to the (weighted) mean seed vector."""
        if weights is None:
            weights = [1.0] * len(seed_rows)
        centre = np.zeros(self.size)
        for seed, weight in zip(seed_rows, weights, strict=True):
            entries = self._order[self._starts[seed] : self._starts[seed + 1]]
            np.add.at(centre, self.columns[entries], weight * self.values[entries])
        norm = np.linalg.norm(centre)
        if not norm:
            return np.zeros(self.count)
        products = self.values * (centre / norm)[self.columns]
        return np.bincount(self.rows, weights=products, minlength=self.count)


class _Metadata(TfIdf):
    """TF-IDF over genre ids and tags."""

    def __init__(self, catalog: Catalog):
        frame = catalog.frame
        super().__init__(
            [
                {f"g:{g}" for g in genres} | {f"t:{t}" for t in tags}
                for genres, tags in zip(frame["genre_ids"], frame["tags"], strict=True)
            ]
        )


class HybridRanker:
    """Baseline score + metadata similarity + popularity, re-ranked with MMR."""

    def __init__(self, config: HybridConfig | None = None):
        self.config = config or HybridConfig.load()
        self._baseline = BaselineRanker()
        self._catalog: Catalog | None = None
        self._metadata: _Metadata | None = None
        self._popularity: np.ndarray | None = None
        self._artists: np.ndarray | None = None
        self._genres: np.ndarray | None = None

    def _prepare(self, catalog: Catalog) -> tuple[_Metadata, np.ndarray]:
        if self._catalog is not catalog or self._metadata is None:
            self._metadata = _Metadata(catalog)
            self._popularity = (
                catalog.frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
            )
            self._artists, self._genres = mmr_keys(catalog)
            self._catalog = catalog
        return self._metadata, self._popularity

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        config = self.config
        base_terms, _, allowed = self._baseline.score_terms(preference, catalog)
        metadata, popularity = self._prepare(catalog)
        seed_rows = [catalog.position[s] for s in preference.seeds]
        terms = [
            ("audio" if name == "similarity" else name, text, values)
            for name, text, values in base_terms
        ]
        terms.append(
            (
                "metadata",
                "similar genres and tags",
                config.metadata_weight * metadata.similarity(seed_rows),
            )
        )
        terms.append(
            (
                "popularity",
                "popular with listeners",
                config.popularity_weight * popularity,
            )
        )
        relevance = np.sum([values for _, _, values in terms], axis=0)

        candidates = np.flatnonzero(allowed)
        if not len(candidates):
            return []
        order = np.lexsort((catalog.track_ids[candidates], -relevance[candidates]))
        pool = candidates[order[: max(config.pool_size, preference.k)]]
        picks, variety = mmr(
            pool,
            relevance[pool],
            preference.k,
            config.mmr_lambda,
            config.genre_redundancy,
            self._artists,
            self._genres,
        )
        return with_variety(catalog, picks, variety, terms)


def with_variety(
    catalog: Catalog,
    picks: list[int],
    variety: dict[int, float],
    terms: list[tuple[str, str, np.ndarray]],
) -> list[Recommendation]:
    """Build the picks' recommendations, adding the `variety` term where MMR
    jumped a more relevant track."""
    results = []
    for row in picks:
        row_terms = list(terms)
        if variety.get(row, 0.0) > 0:
            values = np.zeros(len(catalog))
            values[row] = variety[row]
            row_terms.append(("variety", VARIETY_TEXT, values))
        results.append(BaselineRanker.build(catalog, row, row_terms))
    return results


def mmr_keys(catalog: Catalog) -> tuple[np.ndarray, np.ndarray]:
    """Every row's artist and top genre, as `mmr` reads them."""
    frame = catalog.frame
    artists = frame["artist"].astype(str).to_numpy()
    genres = frame["genre_top"].astype("string").to_numpy(dtype=object)
    return artists, genres


def mmr(
    pool: np.ndarray,
    scores: np.ndarray,
    k: int,
    mmr_lambda: float,
    genre_redundancy: float,
    artists: np.ndarray,
    genres: np.ndarray,
) -> tuple[list[int], dict[int, float]]:
    """Greedy maximal marginal relevance over the pool, which is in relevance order.

    `scores` are the pool's relevance scores; `artists` and `genres` hold every
    catalog row's artist and top genre (missing genres are not strings).
    `genre_redundancy` is genre's share of the penalty; artist gets the rest.

    Returns the picked rows and, for each pick that jumped a more relevant
    track, lambda times the redundancy it avoided.
    """
    lam = mmr_lambda
    span = scores.max() - scores.min()
    rel = (scores - scores.min()) / span if span else np.zeros(len(scores))
    artists = artists[pool]
    genres = genres[pool]
    artist_taken: set[str] = set()
    genre_count: dict[str, int] = {}
    remaining = list(range(len(pool)))
    picks: list[int] = []
    variety: dict[int, float] = {}

    def redundancy(i: int) -> float:
        same_artist = 1.0 if artists[i] in artist_taken else 0.0
        genre = genres[i]
        share = 0.0
        if picks and isinstance(genre, str):
            share = genre_count.get(genre, 0) / len(picks)
        g = genre_redundancy
        return (1 - g) * same_artist + g * share

    while remaining and len(picks) < k:
        gains = [(1 - lam) * rel[i] - lam * redundancy(i) for i in remaining]
        best = remaining[int(np.argmax(gains))]  # ties go to higher relevance
        top = remaining[0]
        if best != top:
            variety[int(pool[best])] = lam * (redundancy(top) - redundancy(best))
        picks.append(int(pool[best]))
        remaining.remove(best)
        artist_taken.add(artists[best])
        if isinstance(genres[best], str):
            genre_count[genres[best]] = genre_count.get(genres[best], 0) + 1
    return picks, variety
