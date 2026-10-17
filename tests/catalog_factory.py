"""Synthetic catalogs shaped like the real FMA catalog, from its aggregate profile.

`tests/fixtures/catalog_shape.json` is written by `scripts/profile_catalog.py`
from a local ingest and holds shares, histograms and quantiles only. Every
value made here is synthetic: titles, artists, tags and ids are generated, and
only their distributions follow the profile.

Measured shares are kept, with one exception: every edge case the code claims
to handle appears at least once, even when today's ingest has none (it has no
track without a top genre, for example). A share of zero is a property of one
ingest, not a promise, and a defect like #89 must still fail on this catalog.
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd

from musicdiscovery.audio_embedding import VectorStore

SHAPE_PATH = Path(__file__).parent / "fixtures" / "catalog_shape.json"
# Fields the catalog code treats as optional; each is missing on at least one row.
EDGE_FIELDS = ("title", "album", "genre_top", "genre_ids", "tags")
EXTRA_GENRE_IDS = 40  # synthetic sub-genre ids after the top-genre ids
VECTOR_DIMENSIONS = 16
_FIRST_ID = 2
_ID_SPAN = 160_000
_LARGEST_BUCKET = 15  # list length drawn for the "10+" bucket, at most

# Characters per script, chosen so scripts/profile_catalog.py classifies them back.
ALPHABETS = {
    "ascii": "abcdefghijklmnopqrstuvwxyz",
    "latin": "àáâäãåçèéêëìíîïñòóôöøùúûüýœß",
    "cjk": "".join(map(chr, range(0x4E00, 0x4E40))) + "あいうえおかきくけこ",
    "cyrillic": "".join(map(chr, range(0x0430, 0x0450))),
    "arabic": "".join(map(chr, range(0x0627, 0x063B))),
    "hebrew": "".join(map(chr, range(0x05D0, 0x05EB))),
    "greek": "".join(map(chr, range(0x03B1, 0x03CA))),
    "emoji": "🎵🎶🔥🌙🌊🎸🥁🎹✨💜",
    "other": "".join(map(chr, range(0x0915, 0x0939))),  # Devanagari
}


def load_shape(path: Path = SHAPE_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _count(share: float | None, n: int, edge: bool = False) -> int:
    """Rows out of `n` for a share; at least one when it occurs or is an edge case."""
    share = share or 0.0
    count = int(round(share * n))
    return min(n, max(count, 1 if share > 0 or edge else 0))


def _quantile_sampler(quantiles: dict[str, float], rng: np.random.Generator):
    """Draw from the piecewise-linear inverse CDF through the profile's quantiles."""
    points = sorted((float(q), v) for q, v in quantiles.items())
    probs, values = zip(*points, strict=True)
    return lambda size: np.interp(rng.random(size), probs, values)


def _buckets(histogram: dict[str, float], n: int, rng: np.random.Generator):
    """List lengths drawn from a "0", "1", ..., "10+" share histogram."""
    labels = list(histogram)
    weights = np.array([histogram[label] for label in labels], dtype=np.float64)
    picks = rng.choice(len(labels), size=n, p=weights / weights.sum())
    lengths = []
    for pick in picks:
        label = labels[pick]
        if label.endswith("+"):
            lengths.append(int(rng.integers(int(label[:-1]), _LARGEST_BUCKET + 1)))
        else:
            lengths.append(int(label))
    return np.array(lengths)


def _rows(n: int, count: int, rng: np.random.Generator, avoid=()) -> np.ndarray:
    """`count` distinct rows, preferring rows not in `avoid`."""
    avoid = set(int(r) for r in avoid)
    free = np.array([r for r in range(n) if r not in avoid], dtype=int)
    rest = np.array(sorted(avoid), dtype=int)
    order = np.concatenate([rng.permutation(free), rng.permutation(rest)])
    return order[:count]


def synthetic_text(script: str, length: int, rng: np.random.Generator) -> str:
    """A made-up name of about `length` characters that contains `script`."""
    length = max(int(length), 1)
    letters = ALPHABETS["ascii" if script == "emoji" else script]
    words, size = [], 0
    while size < length:
        word = "".join(rng.choice(list(letters), size=int(rng.integers(2, 8))))
        words.append(word.capitalize() if script in ("ascii", "latin") else word)
        size += len(word) + 1
    text = " ".join(words)[:length].strip() or letters[0]
    if script == "emoji":
        text = f"{text} {rng.choice(list(ALPHABETS['emoji']))}"
    elif script == "latin" and text.isascii():
        text = f"{text}{rng.choice(list(ALPHABETS['latin']))}"
    return text


def _texts(shape: dict, n: int, rng: np.random.Generator) -> list[str]:
    """Names whose scripts, lengths and duplicate rate follow a text profile."""
    scripts = np.array(["ascii"] * n, dtype=object)
    used: set[int] = set()
    for script, share in shape["scripts"].items():
        rows = _rows(n, _count(share, n), rng, avoid=used)
        scripts[rows] = script
        used.update(int(r) for r in rows)
    lengths = _quantile_sampler(shape["length_quantiles"], rng)(n).round()
    texts = [
        synthetic_text(script, length, rng)
        for script, length in zip(scripts, lengths, strict=True)
    ]
    duplicates = _count(shape["duplicate_rate"], n) // 2
    for row in _rows(n, duplicates, rng):
        texts[int(row)] = texts[int(rng.integers(n))]
    return texts


