"""Guard the disposable launcher's isolation and cleanup boundaries."""

import ast
import http.client
import importlib.util
import io
import itertools
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import psutil
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from playwright_config import load_playwright_config


@pytest.fixture
def launcher(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/ui_instance.py"
    spec = importlib.util.spec_from_file_location("ui_instance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "DATA", tmp_path / "ui-testing")
    monkeypatch.setattr(module, "HANDLE", module.DATA / "instance.json")
    return module


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


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


def test_reuse_address_server_on_port_creates_no_handle_or_bundle(launcher):
    # A competing server with the stdlib default reuse flag, as another
    # worktree's launcher would be; on Windows this once bound the same port.
    other = ThreadingHTTPServer(("127.0.0.1", 0), launcher.Handler)
    thread = threading.Thread(target=other.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(OSError):
            launcher.start(other.server_port)
    finally:
        other.shutdown()
        other.server_close()
        thread.join(timeout=5)
    assert not launcher.HANDLE.exists()
    assert not list(launcher.DATA.iterdir())


BUILD_THREAD = "ui-fixture-build"


@pytest.fixture
def building(launcher, monkeypatch):
    """A fake fixture build on the launcher's worker thread, with no data dependency."""
    bundles = []
    ports = []
    active = threading.Event()  # set while a build is still writing its bundle
    original_server = launcher.ExclusiveHTTPServer
    unrelated = launcher.DATA / "run-unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
    (unrelated / "keep.txt").write_text("keep")

    def server(*args, **kwargs):
        instance = original_server(*args, **kwargs)
        ports.append(instance.server_port)
        return instance

    def seed(*, fail=False, hold=0.0, cancel=None):
        def build(bundle):
            assert threading.current_thread().name == BUILD_THREAD
            active.set()
            try:
                bundles.append(bundle)
                bundle.mkdir()
                if cancel is not None:
                    # Python runs the handler on the main thread, as for a real signal.
                    signal.raise_signal(cancel)
                time.sleep(hold)  # stands in for CPU-bound work that cannot be cut
                (bundle / "catalog.json").write_text(
                    json.dumps({"ids": [100], "licenses": ["synthetic"]})
                )
                (bundle / "sources.json").write_text(
                    json.dumps({"sources": ["synthetic"]})
                )
                if fail:
                    raise ValueError("synthetic build failure")
            finally:
                active.clear()

        monkeypatch.setattr(launcher, "build_bundle", build)

    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", server)
    # Check that cleanup cannot race a build still writing the bundle.
    original_remove = launcher.remove_bundle

    def remove(bundle):
        assert not active.is_set(), "bundle removed while the build was writing it"
        original_remove(bundle)

    monkeypatch.setattr(launcher, "remove_bundle", remove)
    yield seed, bundles, ports


def assert_cleaned(launcher, bundles, ports):
    assert not [t for t in threading.enumerate() if t.name == BUILD_THREAD]
    assert not launcher.HANDLE.exists()
    assert not bundles[-1].exists()
    assert (launcher.DATA / "run-unrelated" / "keep.txt").read_text() == "keep"
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", ports[-1]))
    launcher.stop()  # safe to repeat after interrupted startup


def assert_restart(launcher, monkeypatch, building):
    seed, bundles, ports = building
    seed()
    previous_port = ports[-1]
    serving = threading.Event()
    original_serve = ThreadingHTTPServer.serve_forever
    errors = []
    observed = {}

    def serve(server, **kwargs):
        serving.set()
        original_serve(server, **kwargs)

    def stop_when_ready():
        try:
            if not serving.wait(5):
                raise TimeoutError("Restarted server did not become ready")
            metadata = json.loads(launcher.HANDLE.read_text())
            observed["instance"] = metadata["instance"]
            origin = f"http://127.0.0.1:{metadata['port']}"
            with urlopen(origin + "/__ui_health", timeout=5) as response:
                observed["health"] = json.load(response)
            bundle = launcher.DATA / metadata["instance"]
            observed["licenses"] = json.loads((bundle / "catalog.json").read_text())[
                "licenses"
            ]
            observed["sources"] = json.loads((bundle / "sources.json").read_text())[
                "sources"
            ]
            request = Request(
                origin + "/__ui_stop",
                method="POST",
                headers={"X-UI-Token": metadata["token"]},
            )
            with urlopen(request, timeout=5) as response:
                observed["stop_status"] = response.status
        except Exception as error:
            errors.append(error)
        finally:
            # Bound the test if readiness or an HTTP request fails.
            if launcher_server is not None:
                launcher_server.shutdown()

    # Capture the real server for failure teardown without signalling others.
    server_factory = launcher.ExclusiveHTTPServer
    launcher_server = None

    def capture_server(*args, **kwargs):
        nonlocal launcher_server
        launcher_server = server_factory(*args, **kwargs)
        return launcher_server

    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", capture_server)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve)
    worker = threading.Thread(target=stop_when_ready, daemon=True)
    worker.start()
    launcher.start(previous_port)
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert not errors
    assert observed["health"] == {"instance": observed["instance"]}
    assert observed["licenses"] == ["/synthetic/license"]
    assert observed["sources"] == ["/synthetic/track/100"]
    assert observed["stop_status"] == 204
    assert_cleaned(launcher, bundles, ports)


def test_partial_seed_failure_closes_port_and_removes_owned_files(launcher, building):
    seed, bundles, ports = building
    seed(fail=True)
    with pytest.raises(RuntimeError, match="synthetic build failure"):
        launcher.start(0)
    assert_cleaned(launcher, bundles, ports)
    assert list(launcher.DATA.iterdir()) == [launcher.DATA / "run-unrelated"]


def test_the_real_build_starts_no_process(launcher, monkeypatch, tmp_path):
    """The build waits on nothing outside the launcher, so a stop stays bounded.

    The export once stamped the bundle with git; a slow or stuck git (or a hook
    it starts that holds the pipe) kept the launcher past its stop window (#254).
    """
    spawned = []

    def refuse(*args, **kwargs):
        spawned.append(args[:1])
        raise AssertionError(f"the build started a process: {args[:1]}")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    monkeypatch.chdir(tmp_path)  # the export writes only inside the working directory
    bundle = tmp_path / "bundle"
    launcher.build_bundle(bundle)
    assert spawned == []
    source = json.loads((bundle / "SOURCE.json").read_text(encoding="utf-8"))
    assert source["sha"] == "synthetic"
    assert json.loads((bundle / "catalog.json").read_text())["count"] == 450


def test_seed_timeout_waits_for_the_build_is_nonzero_and_allows_restart(
    launcher, monkeypatch, building, capsys
):
    seed, bundles, ports = building
    assert launcher.FIXTURE_TIMEOUT == 30
    monkeypatch.setattr(launcher, "FIXTURE_TIMEOUT", 0.1)
    port = str(free_port())
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", "--port", port])
    seed(hold=0.5)
    started = time.monotonic()
    assert launcher.main() == 1
    # The timeout fires once the build returns: its files go only after that.
    assert 0.5 <= time.monotonic() - started < 5
    assert "Fixture generation timed out after 0.1 seconds" in capsys.readouterr().err
    assert_cleaned(launcher, bundles, ports)
    assert_restart(launcher, monkeypatch, building)


@pytest.mark.parametrize("cancel", [signal.SIGTERM, signal.SIGINT])
def test_signal_during_the_build_cancels_is_nonzero_and_allows_restart(
    launcher, monkeypatch, building, capsys, cancel
):
    seed, bundles, ports = building
    previous_handler = signal.getsignal(cancel)
    printed = []

    def recording_print(*args, **kwargs):
        # Whether the build was still running when the launcher announced the stop.
        printed.append((args[0], building_now()))
        print(*args, **kwargs)

    def building_now():
        return any(t.name == BUILD_THREAD for t in threading.enumerate())

    monkeypatch.setattr(launcher, "print", recording_print, raising=False)
    seed(hold=0.5, cancel=cancel)
    port = str(free_port())
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", "--port", port])
    started = time.monotonic()
    assert launcher.main() == 1  # #254: a stop or a timeout exits non-zero
    assert time.monotonic() - started < 5
    assert signal.getsignal(cancel) == previous_handler
    assert "UI startup cancelled during fixture generation" in capsys.readouterr().err
    # The stop is announced while the build runs; files go once it has returned.
    assert printed == [("UI startup cancelled during fixture generation", True)]
    assert_cleaned(launcher, bundles, ports)
    assert_restart(launcher, monkeypatch, building)


@pytest.mark.parametrize("cancel", [signal.SIGTERM, signal.SIGINT])
def test_signal_while_serving_cleans_up(
    launcher, monkeypatch, building, capsys, cancel
):
    seed, bundles, ports = building
    seed()
    previous_handler = signal.getsignal(cancel)
    original_serve = ThreadingHTTPServer.serve_forever
    start_returned = threading.Event()
    workers = []
    # Hold the server: garbage collection would close a socket start() left open,
    # and assert_cleaned's port check would then pass.
    servers = []
    errors = []
    observed = {}

    def signal_once_serving(server):
        try:
            observed["instance"] = json.loads(launcher.HANDLE.read_text())["instance"]
            origin = f"http://127.0.0.1:{server.server_port}"
            with urlopen(origin + "/__ui_health", timeout=5) as response:
                observed["health"] = json.load(response)
            # raise_signal, not os.kill: on Windows os.kill ends the process.
            signal.raise_signal(cancel)
            if not start_returned.wait(5):
                raise TimeoutError("The signal did not stop the server")
        except Exception as error:
            errors.append(error)
        finally:
            # Bound the test if the signal or the health request fails.
            server.shutdown()

    def serve(server, **kwargs):
        servers.append(server)
        worker = threading.Thread(
            target=signal_once_serving, args=[server], daemon=True
        )
        workers.append(worker)
        worker.start()
        original_serve(server, **kwargs)

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve)
    try:
        launcher.start(0)
    finally:
        start_returned.set()
    workers[0].join(timeout=5)
    assert not workers[0].is_alive()
    assert not errors
    assert observed["health"] == {"instance": observed["instance"]}
    assert signal.getsignal(cancel) == previous_handler
    assert f"UI_URL=http://127.0.0.1:{ports[-1]}/" in capsys.readouterr().out
    assert_cleaned(launcher, bundles, ports)


def test_signal_after_the_fixture_cancels_before_ready(
    launcher, monkeypatch, building, capsys
):
    seed, bundles, ports = building
    seed()
    original_build = launcher.build_until_stopped

    def finished_then_signalled(bundle, stopping):
        finished = original_build(bundle, stopping)
        signal.raise_signal(signal.SIGTERM)
        return finished

    def served(server, **kwargs):
        raise AssertionError("The server started after the cancellation")

    monkeypatch.setattr(launcher, "build_until_stopped", finished_then_signalled)
    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", served)
    with pytest.raises(launcher.StartupCancelled):
        launcher.start(0)
    captured = capsys.readouterr()
    assert "UI startup cancelled\n" in captured.err
    assert "UI_URL=" not in captured.out
    assert_cleaned(launcher, bundles, ports)


def start_tracing_lines(launcher, monkeypatch, on_line):
    """Run `start` through main(), calling on_line before each launcher line.

    Only this thread's lines are traced. Returns main()'s exit status.
    """
    port = str(free_port())
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", "--port", port])

    def local(frame, event, _arg):
        if event == "line":
            on_line(frame.f_lineno)
        return local

    def enter(frame, _event, _arg):
        return local if frame.f_code.co_filename == launcher.__file__ else None

    previous = sys.gettrace()  # coverage's tracer, when it runs one
    sys.settrace(enter)
    try:
        return launcher.main()
    finally:
        sys.settrace(previous)


@pytest.mark.parametrize("cancel", [signal.SIGTERM, signal.SIGINT])
def test_signal_at_any_startup_line_stops_the_launcher(
    launcher, monkeypatch, building, capsys, cancel
):
    """Class check for #213 and #254: a signal at any startup line has one outcome.

    No line between the handlers and serving loses a signal. A signal at or
    before the readiness commit (the last stop check before UI_URL=) cancels:
    the diagnostic, no ready line, exit 1. After it, the launcher prints both
    ready lines, then stops as when serving: exit 0. Either way it cleans up.
    It finds the lines by running start(), so a line added later is covered too.
    """
    seed, bundles, ports = building
    seed()
    previous_handler = signal.getsignal(cancel)
    original_serve = ThreadingHTTPServer.serve_forever
    lines = {}  # in the order they first run, which is not file order
    served = []

    def handlers_installed():
        return signal.getsignal(cancel) != previous_handler

    def record(line):
        if handlers_installed() and not served:
            lines[line] = True

    monkeypatch.setattr(
        ThreadingHTTPServer, "serve_forever", lambda *_, **__: served.append(True)
    )
    assert start_tracing_lines(launcher, monkeypatch, record) == 0
    capsys.readouterr()
    source = Path(launcher.__file__).read_text(encoding="utf-8").splitlines()
    reached = [source[line - 1] for line in lines]
    assert any("UI_URL=" in text for text in reached)
    assert any("UI_INSTANCE=" in text for text in reached)
    assert any("serve_forever" in text for text in reached)
    commits = [n for n, text in enumerate(source, 1) if "# readiness commit" in text]
    assert len(commits) == 1, "start() must mark one readiness commit"
    commit = commits[0]
    assert "stopping.is_set()" in source[commit - 1]
    order = list(lines)
    url = next(n for n in order if "UI_URL=" in source[n - 1])
    assert commit in order and order.index(commit) < order.index(url)
    assert not any(
        "stopping.is_set()" in source[n - 1]
        for n in order[order.index(commit) + 1 : order.index(url)]
    ), "the readiness commit must be the last stop check before UI_URL="

    overran = []

    def serve(server, **kwargs):
        def expired():
            overran.append(True)
            server.shutdown()

        # Bound the run when the signal was lost, as the launcher would serve on.
        watchdog = threading.Timer(5, expired)
        watchdog.daemon = True
        watchdog.start()
        try:
            original_serve(server, **kwargs)
        finally:
            watchdog.cancel()

    def signal_at(target):
        fired = []

        def fire(line):
            # A helper's line also runs before the handlers, where the signal would
            # end pytest.
            if line == target and handlers_installed() and not fired:
                fired.append(line)
                signal.raise_signal(cancel)

        overran.clear()
        status = start_tracing_lines(launcher, monkeypatch, fire)
        captured = capsys.readouterr()
        assert fired, f"Line {target} was not reached"
        assert_cleaned(launcher, bundles, ports)
        announced = "UI_URL=" in captured.out, "UI_INSTANCE=" in captured.out
        cancelled = "UI startup cancelled" in captured.err
        return bool(overran), (status, announced, cancelled)

    monkeypatch.setattr(ThreadingHTTPServer, "serve_forever", serve)
    lost, wrong = [], []
    for position, line in enumerate(order):
        overran_now, outcome = signal_at(line)
        label = f"{line}: {source[line - 1].strip()}"
        if overran_now:
            lost.append(label)
        before_commit = position <= order.index(commit)
        # At or before the commit: cancel, exit 1. After it: ready lines, exit 0.
        expected = (
            (1, (False, False), True) if before_commit else (0, (True, True), False)
        )
        if outcome != expected:
            wrong.append(f"{label} -> {outcome}, expected {expected}")
    assert not lost, "A signal at these lines was lost:\n" + "\n".join(lost)
    assert not wrong, "Wrong outcome for a signal at:\n" + "\n".join(wrong)
    assert signal.getsignal(cancel) == previous_handler


def cancel_after_changing_handle(launcher, monkeypatch, change):
    original_build = launcher.build_until_stopped

    def cancelled(bundle, stopping):
        original_build(bundle, stopping)
        change()
        raise launcher.StartupCancelled

    monkeypatch.setattr(launcher, "build_until_stopped", cancelled)


def test_start_leaves_another_instances_handle(launcher, monkeypatch, building):
    seed, bundles, ports = building
    seed()
    # Indented, so a rewrite in start()'s own json.dumps format would differ.
    foreign = json.dumps(
        {
            "instance": "run-fedcba9876543210",
            "pid": 12345,
            "port": 4173,
            "token": "synthetic-token-0123456789",
        },
        indent=2,
    ).encode()
    cancel_after_changing_handle(
        launcher, monkeypatch, lambda: launcher.HANDLE.write_bytes(foreign)
    )
    with pytest.raises(launcher.StartupCancelled):
        launcher.start(0)
    assert launcher.HANDLE.read_bytes() == foreign
    launcher.HANDLE.unlink()  # assert_cleaned checks everything else start() owns
    assert_cleaned(launcher, bundles, ports)


def test_start_exits_cleanly_when_its_handle_is_gone(launcher, monkeypatch, building):
    seed, bundles, ports = building
    seed()
    cancel_after_changing_handle(launcher, monkeypatch, launcher.HANDLE.unlink)
    with pytest.raises(launcher.StartupCancelled):
        launcher.start(0)
    assert_cleaned(launcher, bundles, ports)


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


def test_stop_refuses_another_instance_on_the_same_port(launcher, monkeypatch, stale):

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


def test_stop_accepts_authenticated_legacy_handle(launcher, monkeypatch, stale):
    metadata, bundle = stale
    del metadata["created"]
    launcher.HANDLE.write_text(json.dumps(metadata))
    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"instance": metadata["instance"]}).encode()

    def request(url, **kwargs):
        requests.append(url)
        if isinstance(url, Request):
            assert url.get_header("X-ui-token") == metadata["token"]
            launcher.remove_bundle(bundle)
            launcher.HANDLE.unlink()
        return Response()

    monkeypatch.setattr(launcher, "urlopen", request)
    launcher.stop()
    assert len(requests) == 2
    assert not launcher.HANDLE.exists()
    assert not bundle.exists()


