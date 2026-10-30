"""Exercise ingest against a synthetic archive shaped like fma_metadata.zip."""

import hashlib
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from musicdiscovery import ingest as ingest_module
from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.cli import main
from musicdiscovery.ingest import IngestError, ingest

TRACK_COLUMNS = [
    ("album", "title"),
    ("artist", "name"),
    ("set", "subset"),
    ("track", "duration"),
    ("track", "genre_top"),
    ("track", "genres_all"),
    ("track", "license"),
    ("track", "listens"),
    ("track", "tags"),
    ("track", "title"),
]

# id: (subset, license title, license url, features present, tags)
TRACKS = {
    1: ("small", "CC BY", "http://cc/by/", True, "['a', 'b']"),
    2: ("small", "CC BY-SA", "http://cc/by-sa/", True, "[]"),
    3: ("small", None, None, True, "[]"),
    4: ("small", "CC BY", "http://cc/by/", False, "[]"),
    5: ("medium", "CC BY", "http://cc/by/", True, "[]"),
}


def _tracks_csv(cells: dict[str, str] | None = None) -> str:
    """Write tracks.csv; ``cells`` replaces track 1's list cells by column name."""
    cells = cells or {}
    groups = ",".join(["", *(group for group, _ in TRACK_COLUMNS)])
    names = ",".join(["", *(name for _, name in TRACK_COLUMNS)])
    rows = ["track_id" + "," * len(TRACK_COLUMNS)]
    for track_id, (subset, lic, _, _, tags) in TRACKS.items():
        genres = f"[{track_id}, 2]"
        if track_id == 1:
            genres, tags = cells.get("genres_all", genres), cells.get("tags", tags)
        values = [
            f"Album {track_id}",
            f"Artist {track_id}",
            subset,
            "120",
            "Rock",
            f'"{genres}"',
            lic or "",
            "7",
            f'"{tags}"',
            f"Title {track_id}",
        ]
        rows.append(",".join([str(track_id), *values]))
    return "\n".join([groups, names, *rows]) + "\n"


def _raw_tracks_csv() -> str:
    rows = ["track_id,license_url,track_url,extra"]
    for track_id, (_, _, url, _, _) in TRACKS.items():
        rows.append(f"{track_id},{url or ''},http://source/{track_id},x")
    rows.append("1,http://dup/,http://dup/1,x")  # duplicate ids must not multiply rows
    return "\n".join(rows) + "\n"


def _features_csv() -> str:
    rows = [
        "feature,chroma,chroma,mfcc",
        "statistics,mean,std,mean",
        "number,01,01,01",
        "track_id,,,",
    ]
    for track_id, (_, _, _, present, _) in TRACKS.items():
        rows.append(f"{track_id},1.5,2.5,3.5" if present else f"{track_id},1.5,,3.5")
    return "\n".join(rows) + "\n"


def _write_archive(path: Path, tracks_csv: str) -> tuple[Path, str]:
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fma_metadata/tracks.csv", tracks_csv)
        zf.writestr("fma_metadata/raw_tracks.csv", _raw_tracks_csv())
        zf.writestr("fma_metadata/features.csv", _features_csv())
    return path, hashlib.sha1(path.read_bytes()).hexdigest()


@pytest.fixture
def archive(tmp_path: Path) -> tuple[Path, str]:
    return _write_archive(tmp_path / "source" / "fma_metadata.zip", _tracks_csv())


def test_ingest_builds_clean_catalog(tmp_path, archive):
    path, sha1 = archive
    data_dir = tmp_path / "data"

    result = ingest(data_dir, url=path.as_uri(), sha1=sha1)

    assert not result.skipped
    assert (result.subset_tracks, result.kept) == (4, 2)
    assert result.dropped_missing_license == 1
    assert result.dropped_missing_features == 1
    catalog = pd.read_parquet(data_dir / "catalog" / "catalog.parquet")
    assert catalog["track_id"].tolist() == [1, 2]
    first = catalog.iloc[0]
    assert first["title"] == "Title 1"
    assert first["artist"] == "Artist 1"
    assert first["genre_top"] == "Rock"
    assert list(first["genre_ids"]) == [1, 2]
    assert list(first["tags"]) == ["a", "b"]
    assert first["license_url"] == "http://cc/by/"
    assert first["source_url"] == "http://source/1"
    assert first["mfcc_mean_01"] == pytest.approx(3.5)
    assert not catalog.isna().any().any()


