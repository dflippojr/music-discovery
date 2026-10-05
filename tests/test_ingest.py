"""Exercise ingest against a synthetic archive shaped like fma_metadata.zip."""

import hashlib
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from musicdiscovery import ingest as ingest_module
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


def _tracks_csv() -> str:
    groups = ",".join(["", *(group for group, _ in TRACK_COLUMNS)])
    names = ",".join(["", *(name for _, name in TRACK_COLUMNS)])
    rows = ["track_id" + "," * len(TRACK_COLUMNS)]
    for track_id, (subset, lic, _, _, tags) in TRACKS.items():
        values = [
            f"Album {track_id}",
            f"Artist {track_id}",
            subset,
            "120",
            "Rock",
            f'"[{track_id}, 2]"',
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


@pytest.fixture
def archive(tmp_path: Path) -> tuple[Path, str]:
    path = tmp_path / "source" / "fma_metadata.zip"
    path.parent.mkdir()
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fma_metadata/tracks.csv", _tracks_csv())
        zf.writestr("fma_metadata/raw_tracks.csv", _raw_tracks_csv())
        zf.writestr("fma_metadata/features.csv", _features_csv())
    return path, hashlib.sha1(path.read_bytes()).hexdigest()


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


def test_second_run_is_a_noop(tmp_path, archive):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    first = ingest(data_dir, url=path.as_uri(), sha1=sha1)
    catalog_path = data_dir / "catalog" / "catalog.parquet"
    stamp = catalog_path.stat().st_mtime_ns

    second = ingest(data_dir, url="file:///does/not/exist.zip", sha1=sha1)

    assert second.skipped
    assert second.kept == first.kept
    assert catalog_path.stat().st_mtime_ns == stamp


def test_cached_download_is_reused(tmp_path, archive):
    path, sha1 = archive
    data_dir = tmp_path / "data"
    (data_dir / "raw").mkdir(parents=True)
    (data_dir / "raw" / "fma_metadata.zip").write_bytes(path.read_bytes())

    result = ingest(data_dir, url="file:///does/not/exist.zip", sha1=sha1)

    assert result.kept == 2


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