@pytest.fixture
def stale(launcher, monkeypatch):
    """A synthetic exited process ledger and an unrelated directory sentinel."""
    launcher.DATA.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    metadata = {
        "instance": "run-0123456789abcdef",
        "pid": 12345,
        "created": 123456.0,
        "port": port,
        "token": "synthetic-token-0123456789",
    }
    launcher.HANDLE.write_text(json.dumps(metadata))
    bundle = launcher.DATA / metadata["instance"]
    bundle.mkdir()
    (bundle / "synthetic.txt").write_text("synthetic")
    unrelated = launcher.DATA / "run-unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep")

    def exited(pid):
        raise launcher.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(
        launcher,
        "psutil",
        SimpleNamespace(
            Process=exited,
            NoSuchProcess=psutil.NoSuchProcess,
            AccessDenied=psutil.AccessDenied,
            Error=psutil.Error,
        ),
    )
    return metadata, bundle


def assert_preserved(launcher, bundle):
    assert launcher.HANDLE.exists()
    assert (bundle / "synthetic.txt").read_text() == "synthetic"
    assert (launcher.DATA / "run-unrelated" / "keep.txt").read_text() == "keep"


STOP_TOKEN = "synthetic-token-0123456789"


@pytest.fixture
def stop_endpoint(launcher, tmp_path):
    """The launcher's real server on a thread, built as start() builds it."""
    server = launcher.ExclusiveHTTPServer(
        ("127.0.0.1", 0), partial(launcher.Handler, directory=str(tmp_path))
    )
    server.instance = "run-0123456789abcdef"
    server.token = STOP_TOKEN
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield server, thread
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def post(server, path, headers):
    request = Request(
        f"http://127.0.0.1:{server.server_port}{path}", method="POST", headers=headers
    )
    return urlopen(request, timeout=5)


