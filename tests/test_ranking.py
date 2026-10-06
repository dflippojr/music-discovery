"""Ranker behaviour on a small synthetic catalog; no datasets, no network."""

import json
import time

import numpy as np
import pandas as pd
import pytest

from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.cli import main
from musicdiscovery.preference import Preference, PreferenceError, Quality
from musicdiscovery.ranking import BaselineRanker, Ranker

GENRES = ["Folk", "Rock", "Jazz"]


def make_frame(per_genre=40, extra_features=0, seed=0):
    rng = np.random.default_rng(seed)
    rows = []
    track_id = 100
    for g, genre in enumerate(GENRES):
        centre = np.zeros(6)
        centre[g] = 4.0
        for i in range(per_genre):
            vec = centre + rng.normal(size=6)
            rows.append(
                {
                    "track_id": track_id,
                    "title": f"t{track_id}",
                    "artist": f"artist{track_id % 20}",
                    "genre_top": genre,
                    "genre_ids": [g + 1],
                    "tags": ["loud"] if i % 5 == 0 else ["quiet"],
                    "mfcc_mean_01": vec[0],
                    "mfcc_mean_02": vec[1],
                    "mfcc_mean_03": vec[2],
                    "rmse_mean_01": rng.normal(),
                    "spectral_centroid_mean_01": rng.normal(),
                    "zcr_mean_01": rng.normal(),
                    **{f"x_{j}": rng.normal() for j in range(extra_features)},
                }
            )
            track_id += 1
    return pd.DataFrame(rows).sample(frac=1, random_state=1)


@pytest.fixture
def catalog():
    return Catalog(make_frame())


def pref(seed=100, **kwargs):
    return Preference.from_strings([seed], **kwargs)


def test_interface(catalog):
    ranker: Ranker = BaselineRanker()
    assert len(ranker.rank(pref(), catalog)) == 10


def test_seeds_excluded_and_same_genre_first(catalog):
    results = BaselineRanker().rank(Preference((100, 101), k=20), catalog)
    ids = {r.track_id for r in results}
    assert not ids & {100, 101}
    genres = catalog.frame.set_index("track_id").loc[list(ids), "genre_top"]
    assert (genres == "Folk").all()


def test_exclude_seed_artist(catalog):
    seed_artist = catalog.frame.set_index("track_id").loc[100, "artist"]
    by_id = catalog.frame.set_index("track_id")["artist"]
    kept = BaselineRanker().rank(pref(k=100), catalog)
    assert any(by_id[r.track_id] == seed_artist for r in kept)
    excluded = BaselineRanker().rank(pref(k=100, exclude_seed_artist=True), catalog)
    assert all(by_id[r.track_id] != seed_artist for r in excluded)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"seed": 5}, "unknown track ids"),
        ({"likes": ["genre:Polka"]}, "unknown genre"),
        ({"dislikes": ["tag:nope"]}, "unknown tag"),
    ],
)
def test_unknown_values_rejected(catalog, kwargs, message):
    with pytest.raises(PreferenceError, match=message):
        BaselineRanker().rank(pref(**kwargs), catalog)


@pytest.mark.parametrize(
    "text", ["more:speed", "energy", "mood:happy", "more:", "less:Energy:x"]
)
def test_bad_quality_rejected(text):
    with pytest.raises(PreferenceError):
        Quality.parse(text)


def test_bad_preference_rejected():
    with pytest.raises(PreferenceError, match="seed"):
        Preference(())
    with pytest.raises(PreferenceError, match="k must"):
        Preference((1,), k=0)


def test_first_explanation_is_largest_term(catalog):
    results = BaselineRanker().rank(
        pref(likes=["genre:Rock", "more:energy"], dislikes=["tag:quiet"], k=30),
        catalog,
    )
    for item in results:
        assert item.explanations
        assert len(item.explanations) <= 3
        top = max(item.terms, key=lambda t: t.value)
        assert item.explanations[0] == top.text
        assert item.score == pytest.approx(sum(t.value for t in item.terms))


def test_explanation_fallback_when_nothing_positive(catalog):
    results = BaselineRanker().rank(
        pref(
            dislikes=["genre:Rock", "genre:Jazz", "genre:Folk"],
            k=catalog.frame.shape[0],
        ),
        catalog,
    )
    assert all(r.explanations for r in results)


@pytest.mark.parametrize(("field", "sign"), [("likes", 1), ("dislikes", -1)])
def test_axis_moves_results(field, sign):
    catalog = Catalog(make_frame(per_genre=120, seed=3))
    rmse = catalog.frame.set_index("track_id")["rmse_mean_01"]
    ranker = BaselineRanker()
    base = ranker.rank(pref(k=20), catalog)
    moved = ranker.rank(pref(k=20, **{field: ["more:energy"]}), catalog)
    delta = rmse[[r.track_id for r in moved]].mean()
    delta -= rmse[[r.track_id for r in base]].mean()
    assert delta * sign > 0


