"""Offline evaluation on a synthetic catalog with planted structure."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main
from musicdiscovery.evaluation import (
    METRICS,
    Context,
    EvaluationError,
    HeldOut,
    PopularRanker,
    Query,
    RandomRanker,
    _bare,
    evaluate,
    generate_queries,
    load_queries,
    make_ranker,
    query_metrics,
    render_report,
    run,
    write_queries,
)
from musicdiscovery.preference import Preference

GENRES = ["Folk", "Rock", "Jazz"]


def make_frame(per_genre=60, seed=0):
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
                    "artist": f"artist{track_id % 25}",
                    "genre_top": genre,
                    "genre_ids": [g + 1] if i % 2 else [g + 1, 9],
                    "tags": [f"{genre}-tag", "loud"] if i % 3 else [],
                    "listens": int(rng.integers(1, 10_000)),
                    **{f"mfcc_mean_{j:02d}": vec[j] for j in range(6)},
                    "rmse_mean_01": rng.normal(),
                    "spectral_centroid_mean_01": rng.normal(),
                    "zcr_mean_01": rng.normal(),
                }
            )
            track_id += 1
    return pd.DataFrame(rows).sample(frac=1, random_state=1)


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_frame())


@pytest.fixture(scope="module")
def queries(catalog):
    return generate_queries(catalog, per_split=20)


def test_queries_are_seeded_and_split(catalog, queries):
    assert queries == generate_queries(catalog, per_split=20)
    splits = [q.split for q in queries]
    assert splits.count("validation") == 20
    assert splits.count("test") == 20
    assert len({q.seed for q in queries}) == 40
    assert all(q.likes for q in queries)
    for q in queries:
        q.preference().validate(catalog)


def test_queries_round_trip(tmp_path, catalog, queries):
    path = tmp_path / "queries.json"
    write_queries(path, queries, catalog)
    assert load_queries(path, catalog) == queries


def test_query_file_errors(tmp_path, catalog, queries):
    with pytest.raises(EvaluationError, match="no queries"):
        load_queries(tmp_path / "missing.json", catalog)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(EvaluationError, match="cannot read"):
        load_queries(bad, catalog)
    path = tmp_path / "queries.json"
    write_queries(path, queries, catalog)
    with pytest.raises(EvaluationError, match="catalog of"):
        load_queries(path, Catalog(make_frame(per_genre=30)))
    payload = json.loads(path.read_text())
    payload["queries"] = [q for q in payload["queries"] if q["split"] == "test"]
    path.write_text(json.dumps(payload))
    with pytest.raises(EvaluationError, match="no validation"):
        load_queries(path, catalog)
    with pytest.raises(EvaluationError, match="too small"):
        generate_queries(catalog, per_split=500)


def test_reference_rankers(catalog):
    pref = Preference((100,), k=5)
    first = RandomRanker().rank(pref, catalog)
    again = RandomRanker().rank(pref, catalog)
    assert [r.track_id for r in first] == [r.track_id for r in again]
    assert 100 not in {r.track_id for r in first}
    frame = catalog.frame.set_index("track_id")
    popular = PopularRanker().rank(pref, catalog)
    assert {frame.loc[r.track_id, "genre_top"] for r in popular} == {
        frame.loc[100, "genre_top"]
    }
    listens = [frame.loc[r.track_id, "listens"] for r in popular]
    assert listens == sorted(listens, reverse=True)
    wide = Preference((100,), k=200, exclude_seed_artist=True)
    artist = frame.loc[100, "artist"]
    assert all(
        frame.loc[r.track_id, "artist"] != artist
        for r in RandomRanker().rank(wide, catalog)
    )
    with pytest.raises(EvaluationError):
        make_ranker("nope")


def test_random_scores_below_baseline_on_genre_precision(catalog, queries):
    results = evaluate(catalog, queries, ["baseline", "random"], resamples=200)
    for split in ("validation", "test"):
        baseline = results["rankers"]["baseline"][split]["genre_precision"]
        random = results["rankers"]["random"][split]["genre_precision"]
        assert baseline["mean"] > 0.9
        assert random["mean"] < 0.5
        assert random["hi"] < baseline["lo"]
    assert results["rankers"]["baseline"]["test"]["genre_ndcg"]["mean"] > 0.9


def test_metric_ranges_and_popularity_bias(catalog, queries):
    names = ["baseline", "random", "popular"]
    results = evaluate(catalog, queries, names, resamples=100)
    for ranker in results["rankers"].values():
        for scores in ranker.values():
            for name, score in scores.items():
                if score["mean"] is None:
                    continue
                assert 0 <= score["mean"] <= 1, name
                if name == "coverage":
                    assert score["lo"] is None
                    continue
                assert score["lo"] <= score["mean"] + 1e-9, name
                assert score["mean"] <= score["hi"] + 1e-9, name
    rankers = results["rankers"]
    assert (
        rankers["popular"]["test"]["popularity"]["mean"]
        > rankers["random"]["test"]["popularity"]["mean"]
    )
    assert (
        rankers["popular"]["test"]["coverage"]["mean"]
        < rankers["random"]["test"]["coverage"]["mean"]
    )


def test_adherence_counts_results_that_move_the_axis(catalog):
    ctx = Context(catalog)
    frame = catalog.frame
    seed_row = 5
    seed_value = frame["rmse_mean_01"].iloc[seed_row]
    higher = np.flatnonzero(frame["rmse_mean_01"] > seed_value)[:10]
    lower = np.flatnonzero(frame["rmse_mean_01"] < seed_value)[:10]
    seed = int(catalog.track_ids[seed_row])
    more = Query(0, "test", seed, ("more:energy",))
    less = Query(0, "test", seed, ("less:energy",))
    up = _bare(catalog, higher.tolist())
    down = _bare(catalog, lower.tolist())
    assert query_metrics(ctx, more, up)[0]["adherence"] == 1.0
    assert query_metrics(ctx, more, down)[0]["adherence"] == 0.0
    assert query_metrics(ctx, less, down)[0]["adherence"] == 1.0


def test_undefined_metrics_are_none(catalog):
    splits = ["validation"] * 3 + ["test"] * 3
    queries = [Query(i, s, 100 + i, ("genre:Folk",)) for i, s in enumerate(splits)]
    results = evaluate(catalog, queries, ["random"], resamples=50)
    assert results["rankers"]["random"]["test"]["adherence"]["mean"] is None


def test_same_inputs_give_identical_numbers(tmp_path, catalog, queries):
    path = tmp_path / "queries.json"
    write_queries(path, queries, catalog)
    day = date(2026, 10, 5)
    names = ["baseline", "random", "popular"]
    a = run(catalog, path, names, tmp_path / "a", day)
    b = run(catalog, path, names, tmp_path / "b", day)
    assert a[1].read_text() == b[1].read_text()
    assert a[0].read_text() == b[0].read_text()


def test_report_has_tables_limits_and_version(tmp_path, catalog, queries):
    path = tmp_path / "queries.json"
    write_queries(path, queries, catalog)
    manifest = tmp_path / "catalog.json"
    manifest.write_text(json.dumps({"ingest_version": 1, "source_sha1": "abc"}))
    md, js = run(
        catalog,
        path,
        ["baseline", "random", "popular", "random"],
        tmp_path,
        date(2026, 10, 5),
        manifest,
    )
    assert md.name == "offline-2026-10-05.md"
    assert js.name == "offline-2026-10-05.json"
    text = md.read_text()
    for title in METRICS.values():
        assert f"## {title}" in text
    assert text.count("| baseline |") == len(METRICS)
    assert text.count("| random |") == len(METRICS)
    assert "## Limits" in text
    assert "proxy for relevance, not ground truth" in text
    assert "## Catalog version" in text
    assert "FMA dataset v1" in text
    assert "`abc`" in text
    numbers = json.loads(js.read_text())
    assert numbers["catalog"]["ingest_version"] == 1
    assert len(numbers["queries_sha256"]) == 64
    assert set(numbers["rankers"]) == {"baseline", "random", "popular"}


def test_render_handles_missing_values():
    empty = {"mean": None, "lo": None, "hi": None}
    per_split = {s: dict.fromkeys(METRICS, empty) for s in ("validation", "test")}
    results = {
        "k": 10,
        "bootstrap_resamples": 5,
        "queries": {"validation": 1, "test": 1},
        "queries_with_axis_likes": 0,
        "heldout": {
            "fraction": 0.5,
            "seed": 1,
            "queries_skipped": {"validation": 0, "test": 0},
        },
        "catalog_mean_popularity": 0.5,
        "rankers": {"r": per_split},
    }
    version = {"dataset": "d", "tracks": 3}
    assert "n/a" in render_report(results, version, date(2026, 1, 1), "q.json")


@pytest.fixture
def data_dir(tmp_path):
    out = tmp_path / "data" / "catalog"
    out.mkdir(parents=True)
    make_frame(per_genre=100).to_parquet(out / "catalog.parquet", index=False)
    return tmp_path / "data"


def test_cli_generate_then_evaluate(tmp_path, data_dir, capsys):
    queries = tmp_path / "q.json"
    base = ["evaluate", "--data-dir", str(data_dir), "--queries", str(queries)]
    assert main([*base, "--generate-queries"]) == 0
    assert queries.exists()
    out = tmp_path / "reports"
    rankers = ["--ranker", "baseline", "--ranker", "random"]
    argv = [*base, "--out-dir", str(out), "--date", "2026-10-05", *rankers]
    assert main(argv) == 0
    assert (out / "offline-2026-10-05.md").exists()
    assert (out / "offline-2026-10-05.json").exists()
    assert "wrote" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--date", "tomorrow"], []])
def test_cli_errors(tmp_path, data_dir, capsys, extra):
    missing = str(tmp_path / "none.json")
    argv = ["evaluate", "--data-dir", str(data_dir), "--queries", missing, *extra]
    assert main(argv) == 1
    assert "evaluate:" in capsys.readouterr().err


def test_cli_missing_catalog(tmp_path, capsys):
    assert main(["evaluate", "--data-dir", str(tmp_path)]) == 1
    assert "no catalog" in capsys.readouterr().err


def test_paths_outside_working_directory_are_refused(
    tmp_path, monkeypatch, catalog, queries
):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    outside = tmp_path / "elsewhere" / "q.json"
    with pytest.raises(EvaluationError, match="outside the working directory"):
        write_queries(outside, queries, catalog)
    with pytest.raises(EvaluationError, match="outside the working directory"):
        load_queries(Path("..") / "q.json", catalog)


# --- held-out tags -----------------------------------------------------------


def make_tagged_frame(per_group=40, seed=3):
    """Two genres x two styles; a track carries 6 of its style's 10 tags."""
    frame = make_frame(per_genre=2 * per_group, seed=seed).sort_values("track_id")
    rng = np.random.default_rng(seed)
    tags = []
    for position, genre in enumerate(frame["genre_top"]):
        style = (position // per_group) % 2
        pool = [f"{genre}-{style}-{n}" for n in range(10)]
        tags.append(sorted(rng.choice(pool, size=6, replace=False).tolist()))
    frame["tags"] = tags
    frame.loc[frame.index[:3], "tags"] = pd.Series(
        [[], ["solo"], []], index=frame.index[:3]
    )
    return frame


@pytest.fixture(scope="module")
def tagged():
    return Catalog(make_tagged_frame())


@pytest.fixture(scope="module")
def tagged_queries(tagged):
    return generate_queries(tagged, per_split=30)


def test_holdout_is_deterministic_disjoint_and_half(tagged):
    a, b = HeldOut(tagged), HeldOut(tagged)
    assert a.visible == b.visible and a.held == b.held
    for tags, visible, held in zip(
        tagged.frame["tags"], a.visible, a.held, strict=True
    ):
        assert visible | held == frozenset(tags)
        assert not visible & held
        if len(tags) >= 2:
            assert len(held) == len(tags) // 2
        else:
            assert not held
    other = HeldOut(tagged, seed=1)
    assert other.held != a.held
    shuffled = Catalog(tagged.frame.sample(frac=1, random_state=4))
    again = HeldOut(shuffled)
    for track_id, held in zip(shuffled.track_ids, again.held, strict=True):
        assert held == a.held[tagged.position[int(track_id)]]
    with pytest.raises(EvaluationError, match="fraction"):
        HeldOut(tagged, fraction=1.0)


class Spy:
    """Wraps a ranker and records everything it is shown."""

    def __init__(self, inner):
        self.inner = inner
        self.catalogs = []
        self.preferences = []

    def rank(self, preference, catalog):
        self.catalogs.append(catalog)
        self.preferences.append(preference)
        return self.inner.rank(preference, catalog)


def test_held_out_tags_are_never_visible_to_a_ranker(
    monkeypatch, tagged, tagged_queries
):
    from musicdiscovery import evaluation

    holdout = HeldOut(tagged)
    original = dict(evaluation.RANKERS)
    spies = {}

    def factory(name):
        def build():
            spies[name] = Spy(original[name]())
            return spies[name]

        return build

    monkeypatch.setattr(evaluation, "RANKERS", {n: factory(n) for n in original})
    evaluate(tagged, tagged_queries, list(original), resamples=10)
    assert set(spies) == set(original)
    blind_runs = 0
    for spy in spies.values():
        for catalog, preference in zip(spy.catalogs, spy.preferences, strict=True):
            if catalog is tagged:
                continue
            blind_runs += 1
            for track_id, tags in zip(
                catalog.track_ids, catalog.frame["tags"], strict=True
            ):
                row = tagged.position[int(track_id)]
                assert set(tags) == holdout.visible[row]
                assert not set(tags) & holdout.held[row]
            seed_row = tagged.position[preference.seeds[0]]
            for quality in preference.likes:
                if quality.kind == "tag":
                    assert quality.name not in holdout.held[seed_row]
    assert blind_runs == len(original) * len(tagged_queries)


def test_full_tags_would_score_higher_than_visible_tags(tagged, tagged_queries):
    holdout = HeldOut(tagged)
    view = holdout.view()
    ranker = make_ranker("hybrid")
    full, blind = [], []
    for query in tagged_queries:
        seed_row = tagged.position[query.seed]
        if not holdout.held[seed_row]:
            continue
        for catalog, scores in ((tagged, full), (view, blind)):
            results = ranker.rank(holdout.preference(query, 10), catalog)
            rows = np.array([catalog.position[r.track_id] for r in results])
            scores.append(holdout.overlap(seed_row, rows))
    assert np.mean(full) > np.mean(blind)


def test_heldout_metric_orders_rankers_and_counts_skips(tagged, tagged_queries):
    results = evaluate(tagged, tagged_queries, ["hybrid", "random"], resamples=100)
    for split in ("validation", "test"):
        hybrid = results["rankers"]["hybrid"][split]["heldout_tags"]
        random = results["rankers"]["random"][split]["heldout_tags"]
        assert hybrid["lo"] <= hybrid["mean"] <= hybrid["hi"]
        assert hybrid["mean"] > random["mean"]
    held = results["heldout"]
    assert held["fraction"] == 0.5
    assert set(held["queries_skipped"]) == {"validation", "test"}
    again = evaluate(tagged, tagged_queries, ["hybrid", "random"], resamples=100)
    assert again == results


def test_overlap_is_nan_without_held_out_tags(tagged):
    holdout = HeldOut(tagged)
    assert np.isnan(holdout.overlap(0, np.array([1, 2])))
    seed = next(i for i, h in enumerate(holdout.held) if h)
    assert np.isnan(holdout.overlap(seed, np.array([], dtype=int)))
    assert holdout.overlap(seed, np.array([seed])) == 1.0


def test_report_states_holdout_and_label_visibility(tmp_path, tagged, tagged_queries):
    path = tmp_path / "queries.json"
    write_queries(path, tagged_queries, tagged)
    md, js = run(tagged, path, ["baseline", "random"], tmp_path, date(2026, 10, 5))
    text = md.read_text()
    assert "## Same-genre rate@10" in text
    assert "## Held-out tag overlap@10" in text
    assert "Genre precision@10" not in text
    assert "50% of each track's tags" in text
    assert "seed 20261006" in text
    assert text.count("which no ranker sees") == 1
    assert "`baseline` (audio features only)" in text
    assert json.loads(js.read_text())["heldout"]["seed"] == 20261006


def test_report_warns_when_tags_are_too_sparse(tmp_path, tagged, tagged_queries):
    path = tmp_path / "queries.json"
    write_queries(path, tagged_queries, tagged)
    sparse = Catalog(tagged.frame.assign(tags=[[] for _ in range(len(tagged))]))
    write_queries(path, generate_queries(sparse, per_split=30), sparse)
    md, _ = run(sparse, path, ["random"], tmp_path, date(2026, 10, 5))
    assert "**Tags are sparse:** only 0 validation and 0 test queries" in md.read_text()
    write_queries(path, generate_queries(tagged, per_split=60), tagged)
    md, _ = run(tagged, path, ["random"], tmp_path, date(2026, 10, 5))
    assert "Tags are sparse" not in md.read_text()
