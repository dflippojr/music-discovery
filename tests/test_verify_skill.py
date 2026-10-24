"""Guard the verify skill's doctor checks and its cleanup after a failed drive."""

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import psutil
import pytest
from playwright_config import load_playwright_config

SCRIPTS = Path(__file__).resolve().parents[1] / ".claude/skills/verify/scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(
        f"verify_{name}", SCRIPTS / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


doctor = load("doctor")
verify = load("verify")

CATALOG = {
    "ids": [100, 101],
    "titles": ["Synthetic track 100", "Synthetic track 101"],
    "artists": ["artist0", "artist1"],
    "licenses": ["/synthetic/license"],
}
SOURCES = {"sources": ["/synthetic/track/100", "/synthetic/track/101"]}


@pytest.fixture
def serve():
    servers = []

    def start(routes):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path not in routes:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(routes[self.path]).encode())

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_port}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def healthy_routes():
    return {
        "/__ui_health": {"instance": "run-0123456789abcdef"},
        "/catalog.json": CATALOG,
        "/sources.json": SOURCES,
    }


def by_name(checks):
    return {check.name: check for check in checks}


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_doctor_passes_a_healthy_synthetic_instance(serve, monkeypatch, capsys):
    url = serve(healthy_routes())
    checks = doctor.run_checks(url, "run-0123456789abcdef")
    assert all(check.ok for check in checks), [str(check) for check in checks]
    monkeypatch.setattr(doctor, "handle_instance", lambda: "run-0123456789abcdef")
    assert doctor.main(["--url", url]) == 0
    assert capsys.readouterr().out.count("PASS ") == 4


def test_doctor_fails_when_not_ready(monkeypatch, capsys):
    def refuse(url):
        raise URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(doctor, "fetch_json", refuse)
    url = f"http://127.0.0.1:{free_port()}"
    checks = by_name(doctor.run_checks(url, "run-0123456789abcdef"))
    assert not checks["ready"].ok
    assert not checks["loopback"].ok
    assert doctor.main(["--url", url, "--instance", "run-0123456789abcdef"]) == 1
    assert "FAIL ready" in capsys.readouterr().out


def test_doctor_fails_on_another_instance(serve):
    url = serve(healthy_routes())
    check = by_name(doctor.run_checks(url, "run-ffffffffffffffff"))["ready"]
    assert not check.ok and "expected run-ffffffffffffffff" in check.detail


def test_doctor_fails_on_non_loopback_listener(serve):
    url = serve(healthy_routes())
    checks = by_name(doctor.run_checks(url, listeners=lambda port: ["0.0.0.0"]))
    assert not checks["loopback"].ok and "0.0.0.0" in checks["loopback"].detail
    remote = doctor.check_loopback("http://192.0.2.1:4173", lambda port: [])
    assert not remote.ok


@pytest.mark.parametrize(
    "routes",
    [
        {"/catalog.json": CATALOG},
        {
            "/catalog.json": {**CATALOG, "titles": ["Real song"]},
            "/sources.json": SOURCES,
        },
        {
            "/catalog.json": {**CATALOG, "licenses": ["https://example.org/license"]},
            "/sources.json": SOURCES,
        },
    ],
    ids=["missing", "non-synthetic", "external-link"],
)
def test_doctor_fails_without_synthetic_seed(serve, routes):
    url = serve({"/__ui_health": {"instance": "run-0123456789abcdef"}, **routes})
    assert not by_name(doctor.run_checks(url))["seed"].ok


def test_doctor_fails_on_external_configuration(serve, monkeypatch):
    monkeypatch.setenv("FMA_TOKEN", "synthetic-forbidden-value")
    check = by_name(doctor.run_checks(serve(healthy_routes())))["environment"]
    assert not check.ok and "FMA_TOKEN" in check.detail