@pytest.mark.parametrize(
    ("path", "headers"),
    [
        ("/__ui_stop", {}),
        ("/__ui_stop", {"X-UI-Token": "wrong-synthetic-token"}),
        ("/other", {"X-UI-Token": STOP_TOKEN}),
    ],
    ids=["missing", "wrong", "path"],
)
def test_stop_endpoint_refuses_missing_or_wrong_token(stop_endpoint, path, headers):
    server, thread = stop_endpoint
    with pytest.raises(HTTPError) as refused:
        post(server, path, headers)
    assert refused.value.code == 403
    # A shutdown would take one 0.01 s poll; a health request alone could beat it.
    # Stay connected meanwhile: a handler that went on to stop the server after its
    # 403 would otherwise die writing to a closed socket and hide the shutdown.
    with refused.value:
        thread.join(timeout=0.2)
    assert thread.is_alive()
    origin = f"http://127.0.0.1:{server.server_port}"
    with urlopen(origin + "/__ui_health", timeout=5) as response:
        assert json.load(response) == {"instance": server.instance}


@pytest.mark.parametrize("token", [b"\xc3\xa9", b"\xff"], ids=["utf-8", "latin-1"])
def test_stop_token_non_ascii_gets_403(stop_endpoint, token):
    server, thread = stop_endpoint
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.putrequest("POST", "/__ui_stop")
    conn.putheader("X-UI-Token", token)
    conn.endheaders()
    assert conn.getresponse().status == 403
    conn.close()
    assert thread.is_alive()


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_nul_in_path_gets_404(stop_endpoint, method):
    server, _ = stop_endpoint
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    conn.request(method, "/%00")
    assert conn.getresponse().status == 404
    conn.close()


