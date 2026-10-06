"""Export the static demo bundle: page files plus data both rankers need.

The browser ranks from this bundle alone. Feature vectors are quantised to
16-bit integers; `parity.json` holds what the Python rankers return on fixed
queries, and a Node test checks the JavaScript port against it.
"""

import base64
import gzip
import json
import shutil
from collections import Counter
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

from musicdiscovery import ranking
from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation import confined, generate_queries, make_ranker
from musicdiscovery.hybrid import VARIETY_TEXT, HybridConfig, _Metadata
from musicdiscovery.preference import AXES, Preference

FORMAT_VERSION = 1
CATALOG_FILE = "catalog.json"
PARITY_FILE = "parity.json"
STATIC_FILES = ("index.html", "demo.css", "demo.js", "ranker.js")
TARGET_BYTES = 2 * 1024 * 1024  # compressed, for the small catalog
TOP_TAGS = 40
PARITY_QUERIES_PER_SPLIT = 12
ATTRIBUTION = (
    "Track metadata and audio features come from the Free Music Archive dataset "
    "(FMA), Defferrard et al., ISMIR 2017, licensed CC BY 4.0."
)
_INT16_MAX = 32767
_UINT16_MAX = 65535


class DemoError(Exception):
    """Raised when the demo bundle cannot be written."""


@dataclass(frozen=True)
class BundleSize:
    files: dict[str, tuple[int, int]]  # name -> (raw bytes, gzip bytes)

    @property
    def raw(self) -> int:
        return sum(raw for raw, _ in self.files.values())

    @property
    def compressed(self) -> int:
        return sum(packed for _, packed in self.files.values())


def _blob(array: np.ndarray, dtype: str) -> str:
    return base64.b64encode(array.astype(dtype, copy=False).tobytes()).decode("ascii")


def _quantise(values: np.ndarray, limit: float) -> tuple[np.ndarray, float]:
    """Round to int16 so that integer * scale recovers the value."""
    scale = float(limit) / _INT16_MAX if limit else 1.0
    return np.round(values / scale).astype("<i2"), scale


def _text(frame, column: str) -> list[str]:
    if column not in frame.columns:
        return [""] * len(frame)
    return ["" if pd.isna(v) else str(v) for v in frame[column]]


def _table(values: list[str]) -> tuple[list[str], list[int]]:
    """A de-duplicated table of strings and each row's index into it."""
    table = sorted(set(values))
    index = {value: i for i, value in enumerate(table)}
    return table, [index[v] for v in values]


