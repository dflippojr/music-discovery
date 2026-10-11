"""Serve a fresh synthetic UI bundle on loopback; never reuse another server."""

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ui-testing"
HANDLE = DATA / "instance.json"
FIXTURE_TIMEOUT = 30
CHILD_EXIT_TIMEOUT = 2
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "form-action 'none'; object-src 'none'"
)


def validate_environment():
    forbidden = {"DEMO_BUNDLE", "DATABASE_URL", "API_KEY", "OPENAI_API_KEY"}
    names = [
        name
        for name, value in os.environ.items()
        if value and (name in forbidden or name.startswith(("MUSICDISCOVERY_", "FMA_")))
    ]
    if names:
        raise ValueError(
            "Remove external configuration before starting: " + ", ".join(names)
        )


def remove_bundle(bundle):
    # The exporter creates a flat directory. Refuse directories/symlinks rather
    # than recursively removing an unexpected path or someone else's data.
    if (
        bundle.is_symlink()
        or bundle.resolve().parent != DATA.resolve()
        or not bundle.name.startswith("run-")
    ):
        raise ValueError("Disposable bundle is outside data/ui-testing")
    if bundle.exists():
        for path in bundle.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("Unexpected entry in disposable bundle")
            path.unlink()
        bundle.rmdir()


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path == "/__ui_health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"instance": self.server.instance}).encode())
        elif self.path.startswith(("/synthetic/track/", "/synthetic/license")):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><title>Synthetic link</title>"
                b"<h1>Synthetic local reference</h1>"
                b"<p>No audio or external service.</p>"
            )
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path != "/__ui_stop" or not secrets.compare_digest(
            self.headers.get("X-UI-Token", ""), self.server.token
        ):
            self.send_error(403)
            return
        self.send_response(204)
        self.end_headers()
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def terminate_fixture(child):
    """Reap only our fixture child before its files can be removed."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=CHILD_EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=CHILD_EXIT_TIMEOUT)


def wait_for_fixture(child, stopping):
    deadline = time.monotonic() + FIXTURE_TIMEOUT
    while not stopping.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Fixture generation timed out after {FIXTURE_TIMEOUT} seconds"
            )
        try:
            result = child.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        if stopping.is_set():
            return False
        if result:
            raise subprocess.CalledProcessError(result, child.args)
        return True
    return False


def start(port):
    validate_environment()
    DATA.mkdir(parents=True, exist_ok=True)
    if HANDLE.exists():
        raise ValueError("An instance handle exists; stop that owned instance first")
    instance = "run-" + secrets.token_hex(8)
    bundle = DATA / instance
    # Bind first: an occupied port fails before creating any synthetic data.
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(Handler, directory=str(bundle))
    )
    server.instance = instance
    server.token = secrets.token_urlsafe(32)
    metadata = {
        "instance": instance,
        "pid": os.getpid(),
        "port": port,
        "token": server.token,
    }
    stopping = threading.Event()
    serving = threading.Event()

    def request_shutdown(*_):
        stopping.set()
        if serving.is_set():
            threading.Thread(target=server.shutdown, daemon=True).start()

    previous_term = signal.signal(signal.SIGTERM, request_shutdown)
    previous_int = signal.signal(signal.SIGINT, request_shutdown)
    child = None
    try:
        with HANDLE.open("x", encoding="utf-8") as handle:
            json.dump(metadata, handle)
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts/build_demo_fixture.py"), str(bundle)],
            cwd=ROOT,
        )
        if not wait_for_fixture(child, stopping):
            print("UI startup cancelled during fixture generation", file=sys.stderr)
            return
        catalog_path = bundle / "catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["licenses"] = ["/synthetic/license"] * len(catalog["licenses"])
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
        source_path = bundle / "sources.json"
        sources = json.loads(source_path.read_text(encoding="utf-8"))
        sources["sources"] = [f"/synthetic/track/{track}" for track in catalog["ids"]]
        source_path.write_text(json.dumps(sources), encoding="utf-8")
        if stopping.is_set():
            print("UI startup cancelled", file=sys.stderr)
            return
        # The socket is bound and every file exists before announcing readiness.
        print(f"UI_URL=http://127.0.0.1:{port}/", flush=True)
        print(
            f"UI_INSTANCE={instance} PID={os.getpid()} "
            "HANDLE=data/ui-testing/instance.json",
            flush=True,
        )
        serving.set()
        server.serve_forever(poll_interval=0.1)
    finally:
        try:
            if child is not None:
                terminate_fixture(child)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
        server.server_close()
        remove_bundle(bundle)
        if HANDLE.exists():
            saved = json.loads(HANDLE.read_text(encoding="utf-8"))
            if saved["instance"] == instance:
                HANDLE.unlink()


def stop():
    if not HANDLE.exists():
        print("No owned UI instance remains")
        return
    metadata = json.loads(HANDLE.read_text(encoding="utf-8"))
    origin = f"http://127.0.0.1:{int(metadata['port'])}"
    with urlopen(origin + "/__ui_health", timeout=5) as response:
        if json.load(response)["instance"] != metadata["instance"]:
            raise ValueError("Port belongs to a different instance; refusing teardown")
    request = Request(
        origin + "/__ui_stop", method="POST", headers={"X-UI-Token": metadata["token"]}
    )
    with urlopen(request, timeout=5):
        pass
    deadline = time.monotonic() + 15
    while HANDLE.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if HANDLE.exists() or (DATA / metadata["instance"]).exists():
        raise ValueError("Teardown incomplete; preserve the handle for investigation")
    print(f"Stopped {metadata['instance']}; handle and synthetic bundle removed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--port", type=int, default=4173)
    args = parser.parse_args()
    try:
        if args.action == "start":
            start(args.port)
        else:
            stop()
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        URLError,
    ) as error:
        print(f"UI instance failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
