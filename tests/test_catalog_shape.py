"""The real code paths on one synthetic catalog shaped like the real FMA catalog."""

import json
import os
import shutil
import subprocess
import sys
import sysconfig
from contextlib import chdir
from copy import deepcopy
from pathlib import Path

import pytest
from catalog_factory import EDGE_FIELDS, from_shape, load_shape, vectors_for

from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from profile_catalog import SCRIPTS, profile, title_scripts  # noqa: E402

TRACKS = 400
SHAPE = load_shape()


@pytest.fixture(scope="module")
def frame():
    return from_shape(SHAPE, TRACKS)


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory, frame):
    root = tmp_path_factory.mktemp("shape")
    (root / "catalog").mkdir()
    frame.to_parquet(root / "catalog" / "catalog.parquet", index=False)
    store, missing = vectors_for(frame, SHAPE)
    with chdir(root):  # the cache must live inside the working directory
        store.save(Path("vectors.npz"))
    (root / "no_vector.json").write_text(json.dumps(missing))
    return root


@pytest.fixture(autouse=True)
def in_data_dir(data_dir, monkeypatch):
    monkeypatch.chdir(data_dir)


def _rows(frame, column, test):
    return frame[frame[column].map(test)]["track_id"].tolist()


def test_profile_holds_aggregates_only():
    """Every string in the committed profile is a key, a genre or a schema name."""
    allowed_values = {*SHAPE["source"].values(), *SHAPE["features"]["statistics"]}

    def values(node):
        if isinstance(node, dict):
            for value in node.values():
                yield from values(value)
        elif isinstance(node, list):
            for value in node:
                yield from values(value)
        else:
            yield node

    for value in values(SHAPE):
        assert (
            isinstance(value, int | float) or value is None or value in allowed_values
        )
    assert all(isinstance(v, int | float) for v in SHAPE["genres"].values())
    for share in (*SHAPE["missing"].values(), *SHAPE["tag_count"].values()):
        assert 0 <= share <= 1


def test_profile_leaks_no_row_values(frame):
    text = json.dumps(profile(frame), ensure_ascii=False)
    for column in ("title", "artist", "album"):
        assert not any(f'"{v}"' in text for v in frame[column].dropna())
    assert not any(f'"{t}"' in text for row in frame["tags"] for t in row)
    assert not any(f'"{t}"' in text for t in frame["track_id"])  # never a key


def test_generated_catalog_matches_the_profile():
    shaped = profile(from_shape(SHAPE, 2000, seed=1))
    for name, share in SHAPE["missing"].items():
        assert shaped["missing"][name] == pytest.approx(share, abs=0.01), name
    for genre, share in SHAPE["genres"].items():
        assert shaped["genres"][genre] == pytest.approx(share, abs=0.02), genre
    for bucket, share in SHAPE["tag_count"].items():
        assert shaped["tag_count"][bucket] == pytest.approx(share, abs=0.02), bucket
    title = shaped["title"]
    assert title["non_ascii"] == pytest.approx(SHAPE["title"]["non_ascii"], abs=0.01)
    assert title["duplicate_rate"] == pytest.approx(
        SHAPE["title"]["duplicate_rate"], abs=0.02
    )
    for q in ("0.25", "0.5", "0.75"):
        expected = SHAPE["title"]["length_quantiles"][q]
        assert title["length_quantiles"][q] == pytest.approx(expected, abs=2), q


def test_every_edge_case_occurs(frame, data_dir):
    for name in EDGE_FIELDS:
        if name in ("genre_ids", "tags"):
            assert frame[name].map(len).eq(0).any(), name
        else:
            assert frame[name].isna().any(), name
    found = {s for title in frame["title"].dropna() for s in title_scripts(title)}
    expected = {s for s in SCRIPTS if SHAPE["title"]["scripts"][s] > 0}
    assert expected <= found
    assert json.loads((data_dir / "no_vector.json").read_text())
    assert frame["tags"].map(len).ge(2).any()


