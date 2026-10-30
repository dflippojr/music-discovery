"""Write the aggregate shape of an ingested catalog, for shape-matched test data.

Usage: python scripts/profile_catalog.py [--data-dir data] [--vectors PATH]
                                         [--out tests/fixtures/catalog_shape.json]

The profile holds aggregate statistics only: shares, histograms, quantiles and
the feature column schema. It never holds a track id, title, artist, album, tag
or any other row-level value, so it can be committed. `tests/catalog_factory.py`
turns it back into synthetic catalogs. See docs/evaluation.md for the refresh.
"""

import argparse
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "catalog_shape.json"
PROFILE_VERSION = 1

SCALAR_FIELDS = (
    "title",
    "artist",
    "album",
    "genre_top",
    "duration",
    "listens",
    "license_url",
    "source_url",
)
LIST_FIELDS = ("genre_ids", "tags")
QUANTILES = (0.0, 0.01, 0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0)
MAX_BUCKET = 10  # list lengths of 10 or more share the "10+" bucket
# Scripts a title can contain, checked in this order; see `title_scripts`.
SCRIPTS = ("cjk", "cyrillic", "arabic", "hebrew", "greek", "emoji", "latin", "other")
_CJK = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "IDEOGRAPHIC")


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _missing(value: object) -> bool:
    if isinstance(value, list | tuple | np.ndarray):
        return False
    return bool(pd.isna(value)) or (isinstance(value, str) and not value.strip())


def _quantiles(values) -> dict[str, float]:
    values = np.asarray(list(values), dtype=np.float64)
    if not len(values):
        return {}
    return {f"{q:g}": round(float(np.quantile(values, q)), 2) for q in QUANTILES}


def _histogram(lengths) -> dict[str, float]:
    counts = Counter(min(int(n), MAX_BUCKET) for n in lengths)
    total = sum(counts.values())
    label = {MAX_BUCKET: f"{MAX_BUCKET}+"}
    return {label.get(n, str(n)): _share(counts[n], total) for n in sorted(counts)}


def char_script(char: str) -> str | None:
    """The script a non-ASCII character belongs to, or None for ASCII."""
    if ord(char) < 128:
        return None
    if unicodedata.category(char) == "So" or ord(char) >= 0x1F000:
        return "emoji"
    name = unicodedata.name(char, "")
    if name.startswith(_CJK):
        return "cjk"
    for script in ("cyrillic", "arabic", "hebrew", "greek", "latin"):
        if name.startswith(script.upper()):
            return script
    return "other"


def title_scripts(title: str) -> set[str]:
    return {s for s in map(char_script, title) if s is not None}


def is_rtl(title: str) -> bool:
    return any(unicodedata.bidirectional(c) in ("R", "AL") for c in title)


def _text_shape(values: pd.Series) -> dict:
    texts = [str(v) for v in values if not _missing(v)]
    total = len(texts)
    scripts = Counter(s for t in texts for s in title_scripts(t))
    counts = Counter(texts)
    return {
        "non_ascii": _share(sum(1 for t in texts if not t.isascii()), total),
        "rtl": _share(sum(1 for t in texts if is_rtl(t)), total),
        "scripts": {s: _share(scripts[s], total) for s in SCRIPTS},
        "length_quantiles": _quantiles(len(t) for t in texts),
        "duplicate_rate": _share(sum(n for n in counts.values() if n > 1), total),
        "distinct_per_track": _share(len(counts), total),
    }


def _feature_schema(columns: list[str]) -> dict:
    """`<feature>_<statistic>_<nn>` names as statistics and coefficients per feature."""
    stats: dict[str, Counter] = {}
    for column in columns:
        feature, stat, _number = column.rsplit("_", 2)
        stats.setdefault(feature, Counter())[stat] += 1
    statistics = sorted({s for counts in stats.values() for s in counts})
    coefficients = {}
    for feature, counts in sorted(stats.items()):
        if set(counts) != set(statistics) or len(set(counts.values())) != 1:
            raise ValueError(f"feature {feature} does not have every statistic once")
        coefficients[feature] = next(iter(counts.values()))
    return {"statistics": statistics, "coefficients": coefficients}


def profile(frame: pd.DataFrame, vector_ids=None, source: dict | None = None) -> dict:
    """Aggregate shape of a catalog frame; `vector_ids` are the embedded tracks."""
    from musicdiscovery.catalog import METADATA_COLUMNS

    total = len(frame)
    missing = {}
    for name in SCALAR_FIELDS:
        column = frame[name] if name in frame else pd.Series([None] * total)
        missing[name] = _share(int(column.map(_missing).sum()), total)
    for name in LIST_FIELDS:
        missing[name] = _share(int(frame[name].map(len).eq(0).sum()), total)

    features = [c for c in frame.columns if c not in METADATA_COLUMNS]
    genres = frame["genre_top"].dropna().astype(str)
    tag_counts = Counter(t for row in frame["tags"] for t in row)
    artists = frame["artist"].dropna().astype(str)
    no_vector = None
    if vector_ids is not None:
        embedded = set(int(t) for t in vector_ids)
        no_vector = _share(
            sum(int(t) not in embedded for t in frame["track_id"]), total
        )
    return {
        "profile_version": PROFILE_VERSION,
        "source": source or {},
        "tracks": total,
        "missing": missing,
        "genres": {g: _share(n, total) for g, n in sorted(Counter(genres).items())},
        "genre_id_count": _histogram(frame["genre_ids"].map(len)),
        "tag_count": _histogram(frame["tags"].map(len)),
        "distinct_tags_per_track": _share(len(tag_counts), total),
        "tracks_per_artist_quantiles": _quantiles(Counter(artists).values()),
        "title": _text_shape(frame["title"]),
        "artist": _text_shape(frame["artist"]),
        "duration_quantiles": _quantiles(frame["duration"].dropna()),
        "listens_quantiles": _quantiles(frame["listens"].dropna()),
        "audio": {
            "without_features": _share(
                int(frame[features].isna().any(axis=1).sum()), total
            ),
            "without_vector": no_vector,
        },
        "features": _feature_schema(features),
    }


def _source(data_dir: Path) -> dict:
    manifest = data_dir / "catalog" / "catalog.json"
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {
        "subset": saved.get("subset"),
        "source_sha1": saved.get("source_sha1"),
        "ingest_version": saved.get("ingest_version"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--vectors",
        type=Path,
        help="audio vector cache (default: the embed-audio cache, if present)",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    from musicdiscovery.audio_embedding import (
        DEFAULT_VECTORS,
        AudioEmbeddingError,
        VectorStore,
    )
    from musicdiscovery.catalog import Catalog, CatalogError

    try:
        catalog = Catalog.load(args.data_dir / "catalog" / "catalog.parquet")
    except CatalogError as error:
        print(f"profile_catalog: {error}", file=sys.stderr)
        return 1
    vectors = args.vectors or args.data_dir / DEFAULT_VECTORS.relative_to("data")
    vector_ids = None
    if vectors.exists():
        try:
            vector_ids = VectorStore.load(vectors).track_ids
        except AudioEmbeddingError as error:
            print(f"profile_catalog: {error}", file=sys.stderr)
            return 1
    elif args.vectors:
        print(f"profile_catalog: no vector cache at {vectors}", file=sys.stderr)
        return 1
    shape = profile(catalog.frame, vector_ids, _source(args.data_dir))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(shape, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"profile_catalog: wrote the shape of {len(catalog)} tracks to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
