"""Offline evaluation on a synthetic catalog with planted structure."""

import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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
    catalog_version,
    evaluate,
    evaluate_profiles,
    generate_queries,
    load_profiles,
    load_queries,
    make_ranker,
    query_metrics,
    render_profiles_section,
    render_report,
    run,
    write_profiles,
    write_queries,
)
from musicdiscovery.preference import Preference, PreferenceError
from musicdiscovery.profiles import (
    INTERACTIONS_PER_PROFILE,
    PROFILE_SEED,
    WEAK_SIGNAL_THRESHOLD,
    Interaction,
    Profile,
    ProfileError,
    _split,
    generate_profiles,
    ndcg,
    reciprocal_rank,
)

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


def test_write_queries_refuses_a_file_made_for_another_catalog(
    tmp_path, catalog, queries
):
    path = tmp_path / "queries.json"
    path.write_text(json.dumps({"catalog_tracks": 99_999, "queries": []}))
    before = path.read_bytes()
    with pytest.raises(EvaluationError, match=rf"99999.*{len(catalog)}") as error:
        write_queries(path, queries, catalog)
    assert str(path) in str(error.value)
    assert path.read_bytes() == before


def test_write_queries_replaces_a_file_for_this_catalog_or_no_size(
    tmp_path, catalog, queries
):
    path = tmp_path / "queries.json"
    for stored in [{"catalog_tracks": len(catalog), "queries": []}, {"queries": []}]:
        path.write_text(json.dumps(stored))
        write_queries(path, queries, catalog)
        assert load_queries(path, catalog) == queries
    path.write_text("not json")
    write_queries(path, queries, catalog)
    assert load_queries(path, catalog) == queries


JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=5), inner, max_size=3)
    ),
    max_leaves=8,
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(like=JSON_VALUES)
def test_any_json_like_fails_only_as_an_evaluation_error(catalog, queries, like):
    # Whatever JSON sits in a like, loading and preparing the query raises
    # EvaluationError or PreferenceError and nothing else, so no traceback.
    write_queries(Path("q.json"), queries, catalog)
    payload = json.loads(Path("q.json").read_text())
    payload["queries"][0]["likes"] = [like]
    Path("q.json").write_text(json.dumps(payload))
    try:
        for query in load_queries(Path("q.json"), catalog):
            query.preference()
    except (EvaluationError, PreferenceError):
        return


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


def test_missing_genre_is_a_miss_and_a_genreless_seed_is_nan():
    frame = make_frame()
    frame["genre_top"] = frame["genre_top"].astype(object)
    frame.loc[frame.index[:5], "genre_top"] = None
    cat = Catalog(frame)
    ctx = Context(cat)
    genreless = [i for i, g in enumerate(ctx.genres) if not g]
    assert len(genreless) == 5
    seed_row = next(i for i, g in enumerate(ctx.genres) if g)
    seed_genre = ctx.genres[seed_row]
    same = [i for i, g in enumerate(ctx.genres) if g == seed_genre and i != seed_row][
        :5
    ]
    query = Query(0, "test", int(cat.track_ids[seed_row]), ())
    metrics = query_metrics(ctx, query, _bare(cat, same + genreless[:5]))[0]
    assert metrics["genre_precision"] == 0.5
    seedless = Query(1, "test", int(cat.track_ids[genreless[0]]), ())
    metrics = query_metrics(ctx, seedless, _bare(cat, same))[0]
    assert np.isnan(metrics["genre_precision"])
    assert np.isnan(metrics["genre_ndcg"])

    queries = generate_queries(cat, per_split=20)
    seeds = {int(cat.track_ids[i]) for i in genreless}
    assert seeds & {q.seed for q in queries}
    report = evaluate(cat, queries, ["baseline", "hybrid"], resamples=50)
    assert set(report["rankers"]) >= {"baseline", "hybrid"}


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