def send_raw(server, request):
    """Send exact bytes; a handler that raised answers with an empty reply."""
    with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as conn:
        conn.sendall(request)
        reply = b""
        # The handler has finished, and reported any error, by the time of EOF.
        while chunk := conn.recv(4096):
            reply += chunk
    return reply


def wire_request(method, path, header, value):
    return b"%s %s HTTP/1.1\r\n%s: %s\r\nConnection: close\r\n\r\n" % (
        method,
        path,
        header,
        value,
    )


@pytest.fixture
def handler_errors(stop_endpoint):
    """Exceptions that escape a request handler, which would drop the connection."""
    server, _ = stop_endpoint
    errors = []
    server.handle_error = lambda *_: errors.append(sys.exception())
    return errors


# Lean on the bytes that have broken the handler: NUL and anything above 0x7f.
WIRE_BYTES = st.lists(
    st.one_of(st.sampled_from([0x00, 0x80, 0xC3, 0xFF]), st.integers(0, 255)),
    max_size=20,
).map(bytes)
FIELD_BYTES = WIRE_BYTES.filter(lambda raw: not set(raw) & set(b"\r\n"))
ESCAPED_BYTES = WIRE_BYTES.map(
    lambda raw: "".join(f"%{byte:02x}" for byte in raw).encode()
)
FUZZ_SETTINGS = settings(
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
)