def test_second_run_is_a_noop(tmp_path, archive, monkeypatch):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    first = ingest(data_dir, url=path.as_uri(), sha1=sha1)
    catalog_path = data_dir / "catalog" / "catalog.parquet"
    stamp = catalog_path.stat().st_mtime_ns

    manifest_path = data_dir / "catalog" / "catalog.json"
    manifest_stamp = manifest_path.stat().st_mtime_ns

    def unexpected(*args, **kwargs):
        pytest.fail("valid cache must not fetch an archive or build a catalog")

    monkeypatch.setattr(ingest_module, "fetch_archive", unexpected)
    monkeypatch.setattr(ingest_module, "build_catalog", unexpected)
    second = ingest(data_dir, url="file:///does/not/exist.zip", sha1=sha1)

    assert second.skipped
    assert second.kept == first.kept
    assert catalog_path.stat().st_mtime_ns == stamp
    assert manifest_path.stat().st_mtime_ns == manifest_stamp


def test_cached_download_is_reused(tmp_path, archive):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True)
    (data_dir / "raw" / "fma_metadata.zip").write_bytes(path.read_bytes())

    result = ingest(data_dir, url="file:///does/not/exist.zip", sha1=sha1)

    assert result.kept == 2


def test_checksum_works_where_sha1_is_blocked_for_security(
    tmp_path, archive, monkeypatch
):
    # SHA1 only matches the checksum the FMA publishes; it protects nothing. A
    # FIPS-mode Python refuses hashlib.sha1() unless told it is not for security.
    path, sha1 = archive
    real_sha1 = hashlib.sha1

    def fips_sha1(*args, usedforsecurity=True):
        if usedforsecurity:
            raise ValueError("[digital envelope routines] unsupported")
        return real_sha1(*args, usedforsecurity=False)

    monkeypatch.setattr(ingest_module.hashlib, "sha1", fips_sha1)

    assert ingest_module.sha1_of(path) == sha1


def test_checksum_mismatch_fails_and_removes_download(tmp_path, archive):
    path, _ = archive
    data_dir = tmp_path / "data"

    with pytest.raises(IngestError, match="checksum mismatch"):
        ingest(data_dir, url=path.as_uri(), sha1="0" * 40)

    assert not (data_dir / "raw" / "fma_metadata.zip").exists()
    assert not (data_dir / "catalog" / "catalog.parquet").exists()


def test_cli_ingest_reports_counts(tmp_path, archive, monkeypatch, capsys):
    path, sha1 = archive
    monkeypatch.setattr(
        ingest_module.ingest, "__defaults__", (path.as_uri(), sha1), raising=True
    )
    data_dir = str(tmp_path / "data")

    assert main(["ingest", "--data-dir", data_dir]) == 0
    assert capsys.readouterr().out == (
        "ingest: kept 2 of 4 tracks; dropped 1 without a license and "
        "1 without features\n"
    )
    assert main(["ingest", "--data-dir", data_dir]) == 0
    assert "up to date" in capsys.readouterr().out


def test_cli_ingest_reports_errors(tmp_path, archive, monkeypatch, capsys):
    path, _ = archive
    monkeypatch.setattr(
        ingest_module.ingest, "__defaults__", (path.as_uri(), "0" * 40), raising=True
    )

    assert main(["ingest", "--data-dir", str(tmp_path / "data")]) == 1
    assert "checksum mismatch" in capsys.readouterr().err


def test_unreadable_manifest_triggers_reingest(tmp_path, archive):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    ingest(data_dir, url=path.as_uri(), sha1=sha1)
    (data_dir / "catalog" / "catalog.json").write_text("not json")

    assert not ingest(data_dir, url=path.as_uri(), sha1=sha1).skipped


@pytest.fixture
def cached_catalog(tmp_path, archive, monkeypatch):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    ingest(data_dir, url=path.as_uri(), sha1=sha1)

    def unexpected_download(*args, **kwargs):
        pytest.fail("recovery must reuse the verified cached archive")

    monkeypatch.setattr(ingest_module, "_download", unexpected_download)
    return data_dir, sha1