@pytest.mark.parametrize("body", ["[]", "null", '"abc"', "5", "{not json"])
def test_a_damaged_manifest_is_reported_not_a_crash(tmp_path, catalog, body):
    manifest = tmp_path / "catalog.json"
    manifest.write_text(body, encoding="utf-8")
    with pytest.raises(EvaluationError, match="catalog.json"):
        catalog_version(manifest, catalog)


def test_cli_reports_a_damaged_manifest(tmp_path, data_dir, catalog, queries, capsys):
    path = tmp_path / "queries.json"
    write_queries(path, queries, catalog)
    (data_dir / "catalog" / "catalog.json").write_text("[]", encoding="utf-8")
    argv = ["evaluate", "--data-dir", str(data_dir), "--queries", str(path)]
    assert main([*argv, "--ranker", "baseline"]) == 1
    err = capsys.readouterr().err
    assert "catalog.json is not an ingest manifest" in err
    assert "Traceback" not in err


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


@pytest.mark.parametrize("extra", [["--date", "tomorrow"], []])
def test_cli_errors(tmp_path, data_dir, capsys, extra):
    missing = str(tmp_path / "none.json")
    argv = ["evaluate", "--data-dir", str(data_dir), "--queries", missing, *extra]
    assert main(argv) == 1
    assert "evaluate:" in capsys.readouterr().err


def test_cli_missing_catalog(tmp_path, capsys):
    assert main(["evaluate", "--data-dir", str(tmp_path)]) == 1
    assert "no catalog" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra",
    [
        ["--queries", "q.json", "--out-dir", "afile/sub", "--ranker", "random"],
        ["--queries", "afile/q.json", "--generate-queries"],
        ["--profiles", "afile/p.json", "--generate-profiles"],
    ],
    ids=["out-dir", "queries", "profiles"],
)
def test_cli_reports_unwritable_paths(tmp_path, data_dir, capsys, extra):
    # A path below a regular file fails on every OS, unlike chmod.
    (tmp_path / "afile").write_text("")
    base = ["evaluate", "--data-dir", str(data_dir)]
    assert main([*base, "--queries", "q.json", "--generate-queries"]) == 0
    capsys.readouterr()
    assert main([*base, *extra]) == 1
    err = capsys.readouterr().err
    assert err.startswith("evaluate: ")
    assert err.count("\n") == 1
    assert "Traceback" not in err


@pytest.mark.parametrize(
    "likes",
    [[5], [None], [["x"]], [{}], "more:energy"],
    ids=["number", "null", "nested-list", "object", "text"],
)
def test_cli_refuses_likes_that_are_not_a_list_of_strings(data_dir, capsys, likes):
    base = ["evaluate", "--data-dir", str(data_dir)]
    assert main([*base, "--queries", "q.json", "--generate-queries"]) == 0
    payload = json.loads(Path("q.json").read_text())
    payload["queries"][0]["likes"] = likes
    Path("bad.json").write_text(json.dumps(payload))
    capsys.readouterr()
    argv = [*base, "--queries", "bad.json", "--out-dir", "out", "--ranker", "random"]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("evaluate: cannot read queries at ")
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_cli_refuses_to_replace_queries_for_another_catalog(data_dir, capsys):
    Path("q.json").write_text(json.dumps({"catalog_tracks": 99_999, "queries": []}))
    base = ["evaluate", "--data-dir", str(data_dir)]
    argv = [*base, "--queries", "q.json", "--generate-queries"]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert err.startswith("evaluate: ")
    assert "99999" in err
    assert "300" in err
    assert err.count("\n") == 1
    assert "Traceback" not in err
    assert json.loads(Path("q.json").read_text())["catalog_tracks"] == 99_999


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
    from test_audio_embedding import store_for

    from musicdiscovery import evaluation
    from musicdiscovery.audio_embedding import DEFAULT_VECTORS

    store_for(tagged).save(DEFAULT_VECTORS)
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


# --- held-out preferences ----------------------------------------------------


