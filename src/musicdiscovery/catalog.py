"""The ingested catalog, wrapped for ranking."""

import numbers
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd
from pyarrow import ArrowException

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


_LOADED_COLUMNS = {"track_id", "artist", "genre_top", "genre_ids", "tags", "listens"}


class CatalogError(Exception):
    """Raised when the catalog is missing or malformed."""


def _named(columns: list[str]) -> str:
    """Name a few columns: a damaged file can have hundreds of bad ones."""
    more = f" and {len(columns) - 5} more" if len(columns) > 5 else ""
    return ", ".join(columns[:5]) + more


def _is_list_column(column: pd.Series, item: type) -> bool:
    return all(
        isinstance(cell, list | tuple | np.ndarray)
        and all(isinstance(value, item) for value in cell)
        for cell in column
    )


def _check_usable(frame: pd.DataFrame) -> None:
    """Refuse a file that rankers could not use; `ingest` never writes one."""
    missing = sorted(_LOADED_COLUMNS - set(frame.columns))
    if missing:
        raise CatalogError(f"catalog is missing columns: {_named(missing)}")
    track_ids = frame["track_id"]
    if not pd.api.types.is_integer_dtype(track_ids) or track_ids.isna().any():
        raise CatalogError("catalog column track_id has missing or non-integer ids")
    if not _is_list_column(frame["tags"], str):
        raise CatalogError("catalog column tags has cells that are not lists of text")
    if not _is_list_column(frame["genre_ids"], numbers.Integral):
        raise CatalogError(
            "catalog column genre_ids has cells that are not lists of integers"
        )
    numeric = ["listens"] + [c for c in frame.columns if c not in METADATA_COLUMNS]
    bad = [c for c in numeric if not pd.api.types.is_numeric_dtype(frame[c])]
    if bad:
        raise CatalogError(f"catalog has non-numeric columns: {_named(bad)}")
    values = frame[numeric].to_numpy(dtype=np.float64, na_value=np.nan)
    finite = np.isfinite(values).all(axis=0)
    bad = [c for c, ok in zip(numeric, finite, strict=True) if not ok]
    if bad:
        raise CatalogError(f"catalog has non-finite values in: {_named(bad)}")


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
        try:
            frame = pd.read_parquet(path)
        except (OSError, ValueError, ArrowException) as error:
            raise CatalogError(
                f"could not read catalog at {path}; "
                f"run `musicdiscovery ingest`: {error}"
            ) from error
        try:
            _check_usable(frame)
            return cls(frame)
        except CatalogError as error:
            raise CatalogError(f"{error}; run `musicdiscovery ingest`") from error

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

    @cached_property
    def popularity(self) -> np.ndarray:
        return self.frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)

    def has_tag(self, tag: str) -> np.ndarray:
        return np.fromiter(
            (tag in row for row in self.frame["tags"]), dtype=bool, count=len(self)
        )

    def has_genre(self, genre: str) -> np.ndarray:
        matches = self.frame["genre_top"].astype("string") == genre
        return matches.fillna(False).to_numpy(dtype=bool)