@pytest.mark.parametrize("manifest", [[], None, True, 7, "manifest"])
def test_wrong_manifest_top_level_recovers(cached_catalog, manifest):
    data_dir, sha1 = cached_catalog
    (data_dir / "catalog" / "catalog.json").write_text(json.dumps(manifest))
    assert not ingest(data_dir, sha1=sha1).skipped
    assert len(Catalog.load(data_dir / "catalog" / "catalog.parquet")) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("result", None),
        ("result", []),
        ("result", {}),
        ("result", "counts"),
        ("ingest_version", True),
        ("ingest_version", 1.0),
        ("ingest_version", 0),
        ("source_sha1", "stale"),
        ("subset", "medium"),
    ],
)
def test_invalid_manifest_fields_recover(cached_catalog, field, value):
    data_dir, sha1 = cached_catalog
    path = data_dir / "catalog" / "catalog.json"
    manifest = json.loads(path.read_text())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    assert not ingest(data_dir, sha1=sha1).skipped


@pytest.mark.parametrize(
    "field",
    ["subset_tracks", "kept", "dropped_missing_license", "dropped_missing_features"],
)
@pytest.mark.parametrize("value", [None, True, "2", 2.0, -1])
def test_invalid_count_types_recover(cached_catalog, field, value):
    data_dir, sha1 = cached_catalog
    path = data_dir / "catalog" / "catalog.json"
    manifest = json.loads(path.read_text())
    manifest["result"][field] = value
    path.write_text(json.dumps(manifest))
    assert not ingest(data_dir, sha1=sha1).skipped


@pytest.mark.parametrize(
    "mutation", ["missing_result", "missing_count", "extra_field", "skipped", "totals"]
)
def test_wrong_result_structure_recovers(cached_catalog, mutation):
    data_dir, sha1 = cached_catalog
    path = data_dir / "catalog" / "catalog.json"
    manifest = json.loads(path.read_text())
    if mutation == "missing_result":
        del manifest["result"]
    elif mutation == "missing_count":
        del manifest["result"]["kept"]
    elif mutation == "extra_field":
        manifest["result"]["extra"] = 0
    elif mutation == "skipped":
        manifest["result"]["skipped"] = 0
    else:
        manifest["result"]["subset_tracks"] += 1
    path.write_text(json.dumps(manifest))
    assert not ingest(data_dir, sha1=sha1).skipped


@pytest.mark.parametrize(
    "damage", ["unreadable", "missing", "count", "schema", "duplicate"]
)
def test_damaged_catalog_recovers(cached_catalog, damage):
    data_dir, sha1 = cached_catalog
    path = data_dir / "catalog" / "catalog.parquet"
    frame = pd.read_parquet(path)
    if damage == "unreadable":
        path.write_bytes(b"truncated parquet")
        with pytest.raises(CatalogError, match="could not read catalog"):
            Catalog.load(path)
    elif damage == "missing":
        path.unlink()
    else:
        if damage == "count":
            frame = frame.iloc[:1]
        elif damage == "schema":
            frame = frame.drop(columns="artist")
        else:
            frame.loc[1, "track_id"] = frame.loc[0, "track_id"]
        frame.to_parquet(path, index=False)
    assert not ingest(data_dir, sha1=sha1).skipped
    assert Catalog.load(path).track_ids.tolist() == [1, 2]
    assert ingest(data_dir, sha1=sha1).skipped


@pytest.mark.parametrize(
    "stage", ["parquet", "manifest", "catalog_replace", "manifest_replace"]
)
@pytest.mark.parametrize("interrupted", [False, True])
def test_publication_failure_is_recoverable(
    cached_catalog, monkeypatch, stage, interrupted
):
    data_dir, sha1 = cached_catalog
    out_dir = data_dir / "catalog"
    catalog_path = out_dir / "catalog.parquet"
    manifest_path = out_dir / "catalog.json"
    failure = KeyboardInterrupt if interrupted else OSError
    expected_error = KeyboardInterrupt if interrupted else IngestError
    old_catalog = catalog_path.read_bytes()
    old_manifest = manifest_path.read_bytes()
    with monkeypatch.context() as patch:
        # Force regeneration while retaining a complete previous cache.
        patch.setattr(ingest_module, "_cached_result", lambda *args: None)
        if stage == "parquet":

            def fail_parquet(self, path, **kwargs):
                path.write_bytes(b"partial parquet")
                raise failure("publication failed")

            patch.setattr(pd.DataFrame, "to_parquet", fail_parquet)
        elif stage == "manifest":
            write_text = Path.write_text

            def fail_manifest(self, text, **kwargs):
                write_text(self, "partial manifest", **kwargs)
                raise failure("publication failed")

            patch.setattr(Path, "write_text", fail_manifest)
        else:
            replace = Path.replace
            target_name = (
                "catalog.parquet" if stage == "catalog_replace" else "catalog.json"
            )

            def fail_replace(self, target):
                if self.name == target_name:
                    raise failure("publication failed")
                return replace(self, target)

            patch.setattr(Path, "replace", fail_replace)
        with pytest.raises(expected_error, match="publication failed"):
            ingest(data_dir, sha1=sha1)
    assert catalog_path.read_bytes() == old_catalog
    assert not list(out_dir.glob(".ingest-*"))
    if stage in {"parquet", "manifest"}:
        assert manifest_path.read_bytes() == old_manifest
        assert ingest(data_dir, sha1=sha1).skipped
    else:
        assert not manifest_path.exists()
        assert not ingest(data_dir, sha1=sha1).skipped
    assert Catalog.load(catalog_path).track_ids.tolist() == [1, 2]