def test_reciprocal_rank_on_hand_built_rankings():
    positives = frozenset({7})
    assert reciprocal_rank([7, 1, 2], positives, 10) == 1.0
    assert reciprocal_rank([*range(100, 109), 7], positives, 10) == pytest.approx(0.1)
    assert reciprocal_rank([*range(100, 110), 7], positives, 10) == 0.0
    assert reciprocal_rank([1, 2, 3], positives, 10) == 0.0
    assert reciprocal_rank([1, 7, 8], frozenset({7, 8}), 10) == 0.5


def test_ndcg_with_several_positives():
    positives = frozenset({1, 2, 3})
    ideal = 1 + 1 / np.log2(3) + 1 / np.log2(4)
    assert ndcg([1, 9, 2], positives, 10) == pytest.approx((1 + 0.5) / ideal)
    assert ndcg([3, 2, 1, 9], positives, 10) == pytest.approx(1.0)
    assert ndcg([9, 8], positives, 10) == 0.0
    # Only k positives fit in the list, so a top-k list of positives is ideal.
    assert ndcg([1, 2], positives, 2) == pytest.approx(1.0)
    with pytest.raises(ProfileError):
        ndcg([1], frozenset(), 10)


@pytest.fixture(scope="module")
def profiles(catalog):
    return generate_profiles(catalog, per_split=15)


def test_profiles_are_deterministic_and_split(catalog, profiles):
    assert generate_profiles(catalog, per_split=15) == profiles
    assert generate_profiles(catalog, per_split=15, seed=1) != profiles
    assert [p.split for p in profiles].count("validation") == 15
    assert [p.split for p in profiles].count("test") == 15
    for profile in profiles:
        history = {i.track for i in profile.history}
        heldout = {i.track for i in profile.heldout}
        assert not history & heldout
        assert len(history) + len(heldout) == INTERACTIONS_PER_PROFILE
        assert (history | heldout) <= set(catalog.position)
        assert profile.seeds() and profile.positives()
        assert len(profile.tags) == 2  # the fixture has two tags per genre
        assert all(0 <= i.strength <= 1 for i in (*profile.history, *profile.heldout))
    with pytest.raises(ProfileError, match="too small"):
        generate_profiles(catalog, interactions=len(catalog) + 1)
    unlabelled = Catalog(catalog.frame.assign(genre_top=None))
    with pytest.raises(ProfileError, match="no top genres"):
        generate_profiles(unlabelled, per_split=1)


def _profile(history, heldout, split="test", index=0):
    return Profile(
        id=index,
        split=split,
        genre="Folk",
        tags=(),
        axis=None,
        history=tuple(Interaction(t, s) for t, s in history),
        heldout=tuple(Interaction(t, s) for t, s in heldout),
    )


def test_weak_signals_leave_history_and_positives():
    profile = _profile([(100, 0.9), (101, 0.1)], [(102, 0.8), (103, 0.29)])
    assert profile.seeds() == (100,)
    assert profile.positives() == frozenset({102})
    assert profile.preference(10).seeds == (100,)
    assert profile.positives(threshold=0.2) == frozenset({102, 103})
    with pytest.raises(ProfileError, match="no history"):
        _profile([(101, 0.1)], [(102, 0.8)]).preference(10)


class Fixed:
    """A ranker that returns fixed tracks, minus the seeds, and records the seeds."""

    def __init__(self, tracks, drop_seeds=True):
        self.tracks = tracks
        self.drop_seeds = drop_seeds
        self.seen = []

    def rank(self, preference, catalog):
        self.seen.append(preference.seeds)
        rows = [catalog.position[t] for t in self.tracks]
        return [
            r
            for r in _bare(catalog, rows)
            if not self.drop_seeds or r.track_id not in preference.seeds
        ]


