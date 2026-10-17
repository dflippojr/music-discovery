"""Laws every ranker keeps for any catalog and query, searched with Hypothesis.

The catalogs are synthetic: missing or NA top genres, empty or sparse tags,
duplicate, blank, non-Latin and emoji titles, repeated or constant feature
rows, exact twins of a track, catalogs with and without the axis feature
columns, and tracks with and without a clip vector. The `ci` profile
(tests/conftest.py) is derandomized and
bounded; HYPOTHESIS_PROFILE=deep searches further.
"""

import base64
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st

from musicdiscovery.audio_embedding import (
    AudioEmbeddingError,
    AudioEmbeddingRanker,
    VectorStore,
)
from musicdiscovery.catalog import METADATA_COLUMNS, Catalog
from musicdiscovery.demo import build_catalog
from musicdiscovery.evaluation import Query, RandomRanker, evaluate, make_ranker
from musicdiscovery.hybrid import HybridRanker
from musicdiscovery.preference import AXES, Preference, Quality
from musicdiscovery.profiles import SPLITS
from musicdiscovery.ranking import BaselineRanker

GENRES = ("Rock", "Folk", "Hip-Hop", "Électronique", "民谣")
TAGS = ("loud", "lo-fi", "ambient", "東京", "🎸")
ARTISTS = ("A", "B", "C", "Дмитрий", "李")
TITLES = ("Untitled", "Intro", "Мелодия", "夜明け", "🎶🔥", "  ", "")
MISSING = (None, np.nan, pd.NA)
VECTOR_DIMENSION = 3


@dataclass(frozen=True)
class Case:
    frame: pd.DataFrame
    store: VectorStore
    preference: Preference

    def _repr_pretty_(self, printer, cycle) -> None:
        """Print a shrunk counterexample in full: every column of every track."""
        with pd.option_context("display.width", 200, "display.max_columns", None):
            frame = self.frame.sort_values("track_id").to_string(index=False)
        printer.text(
            f"Case(\n{frame}\nvectors for {sorted(self.store.track_ids.tolist())}\n"
            f"{self.preference!r})"
        )


@st.composite
def frames(draw, axes: bool | None = None) -> pd.DataFrame:
    """Synthetic catalogs; `axes` says whether they have the axis feature columns."""
    n = draw(st.integers(1, 16))
    ids = draw(st.lists(st.integers(1, 999_999), min_size=n, max_size=n, unique=True))
    rng = np.random.default_rng(draw(st.integers(0, 2**32 - 1)))
    columns = [f"mfcc_mean_{j:02d}" for j in range(draw(st.integers(1, 4)))]
    if draw(st.booleans()) if axes is None else axes:
        columns += [axis.column for axis in AXES.values()]
    features = rng.normal(size=(n, len(columns)))
    layout = draw(st.sampled_from(["generic", "repeated rows", "constant"]))
    if layout == "constant":
        features[:] = features[0]
    elif layout == "repeated rows":
        for row in range(1, n):
            source = draw(st.none() | st.integers(0, row - 1))
            if source is not None:
                features[row] = features[source]
    rows = []
    for i, track_id in enumerate(ids):
        rows.append(
            {
                "track_id": track_id,
                "title": draw(st.sampled_from(TITLES + MISSING) | st.text(max_size=8)),
                "artist": draw(st.sampled_from(ARTISTS)),
                "album": draw(st.sampled_from(("LP", "Мини-альбом", *MISSING))),
                "genre_top": draw(st.sampled_from(GENRES + MISSING)),
                "genre_ids": draw(st.lists(st.integers(1, 6), max_size=2)),
                "tags": draw(st.lists(st.sampled_from(TAGS), max_size=2, unique=True)),
                "listens": draw(st.integers(0, 10_000)),
                **dict(zip(columns, features[i], strict=True)),
            }
        )
    # Exact twins under new ids: every score ties, so the tie-break shows.
    for i, source in enumerate(draw(st.lists(st.integers(0, n - 1), max_size=3))):
        rows.append({**rows[source], "track_id": 1_000_001 + i})
    return pd.DataFrame(rows)


