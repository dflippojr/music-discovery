"""Hybrid ranker and its tuning, on a synthetic catalog with planted structure."""

import json
from dataclasses import replace

import numpy as np
import pytest
from test_evaluation import make_frame

from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main
from musicdiscovery.evaluation import generate_queries, write_queries
from musicdiscovery.hybrid import (
    VARIETY_TEXT,
    HybridConfig,
    HybridConfigError,
    HybridRanker,
    _Metadata,
)
from musicdiscovery.preference import Preference
from musicdiscovery.ranking import Ranker
from musicdiscovery.tuning import main as tune_main
from musicdiscovery.tuning import tune


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_frame())


def pref(seed=100, **kwargs):
    return Preference.from_strings([seed], **kwargs)


def artists(results):
    return [r.artist for r in results]


def test_interface_and_shipped_config(catalog):
    ranker: Ranker = HybridRanker()
    results = ranker.rank(pref(), catalog)
    assert len(results) == 10
    assert 100 not in {r.track_id for r in results}
    assert ranker.config == HybridConfig.load()
    assert 0 <= ranker.config.mmr_lambda <= 1


def test_deterministic(catalog):
    a = HybridRanker().rank(pref(likes=["more:energy"]), catalog)
    b = HybridRanker().rank(pref(likes=["more:energy"]), catalog)
    assert a == b


def test_no_diversity_matches_relevance_order(catalog):
    flat = HybridRanker(HybridConfig(mmr_lambda=0.0))
    results = flat.rank(pref(), catalog)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert not any("variety" in {t.name for t in r.terms if t.value} for r in results)


def test_mmr_adds_artist_variety_and_explains_it(catalog):
    flat = HybridRanker(HybridConfig(mmr_lambda=0.0)).rank(pref(k=20), catalog)
    mixed = HybridRanker(HybridConfig(mmr_lambda=0.8, genre_redundancy=0.0)).rank(
        pref(k=20), catalog
    )
    assert len(set(artists(mixed))) > len(set(artists(flat)))
    promoted = [r for r in mixed if VARIETY_TEXT in r.explanations]
    assert promoted
    for r in promoted:
        variety = next(t for t in r.terms if t.name == "variety")
        assert variety.value > 0
        assert r.score == pytest.approx(sum(t.value for t in r.terms))
    for r in mixed:
        assert 1 <= len(r.explanations) <= 3


def test_genre_redundancy_spreads_genres(catalog):
    only_artist = HybridRanker(HybridConfig(mmr_lambda=0.9, genre_redundancy=0.0))
    with_genre = HybridRanker(HybridConfig(mmr_lambda=0.9, genre_redundancy=1.0))
    frame = catalog.frame.set_index("track_id")

    def genres(ranker):
        out = ranker.rank(pref(k=15), catalog)
        return {frame.loc[r.track_id, "genre_top"] for r in out}

    assert len(genres(with_genre)) >= len(genres(only_artist))


def test_exclude_seed_artist_and_small_catalog(catalog):
    seed = 100
    artist = catalog.frame.set_index("track_id").loc[seed, "artist"]
    out = HybridRanker().rank(pref(seed, exclude_seed_artist=True, k=500), catalog)
    assert artist not in artists(out)
    assert len(out) == len(catalog) - 1 - int(
        (catalog.frame["artist"] == artist).sum() - 1
    )


def test_metadata_similarity_prefers_shared_genre_ids_and_tags(catalog):
    metadata = _Metadata(catalog)
    seed_row = catalog.position[100]
    sim = metadata.similarity([seed_row])
    genres = catalog.frame["genre_top"].to_numpy()
    same = genres == genres[seed_row]
    assert sim[same].mean() > sim[~same].mean()
    assert sim[seed_row] == pytest.approx(1.0)


def test_metadata_similarity_without_tokens():
    frame = make_frame(per_genre=5)
    frame["genre_ids"] = [[] for _ in range(len(frame))]
    frame["tags"] = [[] for _ in range(len(frame))]
    catalog = Catalog(frame)
    sim = _Metadata(catalog).similarity([0])
    assert not sim.any()
    assert len(HybridRanker().rank(pref(), catalog)) == 10