@pytest.mark.parametrize("failure", ["archive", "metadata", "write"])
def test_cli_reports_reconstruction_failure(
    cached_catalog, monkeypatch, capsys, failure
):
    data_dir, sha1 = cached_catalog
    (data_dir / "catalog" / "catalog.parquet").write_bytes(b"truncated parquet")
    monkeypatch.setattr(ingest_module.ingest, "__defaults__", ("unused", sha1))
    if failure == "archive":
        archive_path = data_dir / "raw" / "fma_metadata.zip"
        archive_path.write_bytes(b"not a zip")
        bad_sha1 = hashlib.sha1(archive_path.read_bytes()).hexdigest()
        monkeypatch.setattr(ingest_module.ingest, "__defaults__", ("unused", bad_sha1))
    elif failure == "metadata":

        def fail_build(*args):
            raise KeyError("missing tracks column")

        monkeypatch.setattr(ingest_module, "build_catalog", fail_build)
    else:

        def fail_write(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_write)
    assert main(["ingest", "--data-dir", str(data_dir)]) == 1
    captured = capsys.readouterr()
    assert "ingest: could not rebuild catalog:" in captured.err
    assert "up to date" not in captured.out
    assert "Traceback" not in captured.err


def test_invalid_manifest_encoding_recovers(cached_catalog):
    data_dir, sha1 = cached_catalog
    (data_dir / "catalog" / "catalog.json").write_bytes(b"\xff")
    assert not ingest(data_dir, sha1=sha1).skipped


MALFORMED_CELLS = [
    pytest.param({"tags": "['a', 'b'"}, id="tags-unclosed"),
    pytest.param({"genres_all": "[2, [2]]"}, id="genres_all-nested"),
]


@pytest.mark.parametrize("cells", MALFORMED_CELLS)
@pytest.mark.parametrize("previous", [False, True], ids=["fresh", "cached"])
def test_malformed_list_cell_fails_cleanly(tmp_path, archive, cells, previous):
    data_dir = tmp_path / "data"
    out_dir = data_dir / "catalog"
    if previous:
        path, sha1 = archive
        ingest(data_dir, url=path.as_uri(), sha1=sha1)
        (data_dir / "raw" / "fma_metadata.zip").unlink()  # fetch the bad archive
    old = {p.name: p.read_bytes() for p in out_dir.glob("*")} if previous else {}
    bad, bad_sha1 = _write_archive(
        tmp_path / "bad" / "fma_metadata.zip", _tracks_csv(cells)
    )

    with pytest.raises(IngestError, match="could not rebuild catalog"):
        ingest(data_dir, url=bad.as_uri(), sha1=bad_sha1)

    assert {p.name: p.read_bytes() for p in out_dir.glob("*")} == old


@pytest.mark.parametrize("cells", MALFORMED_CELLS)
def test_cli_reports_malformed_list_cell(tmp_path, monkeypatch, capsys, cells):
    bad, sha1 = _write_archive(
        tmp_path / "bad" / "fma_metadata.zip", _tracks_csv(cells)
    )
    monkeypatch.setattr(ingest_module.ingest, "__defaults__", (bad.as_uri(), sha1))

    assert main(["ingest", "--data-dir", str(tmp_path / "data")]) == 1
    captured = capsys.readouterr()
    assert "ingest: could not rebuild catalog:" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize("value", ["{'a': 1}", "5", "", "  ", float("nan")])
def test_non_list_literal_reads_as_empty(value):
    assert ingest_module._parse_list(value) == []
