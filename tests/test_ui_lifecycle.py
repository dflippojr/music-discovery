"""Run the real launcher twice on real sockets, as two worktrees would (#87).

Each launcher is a copy in its own tree, so it has its own data/ui-testing and
only the port is shared. Nothing is mocked: the launcher builds the synthetic
fixture, serves it, and stops through its authenticated endpoint.
"""

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.request import urlopen

import pytest

ROOT = Path(__file__).resolve().parents[1]
FILES = ["scripts/ui_instance.py", "scripts/build_demo_fixture.py"]
FILES += ["tests/test_evaluation.py"]  # build_demo_fixture imports make_frame
STARTUP_TIMEOUT = 90
EXIT_TIMEOUT = 15


def checkout(base, name):
    tree = base / name
    for file in FILES:
        (tree / file).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / file, tree / file)
    return tree


def clean_environment():
    # The launcher refuses external configuration; a developer shell may have it.
    forbidden = {"DEMO_BUNDLE", "DATABASE_URL", "API_KEY", "OPENAI_API_KEY"}
    return {
        name: value
        for name, value in os.environ.items()
        if name not in forbidden and not name.startswith(("MUSICDISCOVERY_", "FMA_"))
    }


def test_flat_fixture_builds_without_shape_helpers(tmp_path):
    tree = checkout(tmp_path, "flat")
    bundle = tree / "data" / "bundle"
    result = subprocess.run(
        [sys.executable, "scripts/build_demo_fixture.py", "--no-shape", str(bundle)],
        cwd=tree,
        env=clean_environment(),
        capture_output=True,
        text=True,
        timeout=STARTUP_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads((bundle / "catalog.json").read_text())["count"] == 450
    cases = json.loads((bundle / "exclusion.json").read_text())
    assert set(cases) == {"repeated", "one_artist"}
    assert not (bundle / "shape").exists()


class Launcher:
    """One `ui_instance.py start` process and the lines it prints."""

    def __init__(self, tree, port, script="scripts/ui_instance.py"):
        self.tree = tree
        self.handle = tree / "data" / "ui-testing" / "instance.json"
        self.errors = tree / "launcher.err"
        with self.errors.open("w", encoding="utf-8") as errors:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    script,
                    "start",
                    "--port",
                    str(port),
                ],
                cwd=tree,
                env=clean_environment(),
                stdout=subprocess.PIPE,
                stderr=errors,
                text=True,
                encoding="utf-8",
            )
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.process.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def wait_ready(self):
        """Return the announced URL, or None if the launcher exited first."""
        while True:
            line = self.lines.get(timeout=STARTUP_TIMEOUT)
            if line is None:
                self.process.wait(timeout=EXIT_TIMEOUT)
                return None
            if line.startswith("UI_URL="):
                return line.removeprefix("UI_URL=")

    def stderr(self):
        return self.errors.read_text(encoding="utf-8")

    def health(self, url):
        with urlopen(url + "__ui_health", timeout=5) as response:
            return json.load(response)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=EXIT_TIMEOUT)
        self.process.stdout.close()


@pytest.fixture
def launchers(tmp_path):
    started = []

    def launch(name, port, prepare=None, **options):
        tree = checkout(tmp_path, name)
        if prepare is not None:
            prepare(tree)
        launcher = Launcher(tree, port, **options)
        started.append(launcher)
        return launcher

    yield launch
    for launcher in started:
        launcher.close()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# Runs the real launcher but raises SIGTERM inside its second ready line, which
# fixes a timing that a signal from outside hits too rarely to test (#213).
SIGNAL_IN_READY_LINE = """\
import builtins, importlib.util, signal, sys
spec = importlib.util.spec_from_file_location("ui_instance", "scripts/ui_instance.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def signalling_print(*args, **kwargs):
    builtins.print(*args, **kwargs)
    if args and str(args[0]).startswith("UI_INSTANCE="):
        signal.raise_signal(signal.SIGTERM)  # os.kill would end a Windows process


launcher.print = signalling_print
sys.exit(launcher.main())
"""


def test_sigterm_between_ready_lines_and_serving_exits_and_cleans_up(launchers):
    port = free_port()

    def prepare(tree):
        (tree / "signal_in_ready_line.py").write_text(
            SIGNAL_IN_READY_LINE, encoding="utf-8"
        )

    launcher = launchers(
        "signalled", port, prepare=prepare, script="signal_in_ready_line.py"
    )
    assert launcher.wait_ready() == f"http://127.0.0.1:{port}/", launcher.stderr()
    instance = launcher.lines.get(timeout=STARTUP_TIMEOUT)
    assert instance.startswith("UI_INSTANCE="), launcher.stderr()

    # Before #213's fix this timed out with /__ui_health still answering.
    assert launcher.process.wait(timeout=EXIT_TIMEOUT) == 0, launcher.stderr()
    assert not launcher.handle.exists()
    assert not list((launcher.tree / "data" / "ui-testing").glob("run-*"))
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()


def test_second_launcher_refuses_a_served_port_and_stop_frees_it(launchers):
    port = free_port()  # --port must name a port from 1024 to 65535 (#154)
    first = launchers("first", port)
    url = first.wait_ready()
    assert url == f"http://127.0.0.1:{port}/", first.stderr()
    instance = json.loads(first.handle.read_text(encoding="utf-8"))["instance"]
    assert first.health(url) == {"instance": instance}

    # Before #87's fix, Windows let this bind the same port and report ready.
    second = launchers("second", port)
    assert second.wait_ready() is None, "second launcher took a served port"
    assert second.process.returncode == 1
    assert "UI instance failed" in second.stderr()
    assert not second.handle.exists()
    assert not list((second.tree / "data" / "ui-testing").glob("run-*"))
    assert first.health(url) == {"instance": instance}

    stopped = subprocess.run(
        [sys.executable, "scripts/ui_instance.py", "stop"],
        cwd=first.tree,
        env=clean_environment(),
        capture_output=True,
        text=True,
        timeout=EXIT_TIMEOUT + 10,
        check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    assert f"Stopped {instance}" in stopped.stdout
    assert first.process.wait(timeout=EXIT_TIMEOUT) == 0
    assert not first.handle.exists()
    assert not (first.tree / "data" / "ui-testing" / instance).exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=5).close()
