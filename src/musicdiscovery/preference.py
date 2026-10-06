"""Listener preference input, validated against the catalog."""

from collections.abc import Iterable
from dataclasses import dataclass

from musicdiscovery.catalog import Catalog

DEFAULT_K = 10


@dataclass(frozen=True)
class Axis:
    """A named feature axis: one catalog column and how to describe each way."""

    column: str
    up: str
    down: str


# The small axis-to-feature table; documented in docs/ranking.md.
AXES: dict[str, Axis] = {
    "energy": Axis("rmse_mean_01", "more energetic", "calmer"),
    "brightness": Axis("spectral_centroid_mean_01", "brighter", "darker"),
    "noisiness": Axis("zcr_mean_01", "noisier", "cleaner"),
}


class PreferenceError(ValueError):
    """Raised when a preference cannot be used."""


@dataclass(frozen=True)
class Quality:
    """One liked or disliked quality: a genre, a tag, or a direction on an axis."""

    kind: str  # "genre", "tag" or "axis"
    name: str
    direction: int = 1  # axis only: +1 up, -1 down

    @classmethod
    def parse(cls, text: str) -> "Quality":
        """Parse ``genre:Folk``, ``tag:punk``, ``more:energy`` or ``less:energy``."""
        head, sep, name = text.partition(":")
        head, name = head.strip().lower(), name.strip()
        if not sep or not name:
            raise PreferenceError(
                f"cannot read quality {text!r}; use genre:<name>, tag:<name>, "
                "more:<axis> or less:<axis>"
            )
        if head in ("genre", "tag"):
            return cls(head, name)
        if head in ("more", "less"):
            axis = name.lower()
            if axis not in AXES:
                raise PreferenceError(
                    f"unknown axis {name!r}; choose from {sorted(AXES)}"
                )
            return cls("axis", axis, 1 if head == "more" else -1)
        raise PreferenceError(f"unknown quality kind {head!r} in {text!r}")

    def flipped(self) -> "Quality":
        return Quality(self.kind, self.name, -self.direction)


@dataclass(frozen=True)
class Preference:
    """Seeds, liked and disliked qualities, result count and seed-artist option."""

    seeds: tuple[int, ...]
    likes: tuple[Quality, ...] = ()
    dislikes: tuple[Quality, ...] = ()
    k: int = DEFAULT_K
    exclude_seed_artist: bool = False

    def __post_init__(self):
        if not self.seeds:
            raise PreferenceError("at least one seed track id is required")
        if self.k < 1:
            raise PreferenceError(f"k must be at least 1, got {self.k}")

    @classmethod
    def from_strings(
        cls,
        seeds: Iterable[int],
        likes: Iterable[str] = (),
        dislikes: Iterable[str] = (),
        k: int = DEFAULT_K,
        exclude_seed_artist: bool = False,
    ) -> "Preference":
        return cls(
            tuple(int(s) for s in seeds),
            tuple(Quality.parse(q) for q in likes),
            tuple(Quality.parse(q) for q in dislikes),
            k,
            exclude_seed_artist,
        )

    def validate(self, catalog: Catalog) -> None:
        """Reject seeds, genres and tags that the catalog does not contain."""
        unknown = [s for s in self.seeds if s not in catalog.position]
        if unknown:
            raise PreferenceError(f"unknown track ids: {unknown}")
        for quality in (*self.likes, *self.dislikes):
            if quality.kind == "genre" and quality.name not in catalog.genres:
                raise PreferenceError(f"unknown genre {quality.name!r}")
            if quality.kind == "tag" and quality.name not in catalog.tags:
                raise PreferenceError(f"unknown tag {quality.name!r}")
            if quality.kind == "axis":
                column = AXES[quality.name].column
                if column not in catalog.feature_columns:
                    raise PreferenceError(
                        f"catalog has no column {column!r} for axis {quality.name!r}"
                    )

    def signed_qualities(self) -> list[tuple[Quality, int]]:
        """Likes as +1 and dislikes as -1; an axis dislike becomes a flipped like."""
        signed = [(q, 1) for q in self.likes]
        for quality in self.dislikes:
            if quality.kind == "axis":
                signed.append((quality.flipped(), 1))
            else:
                signed.append((quality, -1))
        return signed