def test_evaluation_drops_weak_signals(monkeypatch, catalog):
    from musicdiscovery import evaluation

    # 103 is a weak held-out play, so ranking it first is not a hit.
    fixed = Fixed([103, 104, 102])
    monkeypatch.setattr(evaluation, "RANKERS", {"fixed": lambda: fixed})
    profiles = [
        _profile([(100, 0.9), (101, 0.1)], [(102, 0.8), (103, 0.2)], split, i)
        for i, split in enumerate(["validation", "test"])
    ]
    section = evaluate_profiles(catalog, profiles, ["fixed"], resamples=10)
    assert fixed.seen == [(100,), (100,)]
    scores = section["rankers"]["fixed"]["test"]
    assert scores["mrr"]["mean"] == pytest.approx(1 / 3)
    assert scores["ndcg"]["mean"] == pytest.approx(0.5)
    assert section["weak_signal_threshold"] == WEAK_SIGNAL_THRESHOLD
    assert section["weak_history_dropped"] == {"validation": 1, "test": 1}
    assert section["weak_heldout_dropped"] == {"validation": 1, "test": 1}
    assert section["positives"] == {"validation": 1, "test": 1}


def test_evaluation_refuses_unusable_profiles(monkeypatch, catalog):
    from musicdiscovery import evaluation

    leaky = Fixed([100, 102], drop_seeds=False)
    monkeypatch.setattr(evaluation, "RANKERS", {"fixed": lambda: leaky})
    profile = [_profile([(100, 0.9)], [(102, 0.8)])]
    with pytest.raises(EvaluationError, match="history tracks"):
        evaluate_profiles(catalog, profile, ["fixed"], resamples=10)
    weak = [_profile([(100, 0.9)], [(102, 0.1)])]
    with pytest.raises(EvaluationError, match="no held-out track"):
        evaluate_profiles(catalog, weak, ["fixed"], resamples=10)
    empty = [_profile([(100, 0.1)], [(102, 0.9)])]
    with pytest.raises(EvaluationError, match="no history"):
        evaluate_profiles(catalog, empty, ["fixed"], resamples=10)


def test_profile_ranking_orders_rankers(catalog, profiles):
    section = evaluate_profiles(
        catalog, profiles, ["baseline", "hybrid", "random"], resamples=100
    )
    rankers = section["rankers"]
    for split in ("validation", "test"):
        for metric in ("mrr", "ndcg"):
            random = rankers["random"][split][metric]["mean"]
            for name in ("baseline", "hybrid"):
                score = rankers[name][split][metric]
                assert score["lo"] <= score["mean"] <= score["hi"]
                assert score["mean"] > random, (name, split, metric)
    assert section["profiles"] == {"validation": 15, "test": 15}


def test_profile_file_round_trip_and_errors(tmp_path, catalog, profiles):
    path = tmp_path / "profiles.json"
    write_profiles(path, profiles, catalog)
    assert load_profiles(path, catalog) == profiles
    smaller = Catalog(make_frame(per_genre=30))
    with pytest.raises(EvaluationError, match="catalog of"):
        load_profiles(path, smaller)
    with pytest.raises(EvaluationError, match="remove it"):
        write_profiles(path, generate_profiles(smaller, per_split=5), smaller)
    with pytest.raises(EvaluationError, match="no profiles"):
        load_profiles(tmp_path / "missing.json", catalog)
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    with pytest.raises(EvaluationError, match="cannot read"):
        load_profiles(bad, catalog)
    write_profiles(bad, profiles, catalog)  # a file without a size may be replaced
    bad.write_text("not json")
    write_profiles(bad, profiles, catalog)
    payload = json.loads(path.read_text())
    payload["profiles"][0]["heldout"][0][0] = 99_999
    path.write_text(json.dumps(payload))
    with pytest.raises(EvaluationError, match="missing from the catalog"):
        load_profiles(path, catalog)
    payload["profiles"] = [p for p in payload["profiles"] if p["split"] == "test"]
    path.write_text(json.dumps(payload))
    with pytest.raises(EvaluationError, match="no validation"):
        load_profiles(path, catalog)
    payload["profiles"][0]["split"] = "train"
    payload["profiles"].append({**payload["profiles"][1], "split": "validation"})
    path.write_text(json.dumps(payload))
    with pytest.raises(EvaluationError, match="splits must be"):
        load_profiles(path, catalog)


