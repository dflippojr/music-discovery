"""Guard the disposable launcher's isolation and cleanup boundaries."""

import importlib.util
import json
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

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


@pytest.fixture
def seeding(launcher, monkeypatch):
    """Real owned children with a pipe handshake and no data dependencies."""
    children = []
    ports = []
    original_server = launcher.ThreadingHTTPServer
    original_popen = subprocess.Popen
    unrelated = launcher.DATA / "run-unrelated"
    unrelated.mkdir(parents=True)
    (unrelated / "keep.txt").write_text("keep")

    def server(*args, **kwargs):
        instance = original_server(*args, **kwargs)
        ports.append(instance.server_port)
        return instance

    def seed(*, block=False, exit_code=0, cancel=None, escalate=False):
        def spawn(args, **kwargs):
            script = (
                "import json, sys\n"
                "from pathlib import Path\n"
                "bundle = Path(sys.argv[1])\n"
                "bundle.mkdir()\n"
                "(bundle / 'catalog.json').write_text("
                "json.dumps({'ids': [100], 'licenses': ['synthetic']}))\n"
                "(bundle / 'sources.json').write_text("
                "json.dumps({'sources': ['synthetic']}))\n"
                "print('ready', flush=True)\n"
                + ("sys.stdin.read()\n" if block else "")
                + f"sys.exit({exit_code})\n"
            )
            child = original_popen(
                [sys.executable, "-c", script, args[-1]],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                **kwargs,
            )
            children.append(child)
            ready = threading.Event()

            def read_ready():
                if child.stdout.readline() == "ready\n":
                    ready.set()

            threading.Thread(target=read_ready, daemon=True).start()
            assert ready.wait(5), "Synthetic child did not reach seeding handshake"
            if escalate:
                # Deterministically exercise kill even on Windows, whose
                # terminate() cannot be ignored by a signal handler.
                monkeypatch.setattr(child, "terminate", lambda: None)
            if cancel is not None:
                original_wait = child.wait

                def interrupt_wait(*args, **kwargs):
                    monkeypatch.setattr(child, "wait", original_wait)
                    signal.raise_signal(cancel)
                    return original_wait(*args, **kwargs)

                monkeypatch.setattr(child, "wait", interrupt_wait)
            return child

        monkeypatch.setattr(launcher.subprocess, "Popen", spawn)

    monkeypatch.setattr(launcher, "ThreadingHTTPServer", server)
    # Check that cleanup cannot race a live child still writing the bundle.
    original_remove = launcher.remove_bundle

    def remove(bundle):
        assert children[-1].poll() is not None
        original_remove(bundle)

    monkeypatch.setattr(launcher, "remove_bundle", remove)
    yield seed, children, ports
    for child in children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        child.stdin.close()
        child.stdout.close()


def assert_cleaned(launcher, children, ports):
    assert children[-1].poll() is not None
    assert not launcher.HANDLE.exists()
    assert not Path(children[-1].args[-1]).exists()
    assert (launcher.DATA / "run-unrelated" / "keep.txt").read_text() == "keep"
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", ports[-1]))
    launcher.stop()  # safe to repeat after interrupted startup


def assert_restart(launcher, monkeypatch, seeding):
    seed, children, ports = seeding
    seed()
    previous_port = ports[-1]
    serving = threading.Event()
    original_serve = ThreadingHTTPServer.serve_forever
    errors = []

    def serve(server, **kwargs):
        serving.set()
        original_serve(server, **kwargs)

    def stop_when_ready():
        try:
            assert serving.wait(5)
            metadata = json.loads(launcher.HANDLE.read_text())
            origin = f"http://127.0.0.1:{metadata['port']}"
            with urlopen(origin + "/__ui_health", timeout=5) as response:
                assert json.load(response)["instance"] == metadata["instance"]
            bundle = launcher.DATA / metadata["instance"]
            assert json.loads((bundle / "catalog.json").read_text())["licenses"] == [
                "/synthetic/license"
            ]
            assert json.loads((bundle / "sources.json").read_text())["sources"] == [
                "/synthetic/track/100"
            ]
            request = Request(
                origin + "/__ui_stop",
                method="POST",
                headers={"X-UI-Token": metadata["token"]},
            )
            with urlopen(request, timeout=5) as response:
                assert response.status == 204
        except Exception as error:
            errors.append(error)
            # Bound the test if readiness or an assertion fails.
            launcher_server.shutdown()

    # Capture the real server for failure teardown without signalling others.
    server_factory = launcher.ThreadingHTTPServer
    launcher_server = None

    def capture_server(*args, **kwargs):
        nonlocal launcher_server
        launcher_server = server_factory(*args, **kwargs)
        return launcher_server

    monkeypatch.setattr(launcher, "ThreadingHTTPServer", capture_server)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve)
    worker = threading.Thread(target=stop_when_ready, daemon=True)
    worker.start()
    launcher.start(previous_port)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    assert_cleaned(launcher, children, ports)


def test_partial_seed_failure_closes_port_and_removes_owned_files(launcher, seeding):
    seed, children, ports = seeding
    seed(exit_code=1)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.start(0)
    assert_cleaned(launcher, children, ports)
    assert list(launcher.DATA.iterdir()) == [launcher.DATA / "run-unrelated"]


@pytest.mark.parametrize("escalate", [False, True])
def test_seed_timeout_is_nonzero_and_allows_restart(
    launcher, monkeypatch, seeding, capsys, escalate
):
    seed, children, ports = seeding
    assert launcher.FIXTURE_TIMEOUT == 30
    monkeypatch.setattr(launcher, "FIXTURE_TIMEOUT", 0.1)
    monkeypatch.setattr(launcher, "CHILD_EXIT_TIMEOUT", 0.05)
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", "--port", "0"])
    seed(block=True, escalate=escalate)
    started = time.monotonic()
    assert launcher.main() == 1
    assert time.monotonic() - started < 5
    assert "Fixture generation timed out" in capsys.readouterr().err
    assert_cleaned(launcher, children, ports)
    assert_restart(launcher, monkeypatch, seeding)


@pytest.mark.parametrize("cancel", [signal.SIGTERM, signal.SIGINT])
def test_signal_during_seeding_exits_child_and_allows_restart(
    launcher, monkeypatch, seeding, capsys, cancel
):
    seed, children, ports = seeding
    previous_handler = signal.getsignal(cancel)
    seed(block=True, cancel=cancel)
    started = time.monotonic()
    launcher.start(0)
    assert time.monotonic() - started < 5
    assert signal.getsignal(cancel) == previous_handler
    assert "UI startup cancelled during fixture generation" in capsys.readouterr().err
    assert_cleaned(launcher, children, ports)
    assert_restart(launcher, monkeypatch, seeding)


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