def qualities(frame: pd.DataFrame) -> st.SearchStrategy[list[str]]:
    genres = sorted({g for g in frame["genre_top"] if isinstance(g, str)})
    tags = sorted({t for row in frame["tags"] for t in row})
    options = [f"genre:{g}" for g in genres] + [f"tag:{t}" for t in tags]
    for name, axis in AXES.items():
        if axis.column in frame:
            options += [f"more:{name}", f"less:{name}"]
    return st.lists(st.sampled_from(options), max_size=2) if options else st.just([])


@st.composite
def cases(draw, no_signal: bool = False, axes: bool | None = None) -> Case:
    """A catalog, a clip vector store and a valid query over the catalog.

    `no_signal` takes away every signal a seed can carry: the features are
    constant, no seed has a clip vector and nothing is liked or disliked.
    """
    frame = draw(frames(axes))
    if no_signal:
        features = [c for c in frame.columns if c not in METADATA_COLUMNS]
        frame[features] = frame[features].iloc[0].to_numpy()
    ids = frame["track_id"].tolist()
    seeds = draw(st.lists(st.sampled_from(ids), min_size=1, max_size=3))
    if no_signal:
        likes, dislikes = [], []
    else:
        likes = draw(qualities(frame))
        dislikes = draw(qualities(frame))[:1]
    strengths = draw(
        st.none()
        | st.lists(st.floats(0.1, 5), min_size=len(seeds), max_size=len(seeds))
    )
    preference = Preference(
        tuple(seeds),
        tuple(Quality.parse(q) for q in likes),
        tuple(Quality.parse(q) for q in dislikes),
        k=draw(st.integers(1, 20)),
        exclude_seed_artist=draw(st.booleans()),
        strengths=tuple(strengths or ()),
    )
    with_vector = [t for t in ids if draw(st.booleans())]
    if no_signal:
        with_vector = [t for t in with_vector if t not in seeds]
    if draw(st.booleans()):  # a stale vector for a track the catalog lost
        with_vector.append(1_000_000)
    rng = np.random.default_rng(len(ids))
    vectors = rng.normal(size=(len(with_vector), VECTOR_DIMENSION)) + 0.1
    store = VectorStore(np.array(with_vector, dtype=np.int64), vectors)
    return Case(frame, store, preference)


def quality_text(quality: Quality) -> str:
    """The text form `Quality.parse` reads, as the demo page sends it."""
    if quality.kind == "axis":
        return f"{'more' if quality.direction > 0 else 'less'}:{quality.name}"
    return f"{quality.kind}:{quality.name}"


RANKERS = {
    "baseline": lambda store: make_ranker("baseline"),
    "hybrid": lambda store: make_ranker("hybrid"),
    "profile": lambda store: make_ranker("profile"),
    "profile-diverse": lambda store: make_ranker("profile-diverse"),
    "random": lambda store: RandomRanker(seed=7),
    "popular": lambda store: make_ranker("popular"),
    "audio-embedding": lambda store: AudioEmbeddingRanker(store=store),
}


def has_signal(name: str, case: Case, catalog: Catalog) -> bool:
    """Whether the query carries the signal the ranker needs to rank by.

    The hybrid and profile rankers always have one (metadata and the
    popularity prior), as has `popular` (listen counts). `random` is meant to
    guess and is left out of this law.
    """
    preference = case.preference
    if name == "audio-embedding":
        return bool(set(preference.seeds) & set(case.store.track_ids.tolist()))
    if name == "baseline":
        varies = bool(np.ptp(catalog.features, axis=0).any())
        return varies or bool(preference.likes or preference.dislikes)
    return True


