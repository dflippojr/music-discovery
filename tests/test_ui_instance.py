"""Guard the disposable launcher's isolation and cleanup boundaries."""

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen

import psutil
import pytest
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


@pytest.fixture
def seeding(launcher, monkeypatch):
    """Real owned children with a pipe handshake and no data dependencies."""
    children = []
    ports = []
    original_server = launcher.ExclusiveHTTPServer
    original_popen = subprocess.Popen
    unrelated = launcher.DATA / "run-unrelated"
    unrelated.mkdir(parents=True, exist_ok=True)
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

    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", server)
    # Check that cleanup cannot race a live child still writing the bundle.
    original_remove = launcher.remove_bundle

    def remove(bundle):
        if children:
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
    del metadata["phase"]
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
        "phase": "ready",
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


def test_recover_allows_normal_restart(launcher, monkeypatch, stale, seeding):
    launcher.recover()
    monkeypatch.setattr(launcher, "psutil", psutil)
    metadata, _ = stale
    # assert_restart expects a previous port in the seeding fixture ledger.
    seed, children, ports = seeding
    ports.append(metadata["port"])
    assert_restart(launcher, monkeypatch, (seed, children, ports))


@pytest.mark.parametrize("phase", ["starting", "seeding", "ready"])
def test_recover_refuses_live_process_even_before_health(
    launcher, monkeypatch, stale, phase
):
    metadata, bundle = stale
    metadata["phase"] = phase
    launcher.HANDLE.write_text(json.dumps(metadata))
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


@pytest.mark.parametrize("phase", ["starting", "seeding", None])
def test_recover_unknown_fixture_identity(launcher, stale, phase):
    metadata, bundle = stale
    metadata["phase"] = phase
    launcher.HANDLE.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="identity|ownership"):
        launcher.recover()
    assert_preserved(launcher, bundle)


@pytest.mark.parametrize("live_child", [False, True])
def test_recover_requires_fixture_child_exit(launcher, monkeypatch, stale, live_child):
    metadata, bundle = stale
    metadata.update(phase="seeding", fixture={"pid": 54321, "created": 123456.0})
    launcher.HANDLE.write_text(json.dumps(metadata))

    def process(pid):
        if live_child and pid == 54321:
            return SimpleNamespace(create_time=lambda: 123456.0)
        raise launcher.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(launcher.psutil, "Process", process)
    if live_child:
        with pytest.raises(ValueError, match="still live"):
            launcher.recover()
        assert_preserved(launcher, bundle)
    else:
        launcher.recover()
        assert not launcher.HANDLE.exists()
        assert not bundle.exists()


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
]
GOOD_PORTS = [("1024", 1024), ("4180", 4180), ("04180", 4180), ("65535", 65535)]
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


@pytest.mark.parametrize("environ", [{}, {"UI_PORT": ""}], ids=["unset", "empty"])
def test_ui_port_defaults_to_4173(launcher, environ):
    assert launcher.ui_port(environ) == 4173


@pytest.mark.parametrize(("raw", "port"), GOOD_PORTS)
def test_ui_port_reads_the_variable(launcher, raw, port):
    assert launcher.ui_port({"UI_PORT": raw}) == port


@pytest.mark.parametrize("raw", BAD_PORTS, ids=lambda raw: ascii(raw)[1:13])
def test_ui_port_refuses_unusable_values(launcher, raw):
    with pytest.raises(ValueError, match="^UI_PORT must be a whole number"):
        launcher.ui_port({"UI_PORT": raw})


@pytest.mark.parametrize(
    ("ui_port", "argv", "port"),
    [(None, [], 4173), ("4180", [], 4180), ("4180", ["--port", "4190"], 4190)],
    ids=["default", "variable", "explicit-port-wins"],
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
        raise AssertionError("a process was spawned")

    monkeypatch.setattr(launcher.subprocess, "Popen", spawned)
    monkeypatch.setattr(launcher, "ExclusiveHTTPServer", spawned)
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "start"])
    assert launcher.main() == 1
    assert "UI_PORT must be a whole number" in capsys.readouterr().err
    assert not launcher.DATA.exists()


def test_stop_ignores_ui_port(launcher, monkeypatch, capsys):
    # Stop targets the port this checkout's handle records, never UI_PORT.
    monkeypatch.setenv("UI_PORT", "not-a-port")
    monkeypatch.setattr(sys, "argv", ["ui_instance.py", "stop"])
    assert launcher.main() == 0
    assert "No owned UI instance remains" in capsys.readouterr().out


@needs_node
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


@needs_node
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


@needs_node
@pytest.mark.parametrize("raw", ["abc", "80", "65536"])
def test_playwright_config_refuses_bad_ui_port(raw):
    result = load_playwright_config("playwright.config.mjs", UI_PORT=raw)
    assert result.returncode != 0
    assert "UI_PORT must be a whole number" in result.stderr