@FUZZ_SETTINGS
@given(token=FIELD_BYTES)
def test_every_wrong_stop_token_gets_403(stop_endpoint, handler_errors, token):
    server, thread = stop_endpoint
    assume(token.strip() != STOP_TOKEN.encode())
    handler_errors.clear()
    reply = send_raw(server, wire_request(b"POST", b"/__ui_stop", b"X-UI-Token", token))
    assert handler_errors == []
    assert reply.split()[1] == b"403"
    assert thread.is_alive()


@FUZZ_SETTINGS
@given(
    method=st.sampled_from([b"GET", b"HEAD", b"POST"]),
    path=st.one_of(st.just(b"__ui_stop"), FIELD_BYTES, ESCAPED_BYTES),
    header=st.sampled_from([b"Host", b"If-Modified-Since", b"X-UI-Token"]),
    value=FIELD_BYTES,
)
def test_every_request_gets_an_http_reply(
    stop_endpoint, handler_errors, method, path, header, value
):
    """The class behind #215: no client bytes may raise inside the handler."""
    server, thread = stop_endpoint
    assume(value.strip() != STOP_TOKEN.encode())
    handler_errors.clear()
    reply = send_raw(server, wire_request(method, b"/" + path, header, value))
    assert handler_errors == []
    assert reply.startswith(b"HTTP/1.")
    assert thread.is_alive()


def test_stop_endpoint_accepts_the_token_and_stops(stop_endpoint):
    server, thread = stop_endpoint
    with post(server, "/__ui_stop", {"X-UI-Token": STOP_TOKEN}) as response:
        assert response.status == 204
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_stop_reports_incomplete_teardown(launcher, monkeypatch, stale):
    metadata, bundle = stale
    health = json.dumps({"instance": metadata["instance"]}).encode()
    monkeypatch.setattr(launcher, "urlopen", lambda *_, **__: io.BytesIO(health))
    # Replace the module's name for time: patching time.monotonic itself would
    # also move the clock under pytest and every other thread.
    clock = itertools.count(step=6)
    slept = []
    monkeypatch.setattr(
        launcher,
        "time",
        SimpleNamespace(monotonic=lambda: next(clock), sleep=slept.append),
    )
    with pytest.raises(ValueError, match="Teardown incomplete"):
        launcher.stop()
    assert slept == [0.1, 0.1]  # read 6 and 12 before the 15 s deadline, then 18
    assert_preserved(launcher, bundle)


