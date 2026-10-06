"""Demo bundle export on a synthetic catalog; the JavaScript side is tested in Node."""

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from test_evaluation import make_frame

from musicdiscovery import demo
from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_frame(per_genre=60))


@pytest.fixture(scope="module")
def bundle(tmp_path_factory, catalog):
    out = tmp_path_factory.mktemp("bundle")
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(out)
        size = demo.export_demo(catalog, out)
    return out, size


def ints(blob, dtype):
    return np.frombuffer(base64.b64decode(blob), dtype=dtype)


def test_export_writes_every_file(bundle):
    out, size = bundle
    for name in (
        *demo.STATIC_FILES,
        demo.CATALOG_FILE,
        demo.SOURCES_FILE,
        demo.PARITY_FILE,
    ):
        assert (out / name).is_file()
    assert set(size.files) == {
        *demo.STATIC_FILES,
        demo.CATALOG_FILE,
        demo.SOURCES_FILE,
    }
    assert size.initial + size.deferred == size.compressed
    assert 0 < size.compressed < size.raw < demo.TARGET_BYTES


def test_catalog_arrays_line_up(bundle, catalog):
    data = json.loads((bundle[0] / demo.CATALOG_FILE).read_text())
    n = len(catalog)
    assert data["count"] == n
    assert data["ids"] == [int(t) for t in catalog.track_ids]
    for key in ("titles", "artist", "genre", "license"):
        assert len(data[key]) == n
    assert "sources" not in data
    sources = json.loads((bundle[0] / demo.SOURCES_FILE).read_text())
    assert sources["sources"] == [""] * n  # the synthetic frame has no source URLs
    base = data["baseline"]
    projected = ints(base["projected"], "<i2")
    assert projected.size == n * base["dims"]
    assert ints(base["axisData"], "<i2").size == n * len(data["axes"])
    hybrid = data["hybrid"]
    counts = ints(hybrid["counts"], "<u2")
    assert counts.size == n
    assert ints(hybrid["columns"], "<u2").size == counts.sum()
    assert ints(hybrid["popularity"], "<u2").size == n
    assert "Free Music Archive" in data["attribution"]
    assert set(data["chips"]["genres"]) == set(catalog.genres)


def test_quantised_projection_stays_close(bundle, catalog):
    data = json.loads((bundle[0] / demo.CATALOG_FILE).read_text())
    base = data["baseline"]
    stored = ints(base["projected"], "<i2").reshape(len(catalog), -1)
    fit = demo.ranking.BaselineRanker()._fitted(catalog)
    assert np.abs(stored * base["projectedScale"] - fit.projected).max() < 1e-3


def test_parity_file_matches_the_rankers(bundle, catalog):
    parity = json.loads((bundle[0] / demo.PARITY_FILE).read_text())
    assert len(parity["queries"]) >= 20
    assert any(q["dislikes"] for q in parity["queries"])
    for query in parity["queries"]:
        assert query["seed"] in catalog.position
        for ids in query["expected"].values():
            assert len(ids) == 10
            assert query["seed"] not in ids


def test_static_files_follow_the_csp_rules(bundle):
    out = bundle[0]
    for name, script in (("index.html", "demo.js"), ("study.html", "study.js")):
        page = (out / name).read_text()
        assert "<script>" not in page and "<style" not in page
        assert " style=" not in page and " onclick=" not in page
        assert f'src="{script}"' in page and 'href="demo.css"' in page
    for name in (n for n in demo.STATIC_FILES if n.endswith(".js")):
        script = (out / name).read_text("utf-8")
        for forbidden in (".innerHTML", "eval(", 'setAttribute("style"', 'fetch("http'):
            assert forbidden not in script, name
        urls = re.findall(r"https?://[^\"' ]+", script)
        # at most the SVG namespace, which is not a request
        assert urls in ([], ["http://www.w3.org/2000/svg"]), name
    assert "https://" not in (out / "demo.css").read_text()


def test_cli_exports_and_reports_size(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).to_parquet(data / "catalog.parquet", index=False)
    out = tmp_path / "site"
    assert main(["export-demo", str(out), "--data-dir", str(tmp_path / "data")]) == 0
    said = capsys.readouterr().out
    assert "KiB compressed" in said and "catalog.json" in said
    assert (out / "catalog.json").is_file()


def test_cli_refuses_to_write_outside_the_working_directory(
    tmp_path, capsys, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).to_parquet(data / "catalog.parquet", index=False)
    argv = [
        "export-demo",
        str(tmp_path / "elsewhere"),
        "--data-dir",
        str(tmp_path / "data"),
    ]
    assert main(argv) == 1
    assert "outside the working directory" in capsys.readouterr().err
    assert not (tmp_path / "elsewhere").exists()


def test_cli_reports_a_missing_catalog(tmp_path, capsys):
    assert main(["export-demo", str(tmp_path / "x"), "--data-dir", str(tmp_path)]) == 1
    assert "export-demo:" in capsys.readouterr().err


def test_cli_fails_when_over_the_size_target(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).to_parquet(data / "catalog.parquet", index=False)
    monkeypatch.setattr("musicdiscovery.cli.TARGET_BYTES", 10)
    assert (
        main(["export-demo", str(tmp_path / "o"), "--data-dir", str(tmp_path / "data")])
        == 1
    )
    assert "over the size target" in capsys.readouterr().err


def test_table_and_text_helpers():
    assert demo._table(["b", "a", "b"]) == (["a", "b"], [1, 0, 1])
    assert demo._quantise(np.array([0.0, 0.0]), 0.0)[1] == 1.0


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX shell")
def test_vendor_script_copies_runtime_files(bundle, tmp_path):
    script = ROOT / "scripts" / "vendor-into-site"
    target = tmp_path / "site" / "demo"
    run = subprocess.run(
        ["sh", str(script), str(bundle[0]), str(target)], capture_output=True, text=True
    )
    assert run.returncode == 0, run.stderr
    assert sorted(p.name for p in target.iterdir()) == sorted(
        [*demo.STATIC_FILES, demo.CATALOG_FILE, demo.SOURCES_FILE]
    )
    empty = subprocess.run(
        ["sh", str(script), str(tmp_path), str(tmp_path / "t")],
        capture_output=True,
        text=True,
    )
    assert empty.returncode == 1 and "missing" in empty.stderr
    usage = subprocess.run(["sh", str(script)], capture_output=True, text=True)
    assert usage.returncode == 2