@pytest.mark.skipif(shutil.which("node") is None, reason="launcher needs Node")
def test_cleanup_after_failed_drive_leaves_nothing_running(tmp_path, capsys):
    handle = verify.ROOT / "data/ui-testing/instance.json"
    if handle.exists():
        pytest.skip("another UI instance owns this checkout's handle")
    port = free_port()
    started = []

    def failing_drive(run):
        # A stand-in browser the drive step owns, recorded like the real one.
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        started.append(child)
        run.state["drive"].append(verify.identity(child.pid))
        raise verify.StepFailed("deliberate drive failure")

    argv = ["--port", str(port), "--out", str(tmp_path)]
    assert verify.main(argv, drive=failing_drive) == 1
    capsys.readouterr()
    (evidence,) = tmp_path.iterdir()
    summary = (evidence / "summary.md").read_text(encoding="utf-8")
    assert "| launch | pass |" in summary and "| doctor | pass |" in summary
    assert "| drive | FAIL | deliberate drive failure |" in summary
    assert "| cleanup | pass |" in summary
    state = json.loads((evidence / "launch.json").read_text(encoding="utf-8"))
    assert "token" not in json.dumps(state)
    for owned in state["launcher"] + state["drive"]:
        assert not verify.alive(owned)
    assert started[0].wait(timeout=5) is not None
    assert not doctor.listening_addresses(port)
    assert not handle.exists()
    assert not (verify.ROOT / state["instance_dir"]).exists()


@pytest.mark.parametrize(
    ("ui_port", "argv", "url"),
    [
        (None, [], "http://127.0.0.1:4173"),
        ("4180", [], "http://127.0.0.1:4180"),
        ("not-a-port", ["--url", "http://127.0.0.1:4190"], "http://127.0.0.1:4190"),
    ],
    ids=["default", "variable", "explicit-url-wins"],
)
def test_doctor_default_url_follows_ui_port(monkeypatch, ui_port, argv, url):
    if ui_port is None:
        monkeypatch.delenv("UI_PORT", raising=False)
    else:
        monkeypatch.setenv("UI_PORT", ui_port)
    checked = []
    monkeypatch.setattr(
        doctor, "run_checks", lambda base, _: checked.append(base) or []
    )
    assert doctor.main([*argv, "--instance", "run-0123456789abcdef"]) == 0
    assert checked == [url]


@pytest.mark.parametrize("module", [doctor, verify], ids=["doctor", "verify"])
@pytest.mark.parametrize("raw", ["abc", "80", "65536"])
def test_verify_skill_refuses_bad_ui_port(monkeypatch, capsys, tmp_path, module, raw):
    monkeypatch.setenv("UI_PORT", raw)
    argv = ["--out", str(tmp_path)] if module is verify else []
    with pytest.raises(SystemExit) as exit_info:
        module.main(argv)
    assert exit_info.value.code == 2
    assert "UI_PORT must be a whole number" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())  # no evidence folder, nothing launched


@pytest.mark.parametrize(
    ("ui_port", "argv", "port"),
    [(None, [], 4173), ("4180", [], 4180), ("not-a-port", ["--port", "4190"], 4190)],
    ids=["default", "variable", "explicit-port-wins"],
)
def test_verify_default_port_follows_ui_port(
    monkeypatch, capsys, tmp_path, ui_port, argv, port
):
    if ui_port is None:
        monkeypatch.delenv("UI_PORT", raising=False)
    else:
        monkeypatch.setenv("UI_PORT", ui_port)
    ports = []

    class Recorded:
        def __init__(self, evidence, port, feature):
            self.evidence = evidence
            ports.append(port)

        def execute(self, drive):
            (self.evidence / "summary.md").write_text("ok", encoding="utf-8")
            return True

    monkeypatch.setattr(verify, "Run", Recorded)
    assert verify.main([*argv, "--out", str(tmp_path)]) == 0
    capsys.readouterr()
    assert ports == [port]


@pytest.mark.parametrize("raw", ["0", "80", "70000", "abc"])
def test_verify_refuses_a_bad_port_flag_before_any_run(
    monkeypatch, capsys, tmp_path, raw
):
    # --port 0 used to start a real run; the flag takes the UI_PORT rule (#154).
    monkeypatch.setattr(verify, "Run", lambda *args: pytest.fail("a run started"))
    with pytest.raises(SystemExit) as exit_info:
        verify.main(["--port", raw, "--out", str(tmp_path)])
    assert exit_info.value.code == 2
    assert (
        "argument --port: must be a whole number from 1024 to 65535"
        in capsys.readouterr().err
    )
    assert not list(tmp_path.iterdir())  # no evidence folder was made


