"""Guard the disposable launcher's isolation and cleanup boundaries."""

import importlib.util
import json
import socket
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/ui_instance.py"
    spec = importlib.util.spec_from_file_location("ui_instance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA", tmp_path / "ui-testing")
    monkeypatch.setattr(module, "HANDLE", module.DATA / "instance.json")
    return module


@pytest.mark.parametrize(
    "name",
    [
        "DEMO_BUNDLE",
        "DATABASE_URL",
        "API_KEY",
        "OPENAI_API_KEY",
        "FMA_TOKEN",
        "MUSICDISCOVERY_ENDPOINT",
    ],
)
def test_refuses_external_configuration_before_binding(launcher, monkeypatch, name):
    monkeypatch.setenv(name, "synthetic-forbidden-value")
    with pytest.raises(ValueError, match=name):
        launcher.start(0)
    assert not launcher.DATA.exists()


def test_occupied_port_creates_no_handle_or_bundle(launcher):
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        reserved.listen()
        with pytest.raises(OSError):
            launcher.start(reserved.getsockname()[1])
    assert not launcher.HANDLE.exists()
    assert not list(launcher.DATA.iterdir())


def test_partial_seed_failure_closes_port_and_removes_owned_files(
    launcher, monkeypatch
):
    port = None
    original_server = launcher.ThreadingHTTPServer

    def server(*args, **kwargs):
        nonlocal port
        instance = original_server(*args, **kwargs)
        port = instance.server_port
        return instance

    def fail_seed(args, **kwargs):
        bundle = Path(args[-1])
        bundle.mkdir()
        (bundle / "partial.json").write_text("{}")
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(launcher, "ThreadingHTTPServer", server)
    monkeypatch.setattr(launcher.subprocess, "run", fail_seed)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.start(0)
    assert not launcher.HANDLE.exists()
    assert not list(launcher.DATA.iterdir())
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    launcher.stop()  # safe after partial failure


def test_cleanup_refuses_paths_outside_disposable_directory(launcher, tmp_path):
    unrelated = tmp_path / "run-unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="outside"):
        launcher.remove_bundle(unrelated)
    assert sentinel.read_text() == "keep"


def test_cleanup_refuses_nested_unexpected_data(launcher):
    bundle = launcher.DATA / "run-owned"
    unexpected = bundle / "nested"
    unexpected.mkdir(parents=True)
    with pytest.raises(ValueError, match="Unexpected"):
        launcher.remove_bundle(bundle)
    assert unexpected.is_dir()


def test_stop_refuses_another_instance_on_the_same_port(launcher, monkeypatch):
    launcher.DATA.mkdir()
    launcher.HANDLE.write_text(
        json.dumps({"instance": "run-owned", "port": 4173, "token": "synthetic"})
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return b'{"instance":"run-other"}'

    requests = []

    def request(url, **kwargs):
        requests.append(url)
        return Response()

    monkeypatch.setattr(launcher, "urlopen", request)
    with pytest.raises(ValueError, match="different instance"):
        launcher.stop()
    assert len(requests) == 1
    assert launcher.HANDLE.exists()