def test_split_needs_a_strong_play_on_each_side():
    weak = [Interaction(i, 0.1) for i in range(10)]
    with pytest.raises(ProfileError, match="strong play"):
        _split(np.random.default_rng(0), weak)


def test_cli_profile_benchmark_end_to_end(tmp_path, data_dir, capsys):
    out = tmp_path / "reports"
    base = [
        "evaluate",
        "--data-dir",
        str(data_dir),
        "--queries",
        "q.json",
        "--profiles",
        "p.json",
        "--out-dir",
        str(out),
        "--ranker",
        "baseline",
        "--ranker",
        "hybrid",
        "--ranker",
        "profile",
        "--ranker",
        "random",
    ]
    assert main([*base, "--generate-queries"]) == 0
    assert main([*base, "--generate-profiles"]) == 0

    assert main([*base, "--date", "2026-10-01", "--benchmark", "profiles"]) == 0
    assert "wrote" in capsys.readouterr().out
    text = (out / "offline-2026-10-01.md").read_text()
    assert "## Held-out preferences" in text
    assert "### MRR@10" in text
    assert "### nDCG@10" in text
    assert text.count("| baseline |") == 2
    assert "| hybrid |" in text
    assert text.count("| profile |") == 2
    assert f"below {WEAK_SIGNAL_THRESHOLD} are dropped" in text
    assert f"profile seed {PROFILE_SEED}" in text
    assert "Synthetic tastes reward rankers that share the generator's" in text
    assert "## Same-genre rate@10" not in text
    numbers = json.loads((out / "offline-2026-10-01.json").read_text())
    section = numbers["held_out_preferences"]
    assert section["weak_signal_threshold"] == WEAK_SIGNAL_THRESHOLD
    assert section["profiles_path"] == "p.json"
    assert len(section["profiles_sha256"]) == 64
    assert "queries" not in numbers
    assert set(section["rankers"]) == {"baseline", "random", "hybrid", "profile"}
    assert section["rankers"]["profile"]["test"]["mrr"]["lo"] is not None

    assert main([*base, "--date", "2026-10-02", "--benchmark", "all"]) == 0
    text = (out / "offline-2026-10-02.md").read_text()
    assert "## Same-genre rate@10" in text
    assert text.index("## Held-out preferences") < text.index("## Limits")
    # One row per query metric plus MRR and nDCG in the held-out section.
    assert text.count("| profile |") == 8 + 2
    both = json.loads((out / "offline-2026-10-02.json").read_text())
    assert both["held_out_preferences"]["rankers"] == section["rankers"]

    # Without --benchmark the report is the query report alone.
    assert main([*base, "--date", "2026-10-03"]) == 0
    default = (out / "offline-2026-10-03.md").read_text()
    assert "Held-out preferences" not in default
    numbers = json.loads((out / "offline-2026-10-03.json").read_text())
    assert "held_out_preferences" not in numbers
    del both["held_out_preferences"]
    assert numbers == both
    added = "\n".join(render_profiles_section(section)) + "\n"
    assert default == text.replace("2026-10-02", "2026-10-03").replace(added, "")


def test_unknown_benchmark_is_refused(tmp_path, catalog):
    with pytest.raises(EvaluationError, match="unknown benchmark"):
        run(catalog, Path("q.json"), ["random"], tmp_path, date.today(), None, "x")


def test_stored_file_hashes_ignore_the_checkout_line_endings(tmp_path):
    # eval/queries.json is CRLF on a Windows checkout and LF on Linux; the hash
    # in a report must not depend on which one wrote it (#105).
    from musicdiscovery.evaluation import _sha256

    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    lf.write_bytes(b'{\n  "a": 1\n}\n')
    crlf.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    assert _sha256(lf) == _sha256(crlf)
