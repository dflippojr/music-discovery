"""Profile ranker: a learned linear scoring head over audio, metadata, free-text
and popularity features, computed against a listener's whole history."""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.hybrid import (
    HybridConfig,
    TfIdf,
    _Metadata,
    mmr,
    mmr_keys,
    with_variety,
)
from musicdiscovery.jsonfiles import load_packaged_json
from musicdiscovery.preference import Preference
from musicdiscovery.ranking import (
    BaselineRanker,
    Recommendation,
    _allowed,
    _Fit,
    quality_terms,
)

MODEL_NAME = "profile_ranker.json"
FEATURES = ("audio", "metadata", "text", "popularity")
TEXTS = {
    "audio": "similar sound to your listening",
    "metadata": "similar genres and tags",
    "text": "similar titles, artists and tags",
    "popularity": "popular with listeners",
}
TEXT_COLUMNS = ("title", "artist", "album", "genre_top")
_WORD = re.compile(r"[^\W_]{2,}")


class ProfileRankerError(Exception):
    """Raised when a profile cannot be built or the model file cannot be used."""


@dataclass(frozen=True)
class ProfileModel:
    """The scoring head's weights, fixed in `profile_ranker.json`.

    `scripts/train_profile_ranker.py` reproduces them. A track's score is the
    dot product of its features with `weights`; `bias` only shifts the
    probability and does not change the order. `training` records how the
    weights were made and is not used at rank time.
    """

    weights: dict[str, float]
    bias: float = 0.0
    training: dict = field(default_factory=dict, compare=False)

    def __post_init__(self):
        if set(self.weights) != set(FEATURES):
            raise ProfileRankerError(
                f"model weights must name exactly {list(FEATURES)}, "
                f"got {sorted(self.weights)}"
            )
        if not all(np.isfinite(v) for v in (*self.weights.values(), self.bias)):
            raise ProfileRankerError("model weights must be finite numbers")

    def vector(self) -> np.ndarray:
        return np.array([self.weights[name] for name in FEATURES])

    @classmethod
    def load(cls, path: Path | None = None) -> "ProfileModel":
        """Read a model file; with no path, the one shipped in the package."""
        try:
            payload = load_packaged_json(MODEL_NAME, path)
            return cls(
                weights={k: float(v) for k, v in payload["weights"].items()},
                bias=float(payload.get("bias", 0.0)),
                training=dict(payload.get("training", {})),
            )
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ProfileRankerError(f"cannot read profile model: {error!r}") from error

    def dump(self) -> str:
        payload = {
            "features": list(FEATURES),
            "weights": {name: round(self.weights[name], 6) for name in FEATURES},
            "bias": round(self.bias, 6),
            "training": self.training,
        }
        return json.dumps(payload, indent=2) + "\n"


def words(value: object) -> set[str]:
    """Lower-case words of two or more letters or digits; nothing for missing values."""
    if not isinstance(value, str):
        return set()
    return set(_WORD.findall(value.lower()))


def text_documents(catalog: Catalog) -> list[set[str]]:
    """Each track's words from its title, artist, album, top genre and tags."""
    frame = catalog.frame
    fields = [frame[c].tolist() for c in TEXT_COLUMNS if c in frame]
    fields.append([" ".join(tags) for tags in frame["tags"]])
    return [
        words(" ".join(v for v in values if isinstance(v, str)))
        for values in zip(*fields, strict=True)
    ]


def weighted_centre(vectors: np.ndarray, weights: Sequence[float]) -> np.ndarray:
    """The strength-weighted mean of history vectors (one row per track)."""
    w = np.asarray(weights, dtype=np.float64)
    if not len(vectors):
        raise ProfileRankerError("a profile needs at least one history track")
    if len(w) != len(vectors):
        raise ProfileRankerError(
            f"got {len(w)} weights for {len(vectors)} history tracks"
        )
    if not np.all(w > 0):
        raise ProfileRankerError("history weights must be positive")
    return w @ vectors / w.sum()


class Prepared:
    """Everything the features need that depends only on the catalog."""

    def __init__(self, catalog: Catalog, fit: _Fit):
        self.fit = fit
        self.metadata = _Metadata(catalog)
        self.text = TfIdf(text_documents(catalog))
        self.popularity = (
            catalog.frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
        )

    def features(self, seed_rows: list[int], weights: Sequence[float]) -> np.ndarray:
        """One row per catalog track, one column per name in `FEATURES`."""
        centre = weighted_centre(self.fit.projected[seed_rows], weights)
        norm = np.linalg.norm(centre)
        count = len(self.popularity)
        audio = self.fit.unit @ (centre / norm) if norm else np.zeros(count)
        return np.column_stack(
            [
                audio,
                self.metadata.similarity(seed_rows, weights),
                self.text.similarity(seed_rows, weights),
                self.popularity,
            ]
        )


class ProfileRanker:
    """Learned weights over profile features, plus the baseline quality terms."""

    def __init__(self, model: ProfileModel | None = None):
        self.model = model or ProfileModel.load()
        self._baseline = BaselineRanker()
        self._catalog: Catalog | None = None
        self._prepared: Prepared | None = None

    def prepare(self, catalog: Catalog) -> Prepared:
        if self._catalog is not catalog or self._prepared is None:
            self._prepared = Prepared(catalog, self._baseline._fitted(catalog))
            self._catalog = catalog
        return self._prepared

    def score(
        self, preference: Preference, catalog: Catalog
    ) -> tuple[list[tuple[str, str, np.ndarray]], np.ndarray, np.ndarray]:
        """The score terms, the total scores and the allowed rows in score order.

        Highest score first; ties go to the lower track id.
        """
        preference.validate(catalog)
        prepared = self.prepare(catalog)
        seed_rows = [catalog.position[s] for s in preference.seeds]
        features = prepared.features(seed_rows, preference.seed_weights())
        weights = self.model.vector()
        terms = [
            (name, TEXTS[name], weights[i] * features[:, i])
            for i, name in enumerate(FEATURES)
        ]
        terms += quality_terms(preference, catalog, prepared.fit)
        scores = np.sum([values for _, _, values in terms], axis=0)

        candidates = np.flatnonzero(_allowed(preference, catalog))
        order = np.lexsort((catalog.track_ids[candidates], -scores[candidates]))
        return terms, scores, candidates[order]

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        terms, _, ranked = self.score(preference, catalog)
        chosen = ranked[: preference.k]
        return [BaselineRanker.build(catalog, int(row), terms) for row in chosen]


class ProfileDiverseRanker(ProfileRanker):
    """The profile ranker's scores, re-ranked with the hybrid's MMR for variety.

    Uses `mmr_lambda`, `genre_redundancy` and `pool_size` from the hybrid
    config; it has no config or tuning of its own.
    """

    def __init__(
        self, model: ProfileModel | None = None, config: HybridConfig | None = None
    ):
        super().__init__(model)
        self.config = config or HybridConfig.load()
        self._keys: tuple[np.ndarray, np.ndarray] | None = None

    def prepare(self, catalog: Catalog) -> Prepared:
        if self._catalog is not catalog or self._keys is None:
            self._keys = mmr_keys(catalog)
        return super().prepare(catalog)

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        config = self.config
        terms, scores, ranked = self.score(preference, catalog)
        if not len(ranked):
            return []
        pool = ranked[: max(config.pool_size, preference.k)]
        artists, genres = self._keys
        picks, variety = mmr(
            pool,
            scores[pool],
            preference.k,
            config.mmr_lambda,
            config.genre_redundancy,
            artists,
            genres,
        )
        return with_variety(catalog, picks, variety, terms)