def _artists(shape: dict, n: int, rng: np.random.Generator) -> list[str]:
    """Artist names with roughly the profile's distinct-artist share, Zipf-sized."""
    distinct = max(2, _count(shape["artist"]["distinct_per_track"], n))
    names = _texts({**shape["artist"], "duplicate_rate": 0.0}, distinct, rng)
    names = list(dict.fromkeys(names))
    weights = 1 / np.arange(1, len(names) + 1)
    picks = rng.choice(len(names), size=n, p=weights / weights.sum())
    picks[: len(names)] = rng.permutation(len(names))[:n]  # every name is used
    return [names[i] for i in picks]


def from_shape(profile: dict, n: int, seed: int = 0) -> pd.DataFrame:
    """A synthetic catalog frame of `n` tracks shaped like `profile`."""
    rng = np.random.default_rng(seed)
    genres = sorted(profile["genres"])
    weights = np.array([profile["genres"][g] for g in genres], dtype=np.float64)
    genre_rows = rng.choice(len(genres), size=n, p=weights / weights.sum())
    genre_rows[: len(genres)] = np.arange(min(n, len(genres)))  # every genre occurs

    track_ids = np.sort(rng.choice(_ID_SPAN, size=n, replace=False) + _FIRST_ID)
    frame = pd.DataFrame(
        {
            "track_id": track_ids,
            "title": _texts(profile["title"], n, rng),
            "artist": _artists(profile, n, rng),
            "album": [f"Album {i % max(1, n // 6)}" for i in range(n)],
            "genre_top": [genres[g] for g in genre_rows],
        }
    )

    extra = len(genres) + 1 + np.arange(EXTRA_GENRE_IDS)
    genre_ids = []
    for g, size in zip(
        genre_rows, _buckets(profile["genre_id_count"], n, rng), strict=True
    ):
        more = rng.choice(extra, size=max(int(size) - 1, 0), replace=False)
        genre_ids.append([int(g) + 1, *(int(i) for i in more)])
    frame["genre_ids"] = genre_ids

    vocabulary = [
        f"tag{i:03d}"
        for i in range(max(5, _count(profile["distinct_tags_per_track"], n)))
    ]
    tag_weights = 1 / np.arange(1, len(vocabulary) + 1)
    tag_weights /= tag_weights.sum()
    frame["tags"] = [
        sorted(set(rng.choice(vocabulary, size=int(size), p=tag_weights)))
        for size in _buckets(profile["tag_count"], n, rng)
    ]
    if not frame["tags"].map(len).gt(0).any():
        frame.at[0, "tags"] = [vocabulary[0]]

    frame["duration"] = _quantile_sampler(profile["duration_quantiles"], rng)(n).round()
    listens = _quantile_sampler(profile["listens_quantiles"], rng)(n)
    frame["listens"] = listens.round().astype(np.int64)
    frame["license_url"] = "https://creativecommons.org/licenses/by/4.0/"
    frame["source_url"] = [f"https://example.com/track/{t}" for t in track_ids]

    for name in profile["missing"]:
        if name not in frame:
            continue
        count = _count(profile["missing"][name], n, edge=name in EDGE_FIELDS)
        rows = _rows(n, count, rng)
        if name in ("genre_ids", "tags"):
            already = int(frame[name].map(len).eq(0).sum())
            rows = _rows(n, max(count - already, 0), rng)
            for row in rows:
                frame.at[int(row), name] = []
        else:
            frame[name] = frame[name].astype(object)
            frame.loc[rows, name] = None

    columns = [
        f"{feature}_{stat}_{number:02d}"
        for feature, size in profile["features"]["coefficients"].items()
        for stat in profile["features"]["statistics"]
        for number in range(1, size + 1)
    ]
    centres = rng.normal(scale=1.5, size=(len(genres), len(columns)))
    values = centres[genre_rows] + rng.normal(size=(n, len(columns)))
    features = pd.DataFrame(values.astype(np.float32), columns=columns)
    return pd.concat([frame, features], axis=1)


def vectors_for(
    frame: pd.DataFrame, profile: dict, seed: int = 0
) -> tuple[VectorStore, list[int]]:
    """Clip vectors for the catalog, minus the tracks the profile says lack one."""
    rng = np.random.default_rng(seed)
    n = len(frame)
    missing = _rows(n, _count(profile["audio"]["without_vector"], n, edge=True), rng)
    keep = np.setdiff1d(np.arange(n), missing)
    ids = frame["track_id"].to_numpy()
    vectors = rng.normal(size=(len(keep), VECTOR_DIMENSIONS)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    failures = {str(int(ids[r])): "cannot decode: synthetic" for r in missing}
    store = VectorStore(ids[keep], vectors, {"failures": failures})
    return store, sorted(int(ids[r]) for r in missing)