def test_stop_reports_a_bundle_that_outlives_the_handle(launcher, monkeypatch, stale):
    metadata, bundle = stale
    health = json.dumps({"instance": metadata["instance"]}).encode()

    def request(url, **kwargs):
        if isinstance(url, Request):
            launcher.HANDLE.unlink()
        return io.BytesIO(health)

    monkeypatch.setattr(launcher, "urlopen", request)
    with pytest.raises(ValueError, match="Teardown incomplete"):
        launcher.stop()
    assert (bundle / "synthetic.txt").read_text() == "synthetic"


def test_recover_dead_process_free_port_and_repeat(launcher, stale, capsys):
    metadata, bundle = stale
    launcher.recover()
    launcher.recover()
    assert not launcher.HANDLE.exists()
    assert not bundle.exists()
    assert (launcher.DATA / "run-unrelated" / "keep.txt").read_text() == "keep"
    assert "Recovered" in capsys.readouterr().out
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", metadata["port"]))


def test_recover_allows_normal_restart(launcher, monkeypatch, stale, building):
    launcher.recover()
    monkeypatch.setattr(launcher, "psutil", psutil)
    metadata, _ = stale
    # assert_restart expects a previous port in the building fixture ledger.
    seed, bundles, ports = building
    ports.append(metadata["port"])
    assert_restart(launcher, monkeypatch, (seed, bundles, ports))


def test_recover_refuses_live_process_even_before_health(launcher, monkeypatch, stale):
    metadata, bundle = stale
    monkeypatch.setattr(
        launcher.psutil,
        "Process",
        lambda pid: SimpleNamespace(create_time=lambda: metadata["created"]),
    )
    with pytest.raises(ValueError, match="still live"):
        launcher.recover()
    assert_preserved(launcher, bundle)


@pytest.mark.parametrize("unknown", [False, True])
def test_recover_refuses_reused_or_unverifiable_pid(
    launcher, monkeypatch, stale, unknown
):
    _, bundle = stale

    def process(pid):
        if unknown:
            raise launcher.psutil.AccessDenied(pid)
        return SimpleNamespace(create_time=lambda: 999999.0)

    monkeypatch.setattr(launcher.psutil, "Process", process)
    with pytest.raises(ValueError, match="identity|PID reused"):
        launcher.recover()
    assert_preserved(launcher, bundle)


def test_recover_refuses_another_server_on_port(launcher, stale):
    metadata, bundle = stale
    server = ThreadingHTTPServer(("127.0.0.1", metadata["port"]), launcher.Handler)
    server.instance = "run-other"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ValueError, match="another instance"):
            launcher.recover()
        assert_preserved(launcher, bundle)
        with urlopen(f"http://127.0.0.1:{metadata['port']}/__ui_health") as response:
            assert json.load(response) == {"instance": "run-other"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("instance", "../run-unrelated"),
        ("instance", "run-/../../outside"),
        ("instance", 1),
        ("pid", True),
        ("pid", -1),
        ("pid", "12345"),
        ("pid", 10**1000),
        ("created", None),
        ("created", float("nan")),
        ("created", float("inf")),
        ("created", 10**1000),
        ("port", 0),
        ("port", 65536),
        ("port", "4173"),
        ("port", True),
        ("token", "bad\r\nheader"),
        ("token", None),
    ],
)
@pytest.mark.parametrize("action", ["recover", "stop"])
def test_malformed_handle_preserves_resources(launcher, stale, field, value, action):
    metadata, bundle = stale
    metadata[field] = value
    launcher.HANDLE.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="Invalid"):
        getattr(launcher, action)()
    assert_preserved(launcher, bundle)


@pytest.mark.parametrize("raw", ["{", "null", "[]", "{}"])
def test_recover_invalid_json_handle(launcher, stale, raw):
    _, bundle = stale
    launcher.HANDLE.write_text(raw)
    with pytest.raises(ValueError):
        launcher.recover()
    assert_preserved(launcher, bundle)


@pytest.mark.parametrize(
    ("fields", "named"),
    [
        ({"phase": "starting"}, 'phase "starting"'),
        (
            {"phase": "ready", "fixture": {"pid": 54321, "created": 123456.0}},
            'phase "ready", fixture {"pid": 54321',
        ),
        ({"fixture": None}, "fixture null"),
        ({"phase": "bogus"}, 'phase "bogus"'),
        ({"phase": None}, "phase null"),
    ],
)
def test_recover_refuses_a_handle_from_the_child_process_launcher(
    launcher, stale, capsys, fields, named
):
    """Its fixture child may outlive it, so the launcher's exit proves nothing."""
    metadata, bundle = stale
    metadata.update(fields)
    launcher.HANDLE.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="older launcher") as refused:
        launcher.recover()
    assert named in str(refused.value)
    assert_preserved(launcher, bundle)


def test_recover_preflights_all_bundle_entries(launcher, stale):
    _, bundle = stale
    (bundle / "nested").mkdir()
    with pytest.raises(ValueError, match="Unexpected"):
        launcher.recover()
    assert_preserved(launcher, bundle)


