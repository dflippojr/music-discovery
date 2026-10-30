"""Same commit and catalog, same bytes, whatever each process's string hash is.

The contract's determinism law ranks twice in one process, where the hash seed
is fixed, so it cannot see this. These tests start one process per seed.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_evaluation import make_frame

from musicdiscovery.evaluation import RANKERS

HERE = Path(__file__).resolve().parent
HASH_SEEDS = ("1", "2")
# audio-embedding needs a vectors file, so it stays out; the rest come from
# RANKERS, so a new catalog-only ranker is checked without touching this file.
CATALOG_RANKERS = sorted(set(RANKERS) - {"audio-embedding"})
# `recommend` offers no random or popular choice.
RECOMMEND_RANKERS = sorted(set(CATALOG_RANKERS) - {"random", "popular"})
CLI = "from musicdiscovery.cli import main; raise SystemExit(main())"


def run(hash_seed: str, *argv: str, cwd: Path | None = None) -> str:
    env = {**os.environ, "PYTHONHASHSEED": hash_seed}
    result = subprocess.run(
        [sys.executable, *argv], capture_output=True, text=True, env=env, cwd=cwd
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def cli(hash_seed: str, *argv: str, cwd: Path | None = None) -> str:
    return run(hash_seed, "-c", CLI, *argv, cwd=cwd)


def files(root: Path) -> dict[str, bytes]:
    found = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    assert found
    return found


def write_catalog(root: Path, per_genre: int) -> Path:
    (root / "catalog").mkdir()
    frame = make_frame(per_genre=per_genre)
    frame.to_parquet(root / "catalog" / "catalog.parquet", index=False)
    return root


@pytest.fixture(scope="module")
def data_dir(tmp_path_factory) -> Path:
    return write_catalog(tmp_path_factory.mktemp("hash-seed"), per_genre=60)


def test_metadata_arrays_do_not_depend_on_hash_seed():
    code = (
        "import hashlib; from test_evaluation import make_frame; "
        "from musicdiscovery.catalog import Catalog; "
        "from musicdiscovery.hybrid import Metadata; "
        "print(hashlib.sha1(Metadata(Catalog(make_frame())).columns.tobytes())"
        ".hexdigest())"
    )
    digests = {run(seed, "-c", code, cwd=HERE) for seed in HASH_SEEDS}
    assert len(digests) == 1


def test_export_demo_bundle_does_not_depend_on_hash_seed(data_dir, tmp_path):
    # export-demo writes only inside the working directory.
    bundles = []
    for seed in HASH_SEEDS:
        cli(seed, "export-demo", seed, "--data-dir", str(data_dir), cwd=tmp_path)
        bundle = files(tmp_path / seed)
        # The one field allowed to differ: when the bundle was built.
        stamp = json.loads(bundle.pop("SOURCE.json"))
        assert stamp.pop("built")
        bundles.append((bundle, stamp))
    assert "catalog.json" in bundles[0][0]
    assert bundles[0] == bundles[1]


@pytest.mark.parametrize("ranker", RECOMMEND_RANKERS)
def test_recommend_json_does_not_depend_on_hash_seed(data_dir, ranker):
    argv = ["recommend", "--data-dir", str(data_dir), "--ranker", ranker]
    argv += ["--seed", "100", "--like", "more:energy", "-k", "180", "--json"]
    outputs = {cli(seed, *argv) for seed in HASH_SEEDS}
    assert len(outputs) == 1


def test_evaluate_reports_do_not_depend_on_hash_seed(tmp_path):
    # The query sampler refuses a catalog as small as the one above.
    (tmp_path / "data").mkdir()
    data_dir = write_catalog(tmp_path / "data", per_genre=100)
    # evaluate writes only inside the working directory.
    common = ["--data-dir", str(data_dir), "--benchmark", "all"]
    common += ["--queries", "queries.json", "--profiles", "profiles.json"]
    for ranker in CATALOG_RANKERS:
        common += ["--ranker", ranker]
    cli("1", "evaluate", *common, "--generate-queries", cwd=tmp_path)
    cli("1", "evaluate", *common, "--generate-profiles", cwd=tmp_path)
    reports = []
    for seed in HASH_SEEDS:
        out = f"report-{seed}"
        argv = [*common, "--out-dir", out, "--date", "2026-01-02"]
        cli(seed, "evaluate", *argv, cwd=tmp_path)
        reports.append(files(tmp_path / out))
    assert reports[0] == reports[1]
