"""`scripts/profile_catalog.py` main() on a small synthetic catalog."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest
from catalog_factory import from_shape, load_shape

from musicdiscovery.audio_embedding import VectorStore
from musicdiscovery.catalog import METADATA_COLUMNS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from profile_catalog import main, profile  # noqa: E402

TRACKS = 40
MANIFEST = {"subset": "small", "source_sha1": "abc123", "ingest_version": 3}


@pytest.fixture
def frame():
    return from_shape(load_shape(), TRACKS)


@pytest.fixture
def data_dir(tmp_path, frame, monkeypatch):
    """A catalog under `tmp_path`, the working directory the vector cache needs."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "catalog").mkdir()
    frame.to_parquet(tmp_path / "catalog" / "catalog.parquet", index=False)
    return tmp_path


def _write_half_vectors(frame, path: Path) -> None:
    ids = frame["track_id"].to_numpy()[: TRACKS // 2]
    vectors = np.ones((len(ids), 4), dtype=np.float32)
    VectorStore(ids, vectors).save(path)


def _run(*argv) -> int:
    return main([str(a) for a in argv])


@pytest.mark.parametrize("content", ["not an npz archive", ""])
def test_main_reports_an_unreadable_vector_cache(data_dir, capsys, content):
    bad = data_dir / "bad.npz"
    bad.write_text(content)
    out = data_dir / "out.json"

    assert _run("--data-dir", data_dir, "--vectors", bad, "--out", out) == 1

    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert err[0].startswith("profile_catalog: cannot read audio vectors")
    assert not out.exists()


def test_main_reports_missing_inputs(data_dir, tmp_path, capsys):
    out = data_dir / "out.json"
    missing = data_dir / "missing.npz"
    assert _run("--data-dir", data_dir, "--vectors", missing, "--out", out) == 1
    assert "no vector cache at" in capsys.readouterr().err

    empty = tmp_path / "empty"
    empty.mkdir()
    assert _run("--data-dir", empty, "--out", out) == 1
    assert capsys.readouterr().err.startswith("profile_catalog: no catalog")
    assert not out.exists()


def test_main_writes_a_shape_that_round_trips(data_dir, frame, capsys):
    (data_dir / "catalog" / "catalog.json").write_text(
        json.dumps({**MANIFEST, "tracks": TRACKS})
    )
    vectors = data_dir / "vectors.npz"
    _write_half_vectors(frame, vectors)
    out = data_dir / "fixtures" / "shape.json"

    assert _run("--data-dir", data_dir, "--vectors", vectors, "--out", out) == 0

    assert f"wrote the shape of {TRACKS} tracks" in capsys.readouterr().out
    shape = load_shape(out)
    assert shape["tracks"] == TRACKS
    assert shape["audio"]["without_vector"] == pytest.approx(0.5)
    assert shape["source"] == MANIFEST


def test_main_uses_the_default_cache_when_present(data_dir, frame):
    _write_half_vectors(frame, data_dir / "embeddings" / "embeddinggemma-2.npz")
    out = data_dir / "shape.json"

    assert _run("--data-dir", data_dir, "--out", out) == 0
    assert load_shape(out)["audio"]["without_vector"] == pytest.approx(0.5)


@pytest.mark.parametrize("manifest", [None, "{not json", "[]"])
def test_source_falls_back_on_a_bad_manifest(data_dir, manifest):
    if manifest is not None:
        (data_dir / "catalog" / "catalog.json").write_text(manifest)
    out = data_dir / "shape.json"

    assert _run("--data-dir", data_dir, "--out", out) == 0
    shape = load_shape(out)
    assert shape["source"] == {}
    assert shape["audio"]["without_vector"] is None


def test_feature_schema_needs_every_statistic(frame):
    features = [c for c in frame.columns if c not in METADATA_COLUMNS]
    feature = features[0].rsplit("_", 2)[0]
    stat = features[0].rsplit("_", 2)[1]
    dropped = [c for c in features if c.startswith(f"{feature}_{stat}_")]

    with pytest.raises(ValueError, match="does not have every statistic once"):
        profile(frame.drop(columns=dropped))


def test_profile_holds_no_row_values(data_dir, frame):
    out = data_dir / "shape.json"
    assert _run("--data-dir", data_dir, "--out", out) == 0
    text = out.read_text(encoding="utf-8")

    for track_id in frame["track_id"]:
        assert str(int(track_id)) not in text
    for value in [*frame["title"].dropna(), *frame["artist"].dropna()]:
        assert json.dumps(str(value), ensure_ascii=False) not in text