@pytest.mark.parametrize("target", ["bundle", "entry", "handle", "data"])
def test_recover_refuses_symlinks(launcher, stale, tmp_path, monkeypatch, target):
    _, bundle = stale
    if target == "bundle":
        path = bundle
    elif target == "entry":
        path = bundle / "synthetic.txt"
    elif target == "handle":
        path = launcher.HANDLE
    else:
        path = launcher.DATA
    destination = tmp_path / "outside"
    path.rename(destination)
    try:
        path.symlink_to(destination, target_is_directory=destination.is_dir())
    except OSError:
        destination.rename(path)
        # Windows hosts without symlink privileges still exercise refusal;
        # Linux CI exercises actual filesystem symlinks.
        original = Path.is_symlink
        monkeypatch.setattr(
            Path, "is_symlink", lambda self: self == path or original(self)
        )
    with pytest.raises(ValueError, match="outside|Unexpected|Symlinked"):
        launcher.recover()
    assert_preserved(launcher, bundle)


def test_recover_missing_bundle_is_safe(launcher, stale):
    _, bundle = stale
    (bundle / "synthetic.txt").unlink()
    bundle.rmdir()
    launcher.recover()
    assert not launcher.HANDLE.exists()


def test_recover_refuses_changed_handle(launcher, monkeypatch, stale):
    metadata, bundle = stale
    original = launcher.require_exited

    def changed(identity):
        original(identity)
        metadata["instance"] = "run-fedcba9876543210"
        launcher.HANDLE.write_text(json.dumps(metadata))

    monkeypatch.setattr(launcher, "require_exited", changed)
    with pytest.raises(ValueError, match="handle changed"):
        launcher.recover()
    assert_preserved(launcher, bundle)


def test_start_preserves_stale_handle_until_explicit_recovery(launcher, stale):
    metadata, bundle = stale
    with pytest.raises(ValueError, match="verified recover"):
        launcher.start(metadata["port"])
    assert_preserved(launcher, bundle)


def test_recover_cli_reports_failure_without_cleanup(
    launcher, monkeypatch, stale, capsys
):
    metadata, bundle = stale
    del metadata["created"]  # old handles cannot establish process identity
    launcher.HANDLE.write_text(json.dumps(metadata))
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "recover"])
    assert launcher.main() == 1
    assert "preserve handle for investigation" in capsys.readouterr().err
    assert_preserved(launcher, bundle)


ROOT = Path(__file__).resolve().parents[1]
# Values UI_PORT must refuse: not a plain number, out of range, or privileged.
BAD_PORTS = [
    "abc",
    "4180.0",
    " 4180",
    "4180 ",
    "+4180",
    "-1",
    "1e4",
    "0x1054",
    "٤١٨٠",  # Arabic-Indic digits that int() would accept
    "0",
    "80",
    "1023",
    "65536",
    "9" * 400,
    "9" * 5000,  # past int()'s digit limit
]
GOOD_PORTS = [
    ("1024", 1024),
    ("4180", 4180),
    ("04180", 4180),
    ("0" * 5000 + "4180", 4180),
    ("65535", 65535),
]


@pytest.mark.parametrize("environ", [{}, {"UI_PORT": ""}], ids=["unset", "empty"])
def test_ui_port_defaults_to_4173(launcher, environ):
    assert launcher.ui_port(environ) == 4173


@pytest.mark.parametrize(("raw", "port"), GOOD_PORTS, ids=lambda v: str(v)[:12])
def test_ui_port_reads_the_variable(launcher, raw, port):
    assert launcher.ui_port({"UI_PORT": raw}) == port


@pytest.mark.parametrize("raw", BAD_PORTS, ids=lambda raw: ascii(raw)[1:13])
def test_ui_port_refuses_unusable_values(launcher, raw):
    with pytest.raises(ValueError, match="^UI_PORT must be a whole number"):
        launcher.ui_port({"UI_PORT": raw})


@pytest.mark.parametrize(
    ("ui_port", "argv", "port"),
    [
        (None, [], 4173),
        ("4180", [], 4180),
        ("4180", ["--port", "4190"], 4190),
        (None, ["--port", "4180"], 4180),
    ],
    ids=["default", "variable", "explicit-port-wins", "flag"],
)
def test_start_cli_binds_the_ui_port(launcher, monkeypatch, ui_port, argv, port):
    if ui_port is None:
        monkeypatch.delenv("UI_PORT", raising=False)
    else:
        monkeypatch.setenv("UI_PORT", ui_port)
    started = []
    monkeypatch.setattr(launcher, "start", started.append)
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", *argv])
    assert launcher.main() == 0
    assert started == [port]


@pytest.mark.parametrize("raw", ["abc", "80", "65536"])
def test_start_cli_refuses_bad_ui_port_before_anything_starts(
    launcher, monkeypatch, capsys, raw
):
    monkeypatch.setenv("UI_PORT", raw)

    def spawned(*args, **kwargs):
        raise AssertionError("a fixture was built or a socket was bound")

    monkeypatch.setattr(launcher, "build_bundle", spawned)
    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", spawned)
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start"])
    assert launcher.main() == 1
    assert "UI_PORT must be a whole number" in capsys.readouterr().err
    assert not launcher.DATA.exists()


