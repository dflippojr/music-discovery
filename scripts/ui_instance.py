"""Serve a fresh synthetic UI bundle on loopback; never reuse another server."""

import argparse
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import psutil

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "ui-testing"
HANDLE = DATA / "instance.json"
DEFAULT_PORT = 4173
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


def ui_port(environ=os.environ):
    """The port in UI_PORT, or 4173 when it is unset or empty.

    scripts/ui-port.mjs applies the same rule for the Playwright configs.
    """
    raw = environ.get("UI_PORT", "")
    if raw == "":
        return DEFAULT_PORT
    try:
        return port_number(raw)
    except ValueError as error:
        raise ValueError(f"UI_PORT {error}") from error


def port_number(raw):
    """The whole number in raw from 1024 to 65535; the ValueError states the rule."""
    # Drop leading zeros and cap the length first: int() refuses very long strings
    # with its own error, which would not state the rule.
    digits = raw.lstrip("0")
    if (
        re.fullmatch(r"[0-9]+", raw) is None
        or len(digits) > 5
        or not 1024 <= int(digits or "0") <= 65535
    ):
        raise ValueError(
            "must be a whole number from 1024 to 65535, "
            f"got {json.dumps(raw, ensure_ascii=False)}"
        )
    return int(digits)


def port_argument(text):
    """argparse type for --port: the rule UI_PORT follows, so --port 0 is refused."""
    try:
        return port_number(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


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
        entries = list(bundle.iterdir())
        for path in entries:
            if path.is_symlink() or not path.is_file():
                raise ValueError("Unexpected entry in disposable bundle")
        for path in entries:
            path.unlink()
        bundle.rmdir()


def process_identity(pid):
    return {"pid": pid, "created": psutil.Process(pid).create_time()}


def validate_identity(identity):
    if (
        not isinstance(identity, dict)
        or type(identity.get("pid")) is not int
        or not 0 < identity["pid"] <= 2**31 - 1
        or type(identity.get("created")) not in (int, float)
        or not 0 < identity["created"] < 1e11
        or not math.isfinite(identity["created"])
    ):
        raise ValueError("Invalid process identity; preserve handle for investigation")


def read_handle():
    if DATA.is_symlink() or HANDLE.is_symlink():
        raise ValueError("Symlinked instance storage; preserve for investigation")
    raw = HANDLE.read_bytes()
    metadata = json.loads(raw)
    if (
        not isinstance(metadata, dict)
        or not isinstance(metadata.get("instance"), str)
        or re.fullmatch(r"run-[0-9a-f]{16}", metadata["instance"]) is None
        or type(metadata.get("pid")) is not int
        or not 0 < metadata["pid"] <= 2**31 - 1
        or type(metadata.get("port")) is not int
        or not 1 <= metadata["port"] <= 65535
        or not isinstance(metadata.get("token"), str)
        or re.fullmatch(r"[A-Za-z0-9_-]{20,128}", metadata["token"]) is None
    ):
        raise ValueError("Invalid instance handle; preserve for investigation")
    # Old launchers can still be stopped through their authenticated endpoint;
    # only recovery needs creation time as proof against PID reuse.
    if "created" in metadata:
        validate_identity(metadata)
    return metadata, raw


def require_exited(identity):
    validate_identity(identity)
    try:
        created = psutil.Process(identity["pid"]).create_time()
    except psutil.NoSuchProcess:
        return
    except psutil.Error as error:
        raise ValueError(
            "Cannot establish process identity; investigate handle"
        ) from error
    if created != identity["created"]:
        raise ValueError("PID reused by a different process; investigate handle")
    raise ValueError("Recorded process is still live; preserve handle and bundle")


def recover():
    """Remove stale files only with proof of exit and an exclusively free port."""
    if not HANDLE.exists() and not HANDLE.is_symlink():
        print("No owned UI instance remains")
        return
    metadata, raw = read_handle()
    require_exited(metadata)
    if metadata.get("phase") == "seeding":
        require_exited(metadata.get("fixture"))
    elif metadata.get("phase") != "ready":
        raise ValueError("Fixture ownership unknown; preserve handle for investigation")
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", metadata["port"]))
            probe.listen()
        except OSError as error:
            raise ValueError(
                "Port is occupied or unverifiable; "
                "refusing recovery of another instance"
            ) from error
        if HANDLE.is_symlink() or HANDLE.read_bytes() != raw:
            raise ValueError("Instance handle changed; preserve for investigation")
        remove_bundle(DATA / metadata["instance"])
        HANDLE.unlink()
    print(f"Recovered {metadata['instance']}; handle and synthetic bundle removed")


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """Refuse a port another listener holds, even one that set SO_REUSEADDR.

    On Windows, SO_REUSEADDR lets a second socket bind a listening port, so the
    stock server would report ready while requests reach the first instance.
    POSIX keeps the reuse flag so a restart after TIME_WAIT still binds.
    """

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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
        raise ValueError(
            "An instance handle exists; use stop or verified recover first"
        )
    instance = "run-" + secrets.token_hex(8)
    bundle = DATA / instance
    identity = process_identity(os.getpid())
    # Bind first: an occupied port fails before creating any synthetic data.
    server = ExclusiveHTTPServer(
        ("127.0.0.1", port), partial(Handler, directory=str(bundle))
    )
    server.instance = instance
    server.token = secrets.token_urlsafe(32)
    metadata = {
        "instance": instance,
        **identity,
        "port": server.server_port,
        "token": server.token,
        "phase": "starting",
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
            [
                sys.executable,
                str(ROOT / "scripts/build_demo_fixture.py"),
                "--no-shape",  # keep the bundle flat so remove_bundle can delete it
                str(bundle),
            ],
            cwd=ROOT,
        )
        try:
            fixture = process_identity(child.pid)
        except psutil.NoSuchProcess:
            if child.poll() is None:
                raise
        else:
            metadata.update(phase="seeding", fixture=fixture)
            HANDLE.write_text(json.dumps(metadata), encoding="utf-8")
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
        metadata["phase"] = "ready"
        HANDLE.write_text(json.dumps(metadata), encoding="utf-8")
        # The socket is bound and every file exists before announcing readiness.
        print(f"UI_URL=http://127.0.0.1:{server.server_port}/", flush=True)
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
    if not HANDLE.exists() and not HANDLE.is_symlink():
        print("No owned UI instance remains")
        return
    metadata, _ = read_handle()
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
    parser.add_argument("action", choices=("start", "stop", "recover"))
    parser.add_argument(
        "--port", type=port_argument, help="default: UI_PORT, else 4173"
    )
    args = parser.parse_args()
    try:
        if args.action == "start":
            start(ui_port() if args.port is None else args.port)
        elif args.action == "stop":
            stop()
        else:
            recover()
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        URLError,
        psutil.Error,
    ) as error:
        print(f"UI instance failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
