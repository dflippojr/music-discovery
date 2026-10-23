"""Check that a disposable UI instance is ready, loopback-only and synthetic.

Usage: python .claude/skills/verify/scripts/doctor.py [--url URL] [--instance ID]

The default URL is http://127.0.0.1:$UI_PORT, with UI_PORT defaulting to 4173.

Prints PASS or FAIL per check and exits non-zero when any check fails.
"""

import argparse
import importlib.util
import ipaddress
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

import psutil

ROOT = Path(__file__).resolve().parents[4]
HANDLE = ROOT / "data" / "ui-testing" / "instance.json"
SEED_TITLE = "Synthetic track 100"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def __str__(self):
        return f"{'PASS' if self.ok else 'FAIL'} {self.name}: {self.detail}"


def load_launcher():
    """The launcher owns the forbidden-configuration list; reuse it."""
    path = ROOT / "scripts" / "ui_instance.py"
    spec = importlib.util.spec_from_file_location("ui_instance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fetch_json(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def listening_addresses(port):
    return [
        conn.laddr.ip
        for conn in psutil.net_connections(kind="inet")
        if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port
    ]


def check_ready(base, expected):
    try:
        instance = fetch_json(base + "/__ui_health").get("instance")
    except (OSError, ValueError) as error:
        return Check("ready", False, f"GET /__ui_health failed: {error}")
    if expected and instance != expected:
        return Check("ready", False, f"health reports {instance}, expected {expected}")
    return Check("ready", True, f"/__ui_health reports {instance}")


def check_loopback(base, listeners):
    parts = urlsplit(base)
    if parts.hostname != "127.0.0.1":
        return Check("loopback", False, f"{base} is not http://127.0.0.1")
    port = parts.port or 80
    try:
        addresses = listeners(port)
    except psutil.Error as error:
        return Check("loopback", False, f"cannot list listeners: {error}")
    if not addresses:
        return Check("loopback", False, f"nothing listens on port {port}")
    exposed = [a for a in addresses if not ipaddress.ip_address(a).is_loopback]
    if exposed:
        return Check("loopback", False, f"port {port} also listens on {exposed}")
    return Check("loopback", True, f"port {port} listens only on {sorted(addresses)}")


def check_seed(base):
    try:
        catalog = fetch_json(base + "/catalog.json")
        sources = fetch_json(base + "/sources.json")["sources"]
    except (OSError, ValueError, KeyError) as error:
        return Check("seed", False, f"synthetic catalog unavailable: {error}")
    titles = catalog.get("titles", [])
    if SEED_TITLE not in titles or "artist0" not in catalog.get("artists", []):
        return Check("seed", False, f"{SEED_TITLE} by artist0 is missing")
    if not all(title.startswith("Synthetic track ") for title in titles):
        return Check("seed", False, "catalog has non-synthetic titles")
    links = list(catalog.get("licenses", [])) + list(sources)
    if not all(link.startswith("/synthetic/") for link in links):
        return Check("seed", False, "source or license links leave the instance")
    return Check("seed", True, f"{len(titles)} synthetic tracks with local links")


def check_environment(launcher):
    try:
        launcher.validate_environment()
    except ValueError as error:
        return Check("environment", False, str(error))
    return Check("environment", True, "no external data or credential configuration")


def run_checks(base, expected=None, listeners=listening_addresses, launcher=None):
    base = base.rstrip("/")
    return [
        check_ready(base, expected),
        check_loopback(base, listeners),
        check_seed(base),
        check_environment(launcher or load_launcher()),
    ]


def handle_instance():
    try:
        return json.loads(HANDLE.read_text(encoding="utf-8")).get("instance")
    except (OSError, ValueError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", help="default: http://127.0.0.1:$UI_PORT")
    parser.add_argument("--instance", help="expected UI_INSTANCE (default: handle)")
    args = parser.parse_args(argv)
    if args.url is None:
        try:
            args.url = f"http://127.0.0.1:{load_launcher().ui_port()}"
        except ValueError as error:
            parser.error(str(error))
    checks = run_checks(args.url, args.instance or handle_instance())
    for check in checks:
        print(check)
    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