@pytest.mark.parametrize(
    ("env", "url"),
    [
        ({}, "http://127.0.0.1:4173"),
        ({"UI_PORT": "4180"}, "http://127.0.0.1:4180"),
        (
            {"UI_PORT": "not-a-port", "VERIFY_BASE_URL": "http://127.0.0.1:4190"},
            "http://127.0.0.1:4190",
        ),
    ],
    ids=["default", "variable", "verify-base-url-wins"],
)
def test_drive_config_base_url_follows_ui_port(env, url):
    result = load_playwright_config(
        ".claude/skills/verify/scripts/drive.config.mjs", **env
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["use"]["baseURL"] == url


def test_drive_config_refuses_bad_ui_port():
    result = load_playwright_config(
        ".claude/skills/verify/scripts/drive.config.mjs", UI_PORT="80"
    )
    assert result.returncode != 0
    assert "UI_PORT must be a whole number" in result.stderr


@pytest.fixture
def foreign_instance():
    """A real launcher in this checkout that verify did not start (#148)."""
    handle = verify.ROOT / "data/ui-testing/instance.json"
    if handle.exists():
        pytest.skip("another UI instance owns this checkout's handle")
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("MUSICDISCOVERY_", "FMA_"))
        and name not in {"DEMO_BUNDLE", "DATABASE_URL", "API_KEY", "OPENAI_API_KEY"}
    }
    launcher = subprocess.Popen(
        [
            sys.executable,
            "scripts/ui_instance.py",
            "start",
            "--port",
            str(free_port()),
        ],
        cwd=verify.ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    url = None
    for line in launcher.stdout:
        if line.startswith("UI_URL="):
            url = line.strip().removeprefix("UI_URL=")
            break
    assert url, "the foreign launcher did not become ready"
    instance = json.loads(handle.read_text(encoding="utf-8"))["instance"]
    yield url, instance
    if launcher.poll() is None:
        # The authenticated stop removes the handle and bundle on every OS.
        subprocess.run(
            [sys.executable, "scripts/ui_instance.py", "stop"],
            cwd=verify.ROOT,
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
    if launcher.poll() is None:
        launcher.kill()
    launcher.wait(timeout=15)
    launcher.stdout.close()


def health(url):
    with urlopen(url + "__ui_health", timeout=5) as response:
        return json.load(response)


# Needs node on PATH, which every CI job has; without it verify cannot launch.
@pytest.mark.parametrize("same_port", [False, True], ids=["other-port", "same-port"])
def test_verify_leaves_an_instance_it_did_not_start_running(
    foreign_instance, tmp_path, capsys, same_port
):
    url, instance = foreign_instance
    port = int(url.rsplit(":", 1)[1].strip("/")) if same_port else free_port()

    def must_not_drive(run):
        raise AssertionError("verify drove an instance it did not launch")

    argv = ["--port", str(port), "--out", str(tmp_path)]
    assert verify.main(argv, drive=must_not_drive) == 1
    printed = capsys.readouterr().out
    # Verify neither stopped the other instance nor removed its handle or bundle.
    assert health(url) == {"instance": instance}
    handle = verify.ROOT / "data/ui-testing/instance.json"
    assert json.loads(handle.read_text(encoding="utf-8"))["instance"] == instance
    assert (verify.ROOT / "data/ui-testing" / instance).is_dir()
    assert f"| launch | FAIL | another instance ({instance}) owns" in printed
    assert "| cleanup | pass |" in printed
    (evidence,) = tmp_path.iterdir()
    cleanup = (evidence / "cleanup.txt").read_text(encoding="utf-8")
    assert "ui:stop" not in cleanup and "ui:recover" not in cleanup
    assert f"belongs to {instance}" in cleanup


@pytest.mark.parametrize(
    ("saved", "owner"),
    [
        (None, None),
        ({"instance": "run-ffffffffffffffff", "pid": 1, "created": 1.0}, False),
        ({"instance": "run-0123456789abcdef", "pid": 1, "created": 1.0}, True),
        # A launcher that crashed before its readiness line wrote this handle.
        ({"instance": "run-ffffffffffffffff", "pid": "launcher"}, True),
        ("{not json", False),
    ],
    ids=["none", "other-instance", "launched", "crashed-before-ready", "unreadable"],
)
def test_handle_owner_counts_only_this_runs_launcher(tmp_path, saved, owner):
    run = verify.Run(tmp_path, free_port(), None)
    run.handle = tmp_path / "instance.json"
    run.state["instance"] = "run-0123456789abcdef"
    launcher = verify.identity(os.getpid())
    run.state["launcher"].append(launcher)
    if isinstance(saved, dict) and saved["pid"] == "launcher":
        saved = {**saved, **launcher}
    if saved is not None:
        text = saved if isinstance(saved, str) else json.dumps(saved)
        run.handle.write_text(text, encoding="utf-8")
    found = run.handle_owner()
    assert (found if found is None else found[1]) is owner
    handle = by_name(run.leftovers())["handle"]
    assert handle.ok is (owner is not True)


STAND_IN_WRAPPER = """
import json, subprocess, sys, time
from pathlib import Path

import psutil

HANDLE = Path("data/ui-testing/instance.json")
command = sys.argv[1]
if command == "start":
    # Like node's wrapper: the Python launcher runs as a child.
    sys.exit(subprocess.Popen([sys.executable, __file__, "launcher"]).wait())
if command == "launcher":
    # The launcher writes its handle before it prints the readiness line, then
    # crashes or hangs once verify has recorded it (or 5 seconds have passed).
    me = psutil.Process()
    identity = {"pid": me.pid, "created": me.create_time()}
    Path("launcher.json").write_text(json.dumps(identity), encoding="utf-8")
    HANDLE.parent.mkdir(parents=True)
    HANDLE.write_text(json.dumps({"instance": INSTANCE, **identity}), "utf-8")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = json.loads(Path(LAUNCH).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {"launcher": []}
        if identity in state["launcher"]:
            break
        time.sleep(0.05)
    if MODE == "timed-out":
        time.sleep(120)
    sys.exit(3)
if command == "stop":
    sys.exit("stop could not reach the instance")
if command == "recover":
    HANDLE.unlink()
    print("Recovered " + INSTANCE)
"""


@pytest.mark.parametrize("mode", ["crashed", "timed-out"])
def test_launch_owns_a_handle_its_launcher_wrote_before_readiness(
    tmp_path, monkeypatch, mode
):
    """Launch records the wrapper's descendants while it waits for readiness."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    instance = "run-0123456789abcdef"
    # verify.py runs `<node> scripts/ui-command.mjs ...` in the checkout; this
    # stand-in for node runs the file as Python.
    (checkout / "scripts/ui-command.mjs").write_text(
        f"MODE = {mode!r}\nINSTANCE = {instance!r}\n"
        f"LAUNCH = {str(evidence / 'launch.json')!r}\n{STAND_IN_WRAPPER}",
        encoding="utf-8",
    )
    monkeypatch.setattr(verify, "ROOT", checkout)
    monkeypatch.setattr(verify, "LAUNCH_TIMEOUT", 10)
    monkeypatch.setattr(verify, "EXIT_TIMEOUT", 1)
    run = verify.Run(evidence, free_port(), None)
    run.node = sys.executable

    def must_not_drive(run):
        raise AssertionError("verify drove an instance that never became ready")

    try:
        assert not run.execute(drive=must_not_drive)
    finally:
        launcher = json.loads((checkout / "launcher.json").read_text("utf-8"))
        if verify.alive(launcher):
            psutil.Process(launcher["pid"]).kill()
    steps = {name: (ok, detail) for name, ok, detail in run.steps}
    failed = "launcher exited with" if mode == "crashed" else "no readiness line"
    assert steps["launch"][0] is False and steps["launch"][1].startswith(failed)
    assert "another instance" not in steps["launch"][1]
    assert steps["cleanup"] == (
        True,
        "no recorded process, listener, handle or instance directory remains",
    )
    # The launcher that wrote the handle was recorded, stopped and recovered.
    assert launcher in run.state["launcher"]
    cleanup = (evidence / "cleanup.txt").read_text(encoding="utf-8")
    assert f"$ npm run ui:recover\nRecovered {instance}" in cleanup
    assert "did not launch" not in cleanup
    assert not run.handle.exists()