def test_less_axis_goes_the_other_way():
    catalog = Catalog(make_frame(per_genre=120, seed=3))
    rmse = catalog.frame.set_index("track_id")["rmse_mean_01"]
    ranker = BaselineRanker()
    up = ranker.rank(pref(k=20, likes=["more:energy"]), catalog)
    down = ranker.rank(pref(k=20, likes=["less:energy"]), catalog)
    assert (
        rmse[[r.track_id for r in down]].mean() < rmse[[r.track_id for r in up]].mean()
    )
    assert down[0].explanations[0] in ("calmer, as asked", "similar sound to the seed")


def test_genre_and_tag_boost_and_penalty(catalog):
    by_id = catalog.frame.set_index("track_id")
    liked = BaselineRanker().rank(pref(likes=["genre:Rock"], k=100), catalog)
    assert any(r.terms[1].value == pytest.approx(0.15) for r in liked)
    penalised = BaselineRanker().rank(pref(dislikes=["genre:Folk"], k=5), catalog)
    assert all(r.terms[1].value == pytest.approx(-0.15) for r in penalised)
    disliked = BaselineRanker().rank(pref(k=60, dislikes=["tag:loud"]), catalog)
    assert all("loud" not in by_id.loc[r.track_id, "tags"] for r in disliked[:10])
    tagged = BaselineRanker().rank(pref(likes=["tag:loud"], k=50), catalog)
    for item in tagged:
        expected = 0.1 if "loud" in by_id.loc[item.track_id, "tags"] else 0.0
        assert item.terms[1].value == pytest.approx(expected)


def test_deterministic_and_ties_by_track_id():
    frame = make_frame()
    for column in frame.columns[5:]:
        frame[column] = 1.0
    ranker = BaselineRanker()
    first = ranker.rank(pref(), Catalog(frame))
    second = BaselineRanker().rank(
        pref(), Catalog(frame.sample(frac=1, random_state=9))
    )
    assert [r.track_id for r in first] == [r.track_id for r in second]
    assert [r.track_id for r in first] == sorted(r.track_id for r in first)


def test_same_output_across_runs(catalog):
    preference = pref(likes=["more:brightness", "genre:Jazz"])
    first = BaselineRanker().rank(preference, catalog)
    second = BaselineRanker().rank(preference, catalog)
    assert first == second


def test_no_pca_and_constant_columns():
    frame = make_frame()
    frame["flat"] = 1.0
    results = BaselineRanker(dimensions=0).rank(pref(), Catalog(frame))
    assert len(results) == 10


def test_catalog_validation(tmp_path):
    with pytest.raises(CatalogError, match="missing columns"):
        Catalog(pd.DataFrame({"track_id": [1]}))
    frame = make_frame()
    with pytest.raises(CatalogError, match="duplicate"):
        Catalog(pd.concat([frame, frame]))
    meta = frame[["track_id", "artist", "genre_top", "tags"]]
    with pytest.raises(CatalogError, match="no feature"):
        Catalog(meta)
    with pytest.raises(CatalogError, match="ingest"):
        Catalog.load(tmp_path / "missing.parquet")


def test_speed_documented_not_gated():
    catalog = Catalog(make_frame(per_genre=2666, extra_features=60))
    ranker = BaselineRanker()
    ranker.rank(pref(), catalog)  # fit once
    start = time.perf_counter()
    ranker.rank(pref(likes=["more:energy", "genre:Rock"]), catalog)
    assert time.perf_counter() - start < 5  # loose guard; target is in docs


@pytest.fixture
def data_dir(tmp_path):
    out = tmp_path / "catalog"
    out.mkdir()
    make_frame().to_parquet(out / "catalog.parquet", index=False)
    return tmp_path


def test_cli_text_and_json(data_dir, capsys):
    base = ["recommend", "--data-dir", str(data_dir), "--seed", "100", "-k", "3"]
    assert main(base + ["--like", "more:energy"]) == 0
    text = capsys.readouterr().out
    assert text.count("\n") == 6 and "similar sound" in text
    assert main(base + ["--json", "--exclude-seed-artist"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 3 and payload[0]["explanations"]


@pytest.mark.parametrize(
    "extra", [["--seed", "5"], ["--seed", "100", "--like", "bogus"]]
)
def test_cli_errors(data_dir, capsys, extra):
    assert main(["recommend", "--data-dir", str(data_dir), *extra]) == 1
    assert capsys.readouterr().err.startswith("recommend: ")


def test_cli_missing_catalog(tmp_path, capsys):
    assert main(["recommend", "--data-dir", str(tmp_path), "--seed", "1"]) == 1
    assert "ingest" in capsys.readouterr().err