@pytest.mark.parametrize("raw", BAD_PORTS, ids=lambda raw: ascii(raw)[1:13])
def test_port_flag_refuses_what_ui_port_refuses_before_binding(
    launcher, monkeypatch, capsys, raw
):
    # --port 0 used to bind a random port and --port 70000 crashed in bind (#154).
    def spawned(*args, **kwargs):
        raise AssertionError("a fixture was built or a socket was bound")

    monkeypatch.setattr(launcher, "build_bundle", spawned)
    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", spawned)
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start", "--port", raw])
    with pytest.raises(SystemExit) as exit_info:
        launcher.main()
    assert exit_info.value.code == 2
    assert (
        "argument --port: must be a whole number from 1024 to 65535"
        in capsys.readouterr().err
    )
    assert not launcher.DATA.exists()


def test_every_port_flag_takes_the_ui_port_rule():
    # A --port flag typed as a plain int skips the range rule (#154), so every
    # such flag in the repo must name port_argument as its type.
    flags = {}
    for folder in ("scripts", ".claude/skills", "src"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_argument"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "--port"
                ):
                    kinds = {kw.arg: ast.unparse(kw.value) for kw in node.keywords}
                    flags[f"{path.relative_to(ROOT)}:{node.lineno}"] = kinds.get("type")
    # The launcher and the verify skill both take --port, so the scan found them.
    assert len(flags) >= 2, flags
    offenders = {
        where: kind
        for where, kind in flags.items()
        if not str(kind).endswith("port_argument")
    }
    assert offenders == {}


def test_stop_ignores_ui_port(launcher, monkeypatch, capsys):
    # Stop targets the port this checkout's handle records, never UI_PORT.
    monkeypatch.setenv("UI_PORT", "not-a-port")
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "stop"])
    assert launcher.main() == 0
    assert "No owned UI instance remains" in capsys.readouterr().out


def test_javascript_ui_port_matches_python(launcher):
    cases = ["", *[raw for raw, _ in GOOD_PORTS], *BAD_PORTS]
    script = (
        'import { uiPort } from "./scripts/ui-port.mjs";'
        "const cases = JSON.parse(process.env.CASES);"
        "console.log(JSON.stringify(cases.map((raw) => {"
        "  try { return uiPort({ UI_PORT: raw }); } catch (e) { return e.message; }"
        "})));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        env={**os.environ, "CASES": json.dumps(cases)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )

    def python(raw):
        try:
            return launcher.ui_port({"UI_PORT": raw})
        except ValueError as error:
            return str(error)

    assert json.loads(result.stdout) == [python(raw) for raw in cases]


@pytest.mark.parametrize(("env", "port"), [({}, 4173), ({"UI_PORT": "4180"}, 4180)])
def test_playwright_config_uses_ui_port_on_loopback(env, port):
    result = load_playwright_config("playwright.config.mjs", **env)
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["use"]["baseURL"] == f"http://127.0.0.1:{port}"
    assert config["webServer"]["url"] == f"http://127.0.0.1:{port}/__ui_health"
    # The launcher reads UI_PORT itself, so the command stays as CI runs it.
    assert config["webServer"]["command"] == "npm run ui:start"
    assert config["webServer"]["reuseExistingServer"] is False


@pytest.mark.parametrize("raw", ["abc", "80", "65536"])
def test_playwright_config_refuses_bad_ui_port(raw):
    result = load_playwright_config("playwright.config.mjs", UI_PORT=raw)
    assert result.returncode != 0
    assert "UI_PORT must be a whole number" in result.stderr


# ui:stop and ui:recover act on whichever instance the checkout's shared handle
# names, so every code path that runs them must show it only ever stops an
# instance it started (#148). A new path fails here until it says how.
TEARDOWN_CALLERS = {
    "package.json": "defines ui:stop and ui:recover",
    ".claude/skills/verify/scripts/verify.py": (
        "test_verify_leaves_an_instance_it_did_not_start_running"
    ),
    "scripts/ci-local.sh": (
        "test_ci_local_ui_leaves_an_instance_it_did_not_start_running"
    ),
    "tests/ui/teardown.mjs": (
        "Playwright's globalTeardown, which runs only after its webServer, "
        "npm run ui:start, started the instance and became ready"
    ),
    ".github/workflows/ci.yml": "the ui job's fresh checkout holds no other instance",
}
TEARDOWN_CALL = re.compile(
    r"ui:(stop|recover)|ui-command\.mjs|ui_instance\.py\W{0,4}(stop|recover)"
)


def test_every_teardown_caller_shows_it_stops_only_its_own_instance():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    ).stdout.decode("utf-8")
    callers = {
        path
        for path in tracked.split("\0")
        if path
        and not path.endswith(".md")
        and not path.startswith("tests/test_")
        and (root / path).is_file()
        and TEARDOWN_CALL.search(
            (root / path).read_text(encoding="utf-8", errors="replace")
        )
    }
    assert callers == set(TEARDOWN_CALLERS), (
        "Add each new caller of ui:stop or ui:recover to TEARDOWN_CALLERS with "
        "the test that shows it leaves another instance running; drop stale ones"
    )
    tests = "".join(
        path.read_text(encoding="utf-8") for path in (root / "tests").glob("test_*.py")
    )
    for proof in TEARDOWN_CALLERS.values():
        if proof.startswith("test_"):
            assert f"def {proof}(" in tests, f"{proof} no longer exists"
