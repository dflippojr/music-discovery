"""The ingested catalog, wrapped for ranking."""

from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd

METADATA_COLUMNS = (
    "track_id",
    "title",
    "artist",
    "album",
    "genre_top",
    "genre_ids",
    "tags",
    "duration",
    "listens",
    "license_url",
    "source_url",
)


class CatalogError(Exception):
    """Raised when the catalog is missing or malformed."""


class Catalog:
    """Tracks with metadata and numeric feature columns, sorted by track id."""

    def __init__(self, frame: pd.DataFrame):
        missing = {"track_id", "artist", "genre_top", "tags"} - set(frame.columns)
        if missing:
            raise CatalogError(f"catalog is missing columns: {sorted(missing)}")
        if frame["track_id"].duplicated().any():
            raise CatalogError("catalog has duplicate track ids")
        self.frame = frame.sort_values("track_id").reset_index(drop=True)
        self.feature_columns = [
            c for c in self.frame.columns if c not in METADATA_COLUMNS
        ]
        if not self.feature_columns:
            raise CatalogError("catalog has no feature columns")

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        if not path.exists():
            raise CatalogError(f"no catalog at {path}; run `musicdiscovery ingest`")
        return cls(pd.read_parquet(path))

    def __len__(self) -> int:
        return len(self.frame)

    @cached_property
    def track_ids(self) -> np.ndarray:
        return self.frame["track_id"].to_numpy()

    @cached_property
    def position(self) -> dict[int, int]:
        return {int(t): i for i, t in enumerate(self.track_ids)}

    @cached_property
    def genres(self) -> frozenset[str]:
        return frozenset(self.frame["genre_top"].dropna().astype(str))

    @cached_property
    def tags(self) -> frozenset[str]:
        return frozenset(t for row in self.frame["tags"] for t in row)

    @cached_property
    def features(self) -> np.ndarray:
        return self.frame[self.feature_columns].to_numpy(dtype=np.float64)

    def has_tag(self, tag: str) -> np.ndarray:
        return np.fromiter(
            (tag in row for row in self.frame["tags"]), dtype=bool, count=len(self)
        )

    def has_genre(self, genre: str) -> np.ndarray:
        matches = self.frame["genre_top"].astype("string") == genre
        return matches.fillna(False).to_numpy(dtype=bool)