@pytest.mark.parametrize("tag_count", [2, 10, 15])
def test_generated_tags_preserve_sampled_counts(tag_count):
    shape = deepcopy(SHAPE)
    shape["tag_count"] = {str(tag_count): 1.0}
    shape["missing"]["tags"] = 0.0
    # A small vocabulary still needs enough distinct tags for the sampled count.
    shape["distinct_tags_per_track"] = 0.0
    frame = from_shape(shape, 100)
    lengths = frame["tags"].map(len)
    assert lengths.eq(0).sum() == 1  # the forced missing-tags edge case
    assert lengths[lengths.gt(0)].eq(tag_count).all()
    assert all(len(tags) == len(set(tags)) for tags in frame["tags"])


@pytest.mark.parametrize("share", [0.0, 0.25, 1.0])
def test_generated_catalog_matches_missing_feature_share(share):
    shape = deepcopy(SHAPE)
    shape["audio"]["without_features"] = share
    frame = from_shape(shape, 100)
    assert profile(frame)["audio"]["without_features"] == share


def recommend(data_dir, capsys, *argv):
    code = main(["recommend", "--data-dir", str(data_dir), "-k", "5", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.mark.parametrize("ranker", ["baseline", "hybrid", "profile", "profile-diverse"])
def test_recommend_from_messy_seeds(frame, data_dir, capsys, ranker):
    seeds = [
        _rows(frame, "genre_top", lambda g: g is None)[0],
        _rows(frame, "title", lambda t: isinstance(t, str) and not t.isascii())[0],
        _rows(frame, "tags", lambda t: len(t) == 0)[0],
    ]
    for seed in seeds:
        argv = ["--ranker", ranker, "--seed", str(seed), "--json"]
        code, out, err = recommend(data_dir, capsys, *argv)
        assert code == 0, err
        results = json.loads(out)
        assert len(results) == 5
        assert seed not in {r["track_id"] for r in results}


def test_recommend_by_audio_with_a_seed_that_has_no_vector(frame, data_dir, capsys):
    missing = json.loads((data_dir / "no_vector.json").read_text())
    vectors = "vectors.npz"
    argv = ["--ranker", "audio-embedding", "--vectors", vectors, "--json"]
    code, out, err = recommend(data_dir, capsys, *argv, "--seed", str(missing[0]))
    assert code == 0
    assert json.loads(out) == []
    assert "no seed has an audio vector" in err

    other = int(frame["track_id"][~frame["track_id"].isin(missing)].iloc[0])
    code, out, _ = recommend(data_dir, capsys, *argv, "--seed", str(other))
    assert code == 0
    assert len(json.loads(out)) == 5


@pytest.mark.parametrize("separate_scripts_dir", [False, True])
def test_recommend_prints_non_latin_names_on_a_legacy_console(
    frame, data_dir, monkeypatch, separate_scripts_dir
):
    if separate_scripts_dir:
        # A Windows base install keeps python.exe outside its scripts directory.
        executable = Path(sys.executable)
        monkeypatch.setattr(
            sys, "executable", str(executable.parent.parent / executable.name)
        )
    seed = _rows(frame, "title", lambda t: isinstance(t, str) and not t.isascii())[0]
    argv = ["recommend", "--data-dir", str(data_dir), "--seed", str(seed)]
    script = shutil.which("musicdiscovery", path=sysconfig.get_path("scripts"))
    assert script is not None
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    run = subprocess.run(
        [script, *argv],
        capture_output=True,
        env=env,
        check=False,
    )
    assert run.returncode == 0, run.stderr.decode(errors="replace")
    assert b"(id " in run.stdout


def test_evaluate_and_export_demo(data_dir, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    queries = tmp_path / "queries.json"
    common = ["--data-dir", str(data_dir), "--queries", str(queries)]
    assert main(["evaluate", *common, "--generate-queries"]) == 0
    argv = ["evaluate", *common, "--out-dir", "reports", "--date", "2026-01-02"]
    for name in ("baseline", "hybrid", "popular"):
        argv += ["--ranker", name]
    assert main(argv) == 0, capsys.readouterr().err
    numbers = json.loads((tmp_path / "reports" / "offline-2026-01-02.json").read_text())
    assert numbers

    assert main(["export-demo", "bundle", "--data-dir", str(data_dir)]) == 0
    catalog = json.loads((tmp_path / "bundle" / "catalog.json").read_text())
    assert catalog["count"] == TRACKS
    assert -1 in catalog["genre"]
    assert any(not title.isascii() for title in catalog["titles"])
    assert len(Catalog.load(data_dir / "catalog" / "catalog.parquet")) == TRACKS
