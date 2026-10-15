"""One `recommend` journey through the CLI for every catalog-only ranker."""

import json

import pytest
from test_evaluation import make_frame

from musicdiscovery.cli import main
from musicdiscovery.evaluation import RANKERS

SEEDS = (100, 101)
NAMES = ["baseline", "hybrid", "profile", "profile-diverse"]


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory):
    root = tmp_path_factory.mktemp("recommend")
    out = root / "catalog"
    out.mkdir()
    make_frame().to_parquet(out / "catalog.parquet", index=False)
    return root


def recommend(data_dir, capsys, name, *extra):
    argv = ["recommend", "--data-dir", str(data_dir), "--ranker", name]
    for seed in SEEDS:
        argv += ["--seed", str(seed)]
    assert main([*argv, "-k", "5", "--json", *extra]) == 0
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("name", NAMES)
def test_recommend_every_ranker(data_dir, capsys, name):
    assert name in RANKERS
    results = recommend(data_dir, capsys, name)
    assert len(results) == 5
    assert not {r["track_id"] for r in results} & set(SEEDS)
    assert all(r["explanations"] for r in results)

    kept = recommend(data_dir, capsys, name, "--exclude-seed-artist")
    assert len(kept) == 5
    assert all(r["artist"] not in {"artist0", "artist1"} for r in kept)