def build_catalog(catalog: Catalog) -> dict:
    """Everything the browser needs to run both rankers on this catalog."""
    frame = catalog.frame
    fit = ranking.BaselineRanker()._fitted(catalog)
    config = HybridConfig.load()
    metadata = _Metadata(catalog)
    if metadata.size > _UINT16_MAX:
        raise DemoError("metadata vocabulary is too large for 16-bit token ids")

    projected, projected_scale = _quantise(fit.projected, np.abs(fit.projected).max())
    axis_names = sorted(AXES)
    clip = ranking._Z_CLIP
    z = np.stack(
        [
            np.clip(fit.z[:, fit.columns[AXES[a].column]], -clip, clip)
            for a in axis_names
        ],
        axis=1,
    )
    axis_data, axis_scale = _quantise(z, clip)

    popularity = frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
    artists, artist_index = _table(_text(frame, "artist"))
    genres = sorted(catalog.genres)
    genre_names = frame["genre_top"].astype("string").fillna("").tolist()
    licenses, license_index = _table(_text(frame, "license_url"))
    tag_counts = Counter(t for row in frame["tags"] for t in set(row))
    top_tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_TAGS]

    return {
        "version": FORMAT_VERSION,
        "count": len(catalog),
        "attribution": ATTRIBUTION,
        "ids": [int(t) for t in catalog.track_ids],
        "titles": _text(frame, "title"),
        "artists": artists,
        "artist": artist_index,
        "genres": genres,
        "genre": [genres.index(g) if g in genres else -1 for g in genre_names],
        "licenses": licenses,
        "license": license_index,
        "sources": _text(frame, "source_url"),
        "chips": {"genres": genres, "tags": [t for t, _ in top_tags]},
        "axes": [
            {"name": a, "up": AXES[a].up, "down": AXES[a].down} for a in axis_names
        ],
        "baseline": {
            "dims": int(projected.shape[1]),
            "projected": _blob(projected, "<i2"),
            "projectedScale": projected_scale,
            "axisData": _blob(axis_data, "<i2"),
            "axisScale": axis_scale,
            "similarityWeight": ranking.SIMILARITY_WEIGHT,
            "axisWeight": ranking.AXIS_WEIGHT,
            "genreWeight": ranking.GENRE_WEIGHT,
            "tagWeight": ranking.TAG_WEIGHT,
            "maxExplanations": ranking._MAX_EXPLANATIONS,
        },
        "hybrid": {
            "tokens": metadata.tokens,
            "counts": _blob(np.bincount(metadata.rows, minlength=len(catalog)), "<u2"),
            "columns": _blob(metadata.columns, "<u2"),
            "values": _blob(np.round(metadata.values * _UINT16_MAX), "<u2"),
            "valueScale": 1 / _UINT16_MAX,
            "popularity": _blob(np.round(popularity * _UINT16_MAX), "<u2"),
            "popularityScale": 1 / _UINT16_MAX,
            "metadataWeight": config.metadata_weight,
            "popularityWeight": config.popularity_weight,
            "mmrLambda": config.mmr_lambda,
            "genreRedundancy": config.genre_redundancy,
            "poolSize": config.pool_size,
            "varietyText": VARIETY_TEXT,
        },
    }


def parity_queries(catalog: Catalog) -> list[dict]:
    """Fixed queries: the seeded evaluation sampler plus a dislike on some."""
    queries = generate_queries(catalog, per_split=PARITY_QUERIES_PER_SPLIT)
    axes = sorted(AXES)
    genres = sorted(catalog.genres)
    out = []
    for q in queries:
        dislikes: list[str] = []
        if q.id % 3 == 1 and genres:
            dislikes.append(f"genre:{genres[q.id % len(genres)]}")
        elif q.id % 3 == 2:
            dislikes.append(f"more:{axes[q.id % len(axes)]}")
        out.append({"seed": q.seed, "likes": list(q.likes), "dislikes": dislikes})
    return out


def build_parity(catalog: Catalog) -> dict:
    """What the Python rankers return for each parity query."""
    rankers = {name: make_ranker(name) for name in ("baseline", "hybrid")}
    queries = []
    for query in parity_queries(catalog):
        preference = Preference.from_strings(
            [query["seed"]], query["likes"], query["dislikes"]
        )
        expected = {
            name: [r.track_id for r in ranker.rank(preference, catalog)]
            for name, ranker in rankers.items()
        }
        queries.append({**query, "expected": expected})
    return {"version": FORMAT_VERSION, "queries": queries}


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def measure(out_dir: Path) -> BundleSize:
    """Raw and gzip sizes of the files a site needs (parity data excluded)."""
    sizes = {}
    for name in (*STATIC_FILES, CATALOG_FILE):
        raw = (out_dir / name).read_bytes()
        sizes[name] = (len(raw), len(gzip.compress(raw, 9)))
    return BundleSize(sizes)


def export_demo(catalog: Catalog, out_dir: Path) -> BundleSize:
    """Write the bundle into `out_dir` (inside the working directory)."""
    out_dir = confined(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    source = resources.files("musicdiscovery").joinpath("demo")
    for name in STATIC_FILES:
        with resources.as_file(source.joinpath(name)) as path:
            shutil.copyfile(path, out_dir / name)
    _write_json(out_dir / CATALOG_FILE, build_catalog(catalog))
    _write_json(out_dir / PARITY_FILE, build_parity(catalog))
    return measure(out_dir)