def test_metadata_weight_scales_the_metadata_term(catalog):
    low = HybridRanker(HybridConfig(metadata_weight=0.5, mmr_lambda=0.0))
    high = HybridRanker(HybridConfig(metadata_weight=2.0, mmr_lambda=0.0))

    def term(ranker):
        first = ranker.rank(pref(), catalog)[0]
        return next(t.value for t in first.terms if t.name == "metadata")

    assert term(high) > term(low) > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metadata_weight": -1},
        {"popularity_weight": -0.1},
        {"mmr_lambda": 1.5},
        {"genre_redundancy": -0.1},
        {"pool_size": 0},
    ],
)
def test_bad_config_rejected(kwargs):
    with pytest.raises(HybridConfigError):
        HybridConfig(**kwargs)


def test_config_load_and_dump(tmp_path):
    config = replace(HybridConfig(), mmr_lambda=0.25)
    path = tmp_path / "c.json"
    path.write_text(config.dump())
    assert HybridConfig.load(path) == config
    path.write_text('{"nope": 1}')
    with pytest.raises(HybridConfigError):
        HybridConfig.load(path)
    with pytest.raises(HybridConfigError):
        HybridConfig.load(tmp_path / "missing.json")


GRID = {
    "metadata_weight": (0.0, 1.0),
    "popularity_weight": (0.0,),
    "mmr_lambda": (0.0, 0.5),
    "genre_redundancy": (0.0, 0.5),
}


def test_tuning_is_reproducible_and_respects_the_floor(catalog):
    queries = generate_queries(catalog, per_split=20)
    first, rows = tune(catalog, queries, GRID)
    second, _ = tune(catalog, queries, GRID)
    assert first == second
    assert len(rows) == 8
    chosen = next(r for r in rows if r["config"] == first)
    assert chosen["means"]["genre_precision"] >= chosen["floor"]


@pytest.fixture
def workdir(tmp_path, monkeypatch, catalog):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "data" / "catalog"
    out.mkdir(parents=True)
    catalog.frame.to_parquet(out / "catalog.parquet", index=False)
    write_queries(tmp_path / "eval" / "q.json", generate_queries(catalog, 20), catalog)
    return tmp_path


def test_cli_recommend_hybrid(workdir, capsys):
    argv = ["recommend", "--ranker", "hybrid", "--seed", "100", "--json"]
    assert main(argv) == 0
    items = json.loads(capsys.readouterr().out)
    assert len(items) == 10
    assert {t["name"] for t in items[0]["terms"]} >= {"audio", "metadata"}


def test_cli_evaluate_includes_hybrid(workdir, capsys):
    base = ["evaluate", "--queries", "eval/q.json", "--date", "2026-10-05"]
    assert main(base) == 0
    text = (workdir / "reports" / "offline-2026-10-05.md").read_text()
    assert "| hybrid |" in text


def test_tune_cli(workdir, monkeypatch, capsys):
    import musicdiscovery.tuning as tuning

    monkeypatch.setattr(tuning, "GRID", GRID)
    monkeypatch.setattr(
        tuning, "tune", lambda c, q: (HybridConfig(mmr_lambda=0.5), [_row()])
    )
    (workdir / "src" / "musicdiscovery").mkdir(parents=True)
    assert tune_main(["--queries", "eval/q.json", "--write"]) == 0
    written = json.loads((workdir / tuning.CONFIG_PATH).read_text())
    assert written["mmr_lambda"] == 0.5
    assert "genre precision floor" in capsys.readouterr().out
    assert tune_main(["--data-dir", "nowhere"]) == 1
    assert "tune:" in capsys.readouterr().err


def _row():
    means = {"genre_precision": 0.9, "genre_ndcg": 0.8, "artist_diversity": 0.7}
    return {"config": HybridConfig(mmr_lambda=0.5), "means": means, "floor": 0.8}


def test_variety_values_are_finite(catalog):
    out = HybridRanker(HybridConfig(mmr_lambda=0.9)).rank(pref(k=20), catalog)
    assert all(np.isfinite(r.score) for r in out)
