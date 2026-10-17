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


class Launcher:
    """One `ui_instance.py start` process and the lines it prints."""

    def __init__(self, tree, port):
        self.tree = tree
        self.handle = tree / "data" / "ui-testing" / "instance.json"
        self.errors = tree / "launcher.err"
        with self.errors.open("w", encoding="utf-8") as errors:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "scripts/ui_instance.py",
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

    def launch(name, port):
        launcher = Launcher(checkout(tmp_path, name), port)
        started.append(launcher)
        return launcher

    yield launch
    for launcher in started:
        launcher.close()


def test_second_launcher_refuses_a_served_port_and_stop_frees_it(launchers):
    first = launchers("first", 0)
    url = first.wait_ready()
    assert url is not None, first.stderr()
    instance = json.loads(first.handle.read_text(encoding="utf-8"))["instance"]
    assert first.health(url) == {"instance": instance}
    port = int(url.rsplit(":", 1)[1].strip("/"))

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
