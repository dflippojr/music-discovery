"""Checks for AGENTS.md's data rules (#127): no datasets or audio tracked, and
tests reach only loopback."""

import socket
import subprocess
import threading
from pathlib import Path

import pytest
from pytest_socket import SocketConnectBlockedError

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 1024 * 1024
DATA_SUFFIXES = (".mp3", ".wav", ".flac", ".ogg", ".npy", ".npz", ".parquet", ".h5")
# Tracked files allowed over MAX_BYTES, one reviewed path per line.
ALLOWED_LARGE: frozenset[str] = frozenset()


def _offences(files: dict[str, int], allowed: frozenset[str] = ALLOWED_LARGE):
    """Why each tracked path breaks the data rules, given {path: size in bytes}."""
    found = []
    for path, size in sorted(files.items()):
        lower = path.lower()
        if lower.startswith("data/"):
            found.append(f"{path}: tracked under data/")
        if lower.endswith(DATA_SUFFIXES):
            found.append(f"{path}: audio, array or dataset file")
        if size > MAX_BYTES and path not in allowed:
            found.append(f"{path}: {size} bytes, over 1 MiB and not in ALLOWED_LARGE")
    return found


def _tracked() -> dict[str, int]:
    """{path: staged blob size} for every file git tracks. Fails if git can't run."""
    done = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--format=%(objectsize) %(path)"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    entries = (entry.split(" ", 1) for entry in done.stdout.split("\0") if entry)
    files = {path: int(size) for size, path in entries}
    assert files, "git ls-files listed no tracked files"
    return files


def test_no_dataset_audio_or_large_file_is_tracked():
    offences = _offences(_tracked())
    assert not offences, "\n".join(offences)


@pytest.mark.parametrize(
    ("path", "size"),
    [
        ("x.MP3", 10),
        ("foo.npz", 10),
        ("clip.m4a", 10),
        ("clip.aac", 10),
        ("clip.opus", 10),
        ("clip.aiff", 10),
        ("fma_small_sample.zip", 10),
        ("tracks.csv", 10),
        ("reports/big.json", 2 * MAX_BYTES),
        ("data/anything", 10),
    ],
)
def test_data_guard_names_each_offending_path(path, size):
    offences = _offences({path: size, "src/ok.py": 100})
    assert len(offences) == 1, offences
    assert offences[0].startswith(f"{path}: ")


# Audio, archive and tabular or serialised formats that the project's data
# arrives in or could be cached as (#150). Each is refused at any size.
REQUIRED_SUFFIXES = (
    ".m4a",
    ".aac",
    ".opus",
    ".aif",
    ".aiff",
    ".wma",
    ".alac",
    ".mp4",
    ".webm",
    ".mka",
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".7z",
    ".csv",
    ".tsv",
    ".pkl",
    ".pickle",
    ".feather",
    ".arrow",
    ".hdf5",
)


@pytest.mark.parametrize("suffix", REQUIRED_SUFFIXES)
@pytest.mark.parametrize("size", [10, 2 * MAX_BYTES])
def test_data_guard_refuses_each_required_suffix_at_any_size(suffix, size):
    path = f"tests/fixtures/sample{suffix}"
    assert f"{path}: audio, array or dataset file" in _offences({path: size})


@pytest.mark.parametrize(
    ("path", "size"),
    [("reports/big.json", 2 * MAX_BYTES), ("src/musicdiscovery/cli.py", 2000)],
)
def test_data_guard_passes_allow_listed_large_and_ordinary_files(path, size):
    assert _offences({path: size}, frozenset({"reports/big.json"})) == []


def test_data_guard_fails_closed_when_git_cannot_run(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(FileNotFoundError):
        _tracked()


def test_tests_cannot_connect_to_a_non_loopback_address():
    # 192.0.2.1 is TEST-NET-1 (RFC 5737): never routed, so without pytest-socket
    # this would time out instead of raising the blocked-connection error.
    with socket.socket() as sock:
        sock.settimeout(2)
        with pytest.raises(SocketConnectBlockedError):
            sock.connect(("192.0.2.1", 80))


def test_tests_can_still_bind_and_connect_on_loopback():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        accepted = threading.Thread(target=lambda: server.accept()[0].close())
        accepted.start()
        with socket.create_connection(server.getsockname(), timeout=5):
            pass
        accepted.join(5)
