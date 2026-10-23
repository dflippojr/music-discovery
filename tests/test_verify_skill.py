"""Guard the verify skill's doctor checks and its cleanup after a failed drive."""

import importlib.util
import json
import shutil
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

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


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")


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


@needs_node
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


@needs_node
def test_drive_config_refuses_bad_ui_port():
    result = load_playwright_config(
        ".claude/skills/verify/scripts/drive.config.mjs", UI_PORT="80"
    )
    assert result.returncode != 0
    assert "UI_PORT must be a whole number" in result.stderr
