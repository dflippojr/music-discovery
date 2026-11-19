"""Synthetic listener queries: generate them and read and write their file."""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from musicdiscovery import paths
from musicdiscovery.catalog import Catalog
from musicdiscovery.preference import AXES, Preference
from musicdiscovery.profiles import SPLITS
from musicdiscovery.rankers import EvaluationError

K = 10
QUERY_SEED = 20261005
QUERIES_PER_SPLIT = 100
DEFAULT_QUERIES = Path("eval") / "queries.json"


@dataclass(frozen=True)
class Query:
    """One synthetic listener: a seed track and some liked qualities."""

    id: int
    split: str
    seed: int
    likes: tuple[str, ...]

    def preference(self, k: int = K) -> Preference:
        return Preference.from_strings([self.seed], self.likes, k=k)


def generate_queries(
    catalog: Catalog,
    per_split: int = QUERIES_PER_SPLIT,
    seed: int = QUERY_SEED,
) -> list[Query]:
    """Sample seeds and likes with a fixed seed; one catalog gives one list."""
    rng = np.random.default_rng(seed)
    total = per_split * len(SPLITS)
    if total > len(catalog):
        raise EvaluationError(f"catalog of {len(catalog)} tracks is too small")
    rows = rng.choice(len(catalog), size=total, replace=False)
    axes = sorted(AXES)
    queries = []
    for index, row in enumerate(rows.tolist()):
        record = catalog.frame.iloc[row]
        likes: list[str] = []
        if rng.random() < 0.7:
            direction = "more" if rng.random() < 0.5 else "less"
            likes.append(f"{direction}:{axes[int(rng.integers(len(axes)))]}")
        if isinstance(record["genre_top"], str) and rng.random() < 0.5:
            likes.append(f"genre:{record['genre_top']}")
        tags = sorted(record["tags"])
        if tags and rng.random() < 0.5:
            likes.append(f"tag:{tags[int(rng.integers(len(tags)))]}")
        if not likes:
            likes.append(f"more:{axes[int(rng.integers(len(axes)))]}")
        queries.append(
            Query(
                id=index,
                split=SPLITS[index // per_split],
                seed=int(catalog.track_ids[row]),
                likes=tuple(likes),
            )
        )
    return queries


def _recorded_size(path: Path) -> int | None:
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("catalog_tracks")
    except (OSError, ValueError, AttributeError):
        return None


def refuse_other_catalog(path: Path, catalog: Catalog, what: str) -> None:
    """Refuse to replace a file made for a different catalog size.

    `what` names the file's contents in the message, for example `queries`.
    """
    recorded = _recorded_size(path) if path.exists() else None
    if recorded is not None and recorded != len(catalog):
        raise EvaluationError(
            f"{path} was made for a catalog of {recorded} tracks, this one has "
            f"{len(catalog)}; remove it to generate {what} for this catalog"
        )


def write_queries(path: Path, queries: Sequence[Query], catalog: Catalog) -> None:
    """Write queries; refuse to replace a file made for a different catalog size."""
    path = paths.confined(path, EvaluationError)
    refuse_other_catalog(path, catalog, "queries")
    payload = {
        "query_seed": QUERY_SEED,
        "k": K,
        "catalog_tracks": len(catalog),
        "queries": [asdict(q) for q in queries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def _likes(query: dict, path: Path) -> tuple[str, ...]:
    likes = query["likes"]
    if not isinstance(likes, list) or not all(isinstance(text, str) for text in likes):
        raise EvaluationError(
            f"cannot read queries at {path}: query {query['id']} "
            "likes must be a list of strings"
        )
    return tuple(likes)


def load_queries(path: Path, catalog: Catalog) -> list[Query]:
    path = paths.confined(path, EvaluationError)
    if not path.exists():
        raise EvaluationError(
            f"no queries at {path}; create them with `musicdiscovery evaluate "
            "--generate-queries`"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        queries = [
            Query(int(q["id"]), str(q["split"]), int(q["seed"]), _likes(q, path))
            for q in payload["queries"]
        ]
        expected = payload.get("catalog_tracks")
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        raise EvaluationError(f"cannot read queries at {path}: {error!r}") from error
    if expected is not None and expected != len(catalog):
        raise EvaluationError(
            f"queries were made for a catalog of {expected} tracks, "
            f"this one has {len(catalog)}"
        )
    for split in SPLITS:
        if not any(q.split == split for q in queries):
            raise EvaluationError(f"queries have no {split} split")
    if any(q.split not in SPLITS for q in queries):
        raise EvaluationError(f"query splits must be one of {SPLITS}")
    return queries