def is_true(term, catalog: Catalog, row: int) -> bool:
    """Whether a cited score term holds for the track at `row`."""
    frame = catalog.frame
    if term.name.startswith("genre:"):
        genre = frame["genre_top"].iat[row]
        return term.value > 0 and genre == term.name.removeprefix("genre:")
    if term.name.startswith("tag:"):
        return (
            term.value > 0 and term.name.removeprefix("tag:") in frame["tags"].iat[row]
        )
    return term.value > 0


def summary(results) -> list[tuple]:
    return [(r.track_id, r.score, r.explanations) for r in results]


@pytest.mark.parametrize("name", list(RANKERS))
@given(case=cases())
def test_ranker_contract(name, case):
    catalog = Catalog(case.frame)
    preference = case.preference
    ranker = RANKERS[name](case.store)
    if name == "audio-embedding" and not catalog.position.keys() & set(
        case.store.track_ids.tolist()
    ):
        # The documented "cannot rank": no catalog track has a vector.
        with pytest.raises(AudioEmbeddingError, match="no catalog track"):
            ranker.rank(preference, catalog)
        return

    # 3. Missing metadata never raises: any exception fails the test.
    results = ranker.rank(preference, catalog)
    ids = [r.track_id for r in results]

    # 1. Result shape.
    assert len(ids) <= preference.k
    assert len(set(ids)) == len(ids), "duplicate results"
    assert not set(ids) & set(preference.seeds), "a seed was returned"
    assert set(ids) <= catalog.position.keys()
    if preference.exclude_seed_artist:
        artists = catalog.frame["artist"]
        seed_artists = {artists.iat[catalog.position[s]] for s in preference.seeds}
        assert not {artists.iat[catalog.position[t]] for t in ids} & seed_artists
    for r in results:
        title = catalog.frame["title"].iat[catalog.position[r.track_id]]
        shown = f"Track {r.track_id}" if pd.isna(title) or not title.strip() else title
        assert r.title == shown

    # 2. Abstains instead of guessing.
    if not has_signal(name, case, catalog):
        assert results == [], "ranked without any signal"

    # 4. Deterministic: again, and with a new ranker on a new catalog.
    assert summary(ranker.rank(preference, catalog)) == summary(results)
    fresh = RANKERS[name](case.store).rank(preference, Catalog(case.frame.copy()))
    assert summary(fresh) == summary(results)

    # 5. Explanations are true of the track they explain.
    for r in results:
        row = catalog.position[r.track_id]
        for text in r.explanations:
            cited = [t for t in r.terms if t.text == text]
            assert any(is_true(t, catalog, row) for t in cited), (r.track_id, text)


@pytest.mark.parametrize("name", list(RANKERS))
@given(case=cases(no_signal=True))
def test_abstains_without_signal(name, case):
    """Constant features, no seed vector and no qualities: #86's shape."""
    catalog = Catalog(case.frame)
    if name == "audio-embedding" and not catalog.position.keys() & set(
        case.store.track_ids.tolist()
    ):
        return  # the "cannot rank" error is checked by the contract test
    results = RANKERS[name](case.store).rank(case.preference, catalog)
    if not has_signal(name, case, catalog):
        assert results == [], f"{name} ranked without any signal"


@given(case=cases())
def test_evaluate_never_raises_on_missing_metadata(case):
    """#89's class: the evaluator scores catalogs with gaps in their metadata."""
    catalog = Catalog(case.frame)
    seed = case.preference.seeds[0]
    likes = tuple(quality_text(q) for q in case.preference.likes)
    queries = [Query(i, split, seed, likes) for i, split in enumerate(SPLITS)]
    names = [n for n in RANKERS if n not in ("audio-embedding",)]
    results = evaluate(catalog, queries, names, k=case.preference.k, resamples=5)
    assert set(results["rankers"]) == set(names)


# --- 6. JS parity --------------------------------------------------------------

