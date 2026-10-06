"""Content-based baseline ranker and the interface other rankers share."""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.preference import AXES, Preference

PCA_DIMENSIONS = 20
SIMILARITY_WEIGHT = 1.0
AXIS_WEIGHT = 0.05  # per standard deviation, z clipped to +/-3
GENRE_WEIGHT = 0.15
TAG_WEIGHT = 0.10
_Z_CLIP = 3.0
_FEATURE_CLIP = 5.0
_MAX_EXPLANATIONS = 3


@dataclass(frozen=True)
class ScoreTerm:
    """One additive part of a score, with the words that describe it."""

    name: str
    value: float
    text: str


@dataclass(frozen=True)
class Recommendation:
    track_id: int
    title: str
    artist: str
    score: float
    terms: tuple[ScoreTerm, ...]
    explanations: tuple[str, ...]


class Ranker(Protocol):
    """Anything that turns a preference and a catalog into ranked tracks."""

    def rank(
        self, preference: Preference, catalog: Catalog
    ) -> list[Recommendation]: ...


class _Fit:
    """Standardised features and their PCA projection, fit on one catalog."""

    def __init__(self, catalog: Catalog, dimensions: int):
        x = catalog.features
        mean = x.mean(axis=0)
        std = x.std(axis=0)
        std[std == 0] = 1.0
        self.z = np.clip((x - mean) / std, -_FEATURE_CLIP, _FEATURE_CLIP)
        self.columns = {c: i for i, c in enumerate(catalog.feature_columns)}
        dims = min(dimensions, *self.z.shape)
        if dims > 0:
            cov = self.z.T @ self.z / max(len(self.z) - 1, 1)
            values, vectors = np.linalg.eigh(cov)
            basis = vectors[:, np.argsort(values)[::-1][:dims]]
            flip = np.sign(basis[np.abs(basis).argmax(axis=0), range(dims)])
            flip[flip == 0] = 1.0
            self.projected = self.z @ (basis * flip)
        else:
            self.projected = self.z
        norms = np.linalg.norm(self.projected, axis=1)
        norms[norms == 0] = 1.0
        self.unit = self.projected / norms[:, None]


class BaselineRanker:
    """Cosine similarity to the seeds, plus fixed-weight axis, genre and tag terms."""

    def __init__(self, dimensions: int = PCA_DIMENSIONS):
        self.dimensions = dimensions
        self._fit_catalog: Catalog | None = None
        self._fit: _Fit | None = None

    def _fitted(self, catalog: Catalog) -> _Fit:
        if self._fit is None or self._fit_catalog is not catalog:
            self._fit = _Fit(catalog, self.dimensions)
            self._fit_catalog = catalog
        return self._fit

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        fit = self._fitted(catalog)
        seed_rows = [catalog.position[s] for s in preference.seeds]

        centre = fit.projected[seed_rows].mean(axis=0)
        norm = np.linalg.norm(centre)
        similarity = fit.unit @ (centre / norm) if norm else np.zeros(len(catalog))
        terms: list[tuple[str, str, np.ndarray]] = [
            ("similarity", "similar sound to the seed", SIMILARITY_WEIGHT * similarity)
        ]

        for quality, sign in preference.signed_qualities():
            if quality.kind == "axis":
                axis = AXES[quality.name]
                z = np.clip(fit.z[:, fit.columns[axis.column]], -_Z_CLIP, _Z_CLIP)
                up = quality.direction > 0
                terms.append(
                    (
                        f"axis:{quality.name}{'+' if up else '-'}",
                        f"{axis.up if up else axis.down}, as asked",
                        AXIS_WEIGHT * quality.direction * z,
                    )
                )
            elif quality.kind == "genre":
                terms.append(
                    (
                        f"genre:{quality.name}",
                        f"same genre: {quality.name}",
                        sign * GENRE_WEIGHT * catalog.has_genre(quality.name),
                    )
                )
            else:
                terms.append(
                    (
                        f"tag:{quality.name}",
                        f"tagged {quality.name}",
                        sign * TAG_WEIGHT * catalog.has_tag(quality.name),
                    )
                )

        scores = np.sum([values for _, _, values in terms], axis=0)
        allowed = np.ones(len(catalog), dtype=bool)
        allowed[seed_rows] = False
        if preference.exclude_seed_artist:
            artists = catalog.frame["artist"]
            allowed &= ~artists.isin(artists.iloc[seed_rows]).to_numpy()
        candidates = np.flatnonzero(allowed)
        # Highest score first; ties go to the lower track id.
        order = np.lexsort((catalog.track_ids[candidates], -scores[candidates]))
        chosen = candidates[order[: preference.k]]
        return [self._build(catalog, int(row), terms) for row in chosen]

    @staticmethod
    def _build(catalog: Catalog, row: int, terms) -> Recommendation:
        parts = tuple(
            ScoreTerm(name, float(values[row]), text) for name, text, values in terms
        )
        ordered = sorted(parts, key=lambda t: -t.value)
        explained = [t for t in ordered if t.value > 0][:_MAX_EXPLANATIONS]
        if not explained:
            explained = ordered[:1]
        record = catalog.frame.iloc[row]
        return Recommendation(
            track_id=int(record["track_id"]),
            title=str(record.get("title", "")),
            artist=str(record["artist"]),
            score=float(sum(t.value for t in parts)),
            terms=parts,
            explanations=tuple(t.text for t in explained),
        )
