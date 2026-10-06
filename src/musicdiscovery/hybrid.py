"""Hybrid ranker: audio similarity, TF-IDF metadata similarity, a popularity
prior, then maximal marginal relevance (MMR) for artist and genre variety."""

import json
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


class _Metadata:
    """TF-IDF over genre ids and tags, stored as flat (row, column, value) arrays."""

    def __init__(self, catalog: Catalog):
        frame = catalog.frame
        documents = [
            {f"g:{g}" for g in genres} | {f"t:{t}" for t in tags}
            for genres, tags in zip(frame["genre_ids"], frame["tags"], strict=True)
        ]
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
        self.size = len(vocabulary)
        self.count = n

    def similarity(self, seed_rows: list[int]) -> np.ndarray:
        """Cosine similarity of every track to the mean seed vector."""
        centre = np.zeros(self.size)
        for seed in seed_rows:
            mask = self.rows == seed
            np.add.at(centre, self.columns[mask], self.values[mask])
        norm = np.linalg.norm(centre)
        if not norm:
            return np.zeros(self.count)
        weights = self.values * (centre / norm)[self.columns]
        return np.bincount(self.rows, weights=weights, minlength=self.count)


class HybridRanker:
    """Baseline score + metadata similarity + popularity, re-ranked with MMR."""

    def __init__(self, config: HybridConfig | None = None):
        self.config = config or HybridConfig.load()
        self._baseline = BaselineRanker()
        self._catalog: Catalog | None = None
        self._metadata: _Metadata | None = None
        self._popularity: np.ndarray | None = None

    def _prepare(self, catalog: Catalog) -> tuple[_Metadata, np.ndarray]:
        if self._catalog is not catalog or self._metadata is None:
            self._metadata = _Metadata(catalog)
            self._popularity = (
                catalog.frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
            )
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
        order = np.lexsort((catalog.track_ids[candidates], -relevance[candidates]))
        pool = candidates[order[: max(config.pool_size, preference.k)]]
        picks, variety = self._mmr(catalog, pool, relevance[pool], preference.k)

        results = []
        for row in picks:
            row_terms = list(terms)
            if variety.get(row, 0.0) > 0:
                values = np.zeros(len(catalog))
                values[row] = variety[row]
                row_terms.append(("variety", VARIETY_TEXT, values))
            results.append(BaselineRanker.build(catalog, row, row_terms))
        return results

    def _mmr(
        self, catalog: Catalog, pool: np.ndarray, scores: np.ndarray, k: int
    ) -> tuple[list[int], dict[int, float]]:
        """Greedy MMR over the pool, which is in relevance order.

        Returns the picked rows and, for each pick that jumped a more relevant
        track, lambda times the redundancy it avoided.
        """
        config = self.config
        lam = config.mmr_lambda
        span = scores.max() - scores.min()
        rel = (scores - scores.min()) / span if span else np.zeros(len(scores))
        frame = catalog.frame
        artists = frame["artist"].astype(str).to_numpy()[pool]
        genres = frame["genre_top"].astype("string").to_numpy(dtype=object)[pool]
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
            g = config.genre_redundancy
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