LINE_RANKER = Path(__file__).parent / "demo" / "rank-lines.mjs"
JS_RANKERS = ("baseline", "hybrid")
TIE = 1e-9  # scores this close are a tie that float rounding may break either way


@pytest.fixture(scope="module")
def js_rank():
    """Rank with ranker.js in one Node process for the whole module."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("the JS parity law needs node on PATH")
        pytest.skip("node is not on PATH")
    process = subprocess.Popen(
        [node, str(LINE_RANKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )

    def rank(catalog: dict, queries: list[dict]) -> list:
        process.stdin.write(json.dumps({"catalog": catalog, "queries": queries}) + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    yield rank
    process.stdin.close()
    process.wait(timeout=30)


def _decoded(blob: str, dtype: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(blob), dtype=dtype).astype(np.float64)


def bundled_rankers(catalog: Catalog, bundle: dict) -> dict:
    """Python rankers that read the bundle's quantised numbers, as the browser does.

    The stored parity.json checks that quantising keeps the top 10 on a fixed
    catalog; this checks the ranking logic itself, so both sides must start
    from the same numbers.
    """
    n, base, meta = bundle["count"], bundle["baseline"], bundle["hybrid"]
    projected = _decoded(base["projected"], "<i2").reshape(n, base["dims"])
    projected *= base["projectedScale"]
    baseline = BaselineRanker()
    fit = baseline._fitted(catalog)
    norms = np.linalg.norm(projected, axis=1)
    norms[norms == 0] = 1.0
    fit.projected, fit.unit = projected, projected / norms[:, None]
    axis_z = _decoded(base["axisData"], "<i2").reshape(n, -1) * base["axisScale"]
    for i, axis in enumerate(sorted(AXES)):
        fit.z[:, fit.columns[AXES[axis].column]] = axis_z[:, i]
    hybrid = HybridRanker()
    hybrid._baseline = baseline
    metadata, _ = hybrid._prepare(catalog)
    metadata.values = _decoded(meta["values"], "<u2") * meta["valueScale"]
    hybrid._popularity = _decoded(meta["popularity"], "<u2") * meta["popularityScale"]
    return {"baseline": baseline, "hybrid": hybrid}


@given(case=cases(axes=True))
def test_js_rankers_match_python(case, js_rank):
    catalog = Catalog(case.frame)
    bundle = build_catalog(catalog)
    rankers = bundled_rankers(catalog, bundle)
    preference = case.preference
    query = {
        "seeds": list(preference.seeds),
        "likes": [quality_text(q) for q in preference.likes],
        "dislikes": [quality_text(q) for q in preference.dislikes],
        "excludeSeedArtist": preference.exclude_seed_artist,
    }
    replies = js_rank(
        bundle,
        [{"name": n, "preference": query, "k": preference.k} for n in JS_RANKERS],
    )
    for name, got in zip(JS_RANKERS, replies, strict=True):
        assert "error" not in got, (name, got)
        ranker = rankers[name]
        expected = ranker.rank(preference, catalog)
        assert len(got) == len(expected), name
        score = ranker.score_terms if name == "baseline" else ranker.relevance
        terms, scores, _ = score(preference, catalog)
        rows = np.column_stack([values for *_, values in terms])
        for js, py in zip(got, expected, strict=True):
            if js["id"] != py.track_id:
                # Different terms can sum to scores equal in exact arithmetic
                # (s + 0.1 - 0.1 against s) that round apart differently in each
                # language; past such a tie the lists may part ways. Tracks with
                # identical terms must still go to the lower track id in both.
                a, b = catalog.position[js["id"]], catalog.position[py.track_id]
                assert abs(scores[a] - scores[b]) <= TIE, (name, js["id"], py.track_id)
                assert not np.array_equal(rows[a], rows[b]), name
                break
            assert js["score"] == pytest.approx(py.score, abs=TIE), name
            assert tuple(js["explanations"]) == py.explanations, name
