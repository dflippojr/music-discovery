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
from musicdiscovery.evaluation import EvaluationError

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
        demo.SOURCE_FILE,
    ):
        assert (out / name).is_file()
    assert set(size.files) == {
        *demo.STATIC_FILES,
        demo.CATALOG_FILE,
        demo.SOURCES_FILE,
    }
    assert size.initial + size.deferred == size.compressed
    assert 0 < size.compressed < size.raw < demo.TARGET_BYTES


def test_bundle_is_stamped_with_the_checked_out_commit(bundle):
    source = json.loads((bundle[0] / demo.SOURCE_FILE).read_text())
    head = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert source["repo"] == "dflippojr/music-discovery"
    assert source["sha"] in (head, head + "-dirty")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", source["built"])
    assert source["paths"] == ["src/musicdiscovery", "scripts/vendor-into-site"]
    assert all((ROOT / path).exists() for path in source["paths"])


def test_stamp_needs_a_git_checkout(monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(demo.subprocess, "run", no_git)
    with pytest.raises(demo.DemoError, match="git checkout"):
        demo.build_source()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def checkout(tmp_path, monkeypatch, package="src/musicdiscovery", track=True):
    """A real repository holding the package at `package`, imported from there."""
    repo = tmp_path / "repo"
    pkg = repo / package
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (repo / "README.md").write_text("other\n")
    git(repo, "init", "-q")
    git(repo, "add", "README.md", *([package] if track else []))
    identity = (
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@t",
        "-c",
        "commit.gpgsign=false",
    )
    git(repo, *identity, "commit", "-q", "-m", "x")
    monkeypatch.setattr(demo, "__file__", str(pkg / "demo.py"))
    return repo, git(repo, "rev-parse", "HEAD")


def test_stamp_refuses_a_package_the_repository_does_not_track(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch, track=False)
    with pytest.raises(demo.DemoError, match="editable install"):
        demo.build_source()


def test_stamp_refuses_a_package_outside_src(tmp_path, monkeypatch):
    checkout(tmp_path, monkeypatch, package="lib/site-packages/musicdiscovery")
    with pytest.raises(demo.DemoError, match="editable install"):
        demo.build_source()


def test_dirty_tree_marks_the_sha_and_blocks_a_release(tmp_path, monkeypatch):
    repo, head = checkout(tmp_path, monkeypatch)
    assert demo.build_source(release=True)["sha"] == head
    (repo / "src" / "musicdiscovery" / "__init__.py").write_text("x = 1\n")
    assert demo.build_source()["sha"] == head + "-dirty"
    with pytest.raises(demo.DemoError, match="uncommitted changes"):
        demo.build_source(release=True)


def test_untracked_source_file_marks_the_sha_and_blocks_a_release(
    tmp_path, monkeypatch
):
    repo, head = checkout(tmp_path, monkeypatch)
    (repo / "src" / "musicdiscovery" / "new.py").write_text("")
    assert demo.build_source()["sha"] == head + "-dirty"
    with pytest.raises(demo.DemoError, match="uncommitted changes"):
        demo.build_source(release=True)


def test_untracked_files_outside_the_source_paths_keep_the_sha_clean(
    tmp_path, monkeypatch
):
    repo, head = checkout(tmp_path, monkeypatch)
    for path in ("data/catalog.parquet", ".demo-fixture/catalog.json", "notes.txt"):
        (repo / path).parent.mkdir(exist_ok=True)
        (repo / path).write_text("")
    assert demo.build_source(release=True)["sha"] == head


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
    assert "stamped SOURCE.json with " in said


def test_cli_release_refuses_a_dirty_tree(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).to_parquet(data / "catalog.parquet", index=False)
    repo, _ = checkout(tmp_path, monkeypatch)
    (repo / "README.md").write_text("changed\n")
    argv = ["export-demo", str(tmp_path / "o"), "--data-dir", str(tmp_path / "data")]
    assert main([*argv, "--release"]) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


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
def test_vendor_script_copies_runtime_files_and_the_stamp(bundle, tmp_path):
    script = ROOT / "scripts" / "vendor-into-site"
    source = tmp_path / "bundle"
    shutil.copytree(bundle[0], source)
    stamp = {
        "repo": demo.SOURCE_REPO,
        "sha": "0123456789abcdef0123456789abcdef01234567",
        "built": "2026-10-16T09:30:00Z",
        "paths": list(demo.SOURCE_PATHS),
    }
    (source / demo.SOURCE_FILE).write_text(json.dumps(stamp, indent=2) + "\n")

    def vendor(bundle_dir, target):
        return subprocess.run(
            ["sh", str(script), str(bundle_dir), str(target)],
            capture_output=True,
            text=True,
        )

    target = tmp_path / "site" / "demo"
    run = vendor(source, target)
    assert run.returncode == 0, run.stderr
    assert stamp["sha"] in run.stdout
    assert sorted(p.name for p in target.iterdir()) == sorted(
        [*demo.STATIC_FILES, demo.CATALOG_FILE, demo.SOURCE_FILE, demo.SOURCES_FILE]
    )
    vendored = json.loads((target / demo.SOURCE_FILE).read_text())
    assert list(vendored) == ["repo", "sha", "vendored", "paths"]
    assert {k: vendored[k] for k in ("repo", "sha", "paths")} == {
        k: stamp[k] for k in ("repo", "sha", "paths")
    }
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", vendored["vendored"])

    stamp["sha"] += "-dirty"
    (source / demo.SOURCE_FILE).write_text(json.dumps(stamp, indent=2) + "\n")
    dirty = vendor(source, tmp_path / "dirty")
    assert dirty.returncode == 1 and "--release" in dirty.stderr
    assert not (tmp_path / "dirty").exists()

    (source / demo.SOURCE_FILE).unlink()
    unstamped = vendor(source, tmp_path / "unstamped")
    assert unstamped.returncode == 1 and "SOURCE.json is missing" in unstamped.stderr
    empty = vendor(tmp_path / "nothing", tmp_path / "t")
    assert empty.returncode == 1 and "missing" in empty.stderr
    usage = subprocess.run(["sh", str(script)], capture_output=True, text=True)
    assert usage.returncode == 2


def test_export_demo_refuses_an_empty_catalog(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).iloc[:0].to_parquet(data / "catalog.parquet", index=False)
    out = tmp_path / "o"
    assert main(["export-demo", str(out), "--data-dir", str(tmp_path / "data")]) == 1
    err = capsys.readouterr().err
    assert err.splitlines() == ["export-demo: catalog has no tracks"]
    assert not out.exists()


def files_under(directory):
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_export_refusal_leaves_old_bundle_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    frame = make_frame()
    demo.export_demo(Catalog(frame), Path("bundle"))
    before = Path("bundle/catalog.json").read_bytes()
    with pytest.raises(EvaluationError, match="too small"):
        demo.export_demo(Catalog(frame.head(20)), Path("bundle"))
    assert Path("bundle/catalog.json").read_bytes() == before


# Every build_* in the module, found by name, so a stage added later is held to
# the same rule: nothing reaches the out directory until all of them succeeded.
BUILD_STAGES = sorted(name for name in vars(demo) if name.startswith("build_"))


def test_the_build_stages_include_the_ones_a_bundle_needs():
    assert {"build_source", "build_catalog", "build_sources", "build_parity"} <= set(
        BUILD_STAGES
    )


@pytest.mark.parametrize("stage", BUILD_STAGES)
def test_a_refusal_at_any_build_stage_changes_nothing(tmp_path, monkeypatch, stage):
    monkeypatch.chdir(tmp_path)
    catalog = Catalog(make_frame())
    demo.export_demo(catalog, Path("bundle"))
    before = files_under(Path("bundle"))

    def refuse(*args, **kwargs):
        raise demo.DemoError(f"{stage} refused")

    monkeypatch.setattr(demo, stage, refuse)
    with pytest.raises(demo.DemoError, match=f"{stage} refused"):
        demo.export_demo(catalog, Path("bundle"))
    assert files_under(Path("bundle")) == before
    with pytest.raises(demo.DemoError, match=f"{stage} refused"):
        demo.export_demo(catalog, Path("fresh/bundle"))
    assert not Path("fresh").exists()


@pytest.mark.parametrize("tracks", [0, 1, 23])
def test_the_cli_refusing_a_small_catalog_changes_nothing(
    tmp_path, capsys, monkeypatch, tracks
):
    monkeypatch.chdir(tmp_path)
    frame = make_frame()
    demo.export_demo(Catalog(frame), Path("bundle"))
    before = files_under(Path("bundle"))
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    frame.head(tracks).to_parquet(data / "catalog.parquet", index=False)
    assert main(["export-demo", "bundle", "--data-dir", "data"]) == 1
    assert capsys.readouterr().err.startswith("export-demo: catalog ")
    assert files_under(Path("bundle")) == before


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_build_catalog_refuses_an_empty_catalog():
    with pytest.raises(demo.DemoError, match="no tracks"):
        demo.build_catalog(Catalog(make_frame(per_genre=60).iloc[:0]))


def test_build_catalog_refuses_a_vocabulary_over_16_bits(catalog, monkeypatch):
    size = demo.Metadata(catalog).size
    monkeypatch.setattr(demo, "_UINT16_MAX", size)
    assert demo.build_catalog(catalog)["count"] == len(catalog)
    monkeypatch.setattr(demo, "_UINT16_MAX", size - 1)
    with pytest.raises(demo.DemoError, match="too large for 16-bit"):
        demo.build_catalog(catalog)


def test_cli_reports_an_oversized_vocabulary(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    make_frame(per_genre=60).to_parquet(data / "catalog.parquet", index=False)
    monkeypatch.setattr(demo, "_UINT16_MAX", 1)
    argv = ["export-demo", str(tmp_path / "o"), "--data-dir", str(tmp_path / "data")]
    assert main(argv) == 1
    assert capsys.readouterr().err.startswith(
        "export-demo: metadata vocabulary is too large"
    )
