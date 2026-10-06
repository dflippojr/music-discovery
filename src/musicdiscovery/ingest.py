"""Ingest the FMA small subset into a cleaned local catalog.

Only the metadata archive is used (``fma_metadata.zip``); no audio is read or
downloaded. See ``docs/catalog.md`` for the source, checksum and licenses.
"""

import ast
import csv
import hashlib
import http.client
import json
import shutil
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

METADATA_URL = "https://os.unil.cloud.switch.ch/fma/fma_metadata.zip"
METADATA_SHA1 = "f0df49ffe5f2a6008d7dc83c6915b31835dfe733"
SUBSET = "small"
INGEST_VERSION = 1

ARCHIVE_NAME = "fma_metadata.zip"
CATALOG_NAME = "catalog.parquet"
MANIFEST_NAME = "catalog.json"

DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_ATTEMPTS = 3
_BACKOFF_SECONDS = 2

_ARCHIVE_DIR = "fma_metadata/"
_CHUNK_BYTES = 1 << 20
_FEATURE_ROWS = 4  # feature, statistic, number and track_id header rows


class IngestError(Exception):
    """Raised when the source archive cannot be used."""


@dataclass(frozen=True)
class IngestResult:
    """Counts reported by an ingest run."""

    skipped: bool
    subset_tracks: int
    kept: int
    dropped_missing_license: int
    dropped_missing_features: int


def sha1_of(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _download_once(url: str, partial: Path) -> None:
    with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        with partial.open("wb") as out:
            shutil.copyfileobj(response, out, _CHUNK_BYTES)


def _is_transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500
    return True


def _download(url: str, target: Path) -> None:
    """Download to ``target`` via a fresh ``.part`` file, retrying transient errors."""
    partial = target.with_suffix(".part")
    cause: Exception | None = None
    attempts = 0
    while attempts < DOWNLOAD_ATTEMPTS:
        if attempts:
            time.sleep(_BACKOFF_SECONDS * 2 ** (attempts - 1))
        attempts += 1
        partial.unlink(missing_ok=True)
        try:
            _download_once(url, partial)
            partial.replace(target)
            return
        except (OSError, http.client.HTTPException) as error:
            cause = error
            partial.unlink(missing_ok=True)
            if not _is_transient(error):
                break
    raise IngestError(
        f"could not download {url} after {attempts} attempt(s): {cause}"
    ) from cause


def fetch_archive(url: str, sha1: str, cache_dir: Path) -> Path:
    """Return the verified archive, downloading it only when not cached."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / ARCHIVE_NAME
    if not target.exists():
        _download(url, target)
    actual = sha1_of(target)
    if actual != sha1:
        target.unlink()
        raise IngestError(
            f"checksum mismatch for {ARCHIVE_NAME}: expected {sha1}, got {actual}; "
            "the cached file was removed"
        )
    return target


def _parse_list(value: object) -> list:
    if not isinstance(value, str) or not value.strip():
        return []
    parsed = ast.literal_eval(value)
    return list(parsed) if isinstance(parsed, list | tuple) else []


def _read_tracks(archive: zipfile.ZipFile) -> pd.DataFrame:
    with archive.open(_ARCHIVE_DIR + "tracks.csv") as handle:
        raw = pd.read_csv(handle, index_col=0, header=[0, 1], low_memory=False)
    subset = raw[raw[("set", "subset")] == SUBSET]
    tracks = pd.DataFrame(
        {
            "title": subset[("track", "title")],
            "artist": subset[("artist", "name")],
            "album": subset[("album", "title")],
            "genre_top": subset[("track", "genre_top")],
            "genre_ids": subset[("track", "genres_all")].map(_parse_list),
            "tags": subset[("track", "tags")].map(_parse_list),
            "duration": subset[("track", "duration")],
            "license": subset[("track", "license")],
            "listens": subset[("track", "listens")],
        }
    )
    tracks.index.name = "track_id"
    return tracks


def _read_links(archive: zipfile.ZipFile, track_ids: pd.Index) -> pd.DataFrame:
    columns = ["track_id", "license_url", "track_url"]
    with archive.open(_ARCHIVE_DIR + "raw_tracks.csv") as handle:
        links = pd.read_csv(handle, usecols=columns, index_col="track_id")
    links = links[~links.index.duplicated()]
    return links.reindex(track_ids)


def _read_features(archive: zipfile.ZipFile, track_ids: set[int]) -> pd.DataFrame:
    with archive.open(_ARCHIVE_DIR + "features.csv") as handle:
        header = [next(csv.reader([handle.readline().decode()])) for _ in range(3)]
    names = [
        f"{feature}_{stat}_{int(number):02d}"
        for feature, stat, number in zip(*(row[1:] for row in header), strict=True)
    ]
    with archive.open(_ARCHIVE_DIR + "features.csv") as handle:
        chunks = pd.read_csv(
            handle,
            skiprows=_FEATURE_ROWS,
            header=None,
            index_col=0,
            names=["track_id", *names],
            dtype={name: "float32" for name in names},
            chunksize=5000,
        )
        kept = [chunk[chunk.index.isin(track_ids)] for chunk in chunks]
    return pd.concat(kept)


def build_catalog(archive_path: Path) -> tuple[pd.DataFrame, IngestResult]:
    """Build the cleaned catalog from the metadata archive."""
    with zipfile.ZipFile(archive_path) as archive:
        tracks = _read_tracks(archive)
        links = _read_links(archive, tracks.index)
        features = _read_features(archive, set(tracks.index))

    licensed = tracks["license"].notna() & links["license_url"].notna()
    licensed &= links["license_url"].astype("string").str.strip().ne("").fillna(False)
    catalog = tracks.join(links[["license_url", "track_url"]]).drop(columns="license")
    catalog = catalog.rename(columns={"track_url": "source_url"})

    has_features = tracks.index.isin(features.dropna().index)
    kept = licensed & has_features
    result = IngestResult(
        skipped=False,
        subset_tracks=len(tracks),
        kept=int(kept.sum()),
        dropped_missing_license=int((~licensed).sum()),
        dropped_missing_features=int((licensed & ~has_features).sum()),
    )
    catalog = pd.concat([catalog[kept], features.reindex(catalog.index[kept])], axis=1)
    catalog["genre_ids"] = catalog["genre_ids"].map(lambda ids: [int(i) for i in ids])
    return catalog.reset_index(), result


def _manifest_matches(manifest_path: Path, sha1: str) -> bool:
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError):
        return False
    return (
        manifest.get("source_sha1") == sha1
        and manifest.get("ingest_version") == INGEST_VERSION
    )


def ingest(
    data_dir: Path,
    url: str = METADATA_URL,
    sha1: str = METADATA_SHA1,
) -> IngestResult:
    """Download (or reuse) the archive and write the catalog; no-op if current."""
    out_dir = data_dir / "catalog"
    catalog_path = out_dir / CATALOG_NAME
    manifest_path = out_dir / MANIFEST_NAME
    if catalog_path.exists() and _manifest_matches(manifest_path, sha1):
        saved = json.loads(manifest_path.read_text())["result"]
        return IngestResult(**{**saved, "skipped": True})

    archive_path = fetch_archive(url, sha1, data_dir / "raw")
    catalog, result = build_catalog(archive_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    catalog.to_parquet(catalog_path, index=False)
    manifest = {
        "source_sha1": sha1,
        "ingest_version": INGEST_VERSION,
        "subset": SUBSET,
        "result": asdict(result),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return result
