"""Launch, check, drive and clean up a disposable UI instance, saving evidence.

Usage: python .claude/skills/verify/scripts/verify.py [--feature NAME] [--port N]

Follows docs/ui-testing.md: the same launcher as `npm run ui:start`, the same
teardown as `npm run ui:stop`, and `npm run ui:recover` only after the owned
launcher has exited. Evidence goes to .verify/<UTC timestamp>/ (git-ignored).
Cleanup always runs and exits non-zero if anything it started is left behind.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parent))

import doctor  # noqa: E402

ROOT = doctor.ROOT
SKILL = Path(__file__).resolve().parent
FEATURES = ("seed", "qualities", "rankers", "ratings")
READY_LINE = re.compile(r"UI_INSTANCE=(run-[0-9a-f]{16}) PID=(\d+)")
LAUNCH_TIMEOUT = 60
DRIVE_TIMEOUT = 600
EXIT_TIMEOUT = 15


class StepFailed(Exception):
    pass


def identity(pid):
    return {"pid": pid, "created": psutil.Process(pid).create_time()}


def alive(owned):
    """True only for the recorded process, never a reused PID."""
    try:
        process = psutil.Process(owned["pid"])
        return (
            process.create_time() == owned["created"]
            and process.status() != psutil.STATUS_ZOMBIE
        )
    except psutil.NoSuchProcess:
        return False


def wait_gone(owned, timeout):
    deadline = time.monotonic() + timeout
    while any(alive(o) for o in owned) and time.monotonic() < deadline:
        time.sleep(0.2)
    return [o for o in owned if alive(o)]


def owned_by(instance):
    return (
        f"another instance ({instance}) owns data/ui-testing/instance.json; "
        "verify neither attaches to it nor stops it"
    )


def stop_recorded(owned, notes):
    """Terminate, then kill, only recorded processes that are still alive."""
    for signal_name in ("terminate", "kill"):
        survivors = [o for o in owned if alive(o)]
        for o in survivors:
            notes.append(f"{signal_name} recorded PID {o['pid']}")
            try:
                getattr(psutil.Process(o["pid"]), signal_name)()
            except psutil.NoSuchProcess:
                pass
        if not wait_gone(survivors, 5):
            return


class Run:
    def __init__(self, evidence, port, feature):
        self.evidence = evidence
        self.port = port
        self.feature = feature
        self.url = f"http://127.0.0.1:{port}"
        self.handle = ROOT / "data" / "ui-testing" / "instance.json"
        self.state = {
            "url": self.url + "/",
            "port": port,
            "feature": feature or "all",
            "log": str(evidence / "launcher.log"),
            "launcher": [],
            "drive": [],
        }
        self.steps = []
        self.node = shutil.which("node")

    def save(self):
        (self.evidence / "launch.json").write_text(
            json.dumps(self.state, indent=2), encoding="utf-8"
        )

    def command(self, *args, name):
        result = subprocess.run(
            [self.node, "scripts/ui-command.mjs", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode, f"$ npm run ui:{name}\n{output}"

    def handle_owner(self):
        """None without a handle, else (its instance, whether this run wrote it).

        The handle is shared by every instance in the checkout, so it is this
        run's only when it names the instance this run launched, or the process
        that wrote it is one this run recorded under its launcher wrapper.
        """
        if not (self.handle.exists() or self.handle.is_symlink()):
            return None
        try:
            saved = json.loads(self.handle.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = None
        if not isinstance(saved, dict):
            return "an unreadable handle", False
        instance = self.state.get("instance")
        ours = (instance is not None and saved.get("instance") == instance) or any(
            saved.get("pid") == o["pid"] and saved.get("created") == o["created"]
            for o in self.state["launcher"]
        )
        return str(saved.get("instance")), ours

    def record_launcher(self, process):
        """Record processes under the owned wrapper while it runs."""
        recorded = {o["pid"] for o in self.state["launcher"]}
        try:
            for child in psutil.Process(process.pid).children(recursive=True):
                if child.pid not in recorded:
                    self.state["launcher"].append(identity(child.pid))
        except psutil.NoSuchProcess:
            return
        self.save()

    def launch(self):
        if self.node is None:
            raise StepFailed("node is not on PATH")
        # Never attach to, or later tear down, an instance this run did not start.
        owner = self.handle_owner()
        if owner is not None:
            raise StepFailed(owned_by(owner[0]))
        with open(self.evidence / "launcher.log", "wb") as log:
            process = subprocess.Popen(
                [self.node, "scripts/ui-command.mjs", "start"]
                + ["--port", str(self.port)],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        self.state["launcher"].append(identity(process.pid))
        self.save()
        deadline = time.monotonic() + LAUNCH_TIMEOUT
        while time.monotonic() < deadline:
            text = (self.evidence / "launcher.log").read_text(
                encoding="utf-8", errors="replace"
            )
            match = READY_LINE.search(text)
            if match:
                break
            if process.poll() is not None:
                failed = f"launcher exited with {process.returncode}"
                owner = self.handle_owner()
                if owner is not None and not owner[1]:
                    failed += "; " + owned_by(owner[0])
                raise StepFailed(failed)
            # The Python launcher writes the handle before readiness, so record
            # it now: a launcher that crashes before then still owns its handle.
            self.record_launcher(process)
            time.sleep(0.2)
        else:
            raise StepFailed(f"no readiness line within {LAUNCH_TIMEOUT} seconds")
        instance = match.group(1)
        self.state["instance"] = instance
        self.state["instance_dir"] = f"data/ui-testing/{instance}"
        # Record the Python launcher and anything else under the owned wrapper.
        self.record_launcher(process)
        return f"{instance} at {self.url}/ (launcher PID {match.group(2)})"

    def doctor(self):
        checks = doctor.run_checks(self.url, self.state["instance"])
        text = "\n".join(str(check) for check in checks)
        (self.evidence / "doctor.txt").write_text(text + "\n", encoding="utf-8")
        failed = [check.name for check in checks if not check.ok]
        if failed:
            raise StepFailed("failed checks: " + ", ".join(failed))
        return "all checks pass"

    def drive(self):
        cli = ROOT / "node_modules" / "@playwright" / "test" / "cli.js"
        if not cli.exists():
            raise StepFailed("Playwright is not installed; run npm ci")
        config = SKILL / "drive.config.mjs"
        args = [self.node, str(cli), "test", "--config", str(config)]
        if self.feature:
            args += ["--grep", "@" + self.feature]
        env = {
            **os.environ,
            "VERIFY_BASE_URL": self.url,
            "VERIFY_EVIDENCE": str(self.evidence),
        }
        with open(self.evidence / "drive.txt", "wb") as out:
            process = subprocess.Popen(
                args, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT
            )
            self.state["drive"].append(identity(process.pid))
            self.save()
            try:
                code = process.wait(timeout=DRIVE_TIMEOUT)
            except subprocess.TimeoutExpired as error:
                raise StepFailed(f"drive exceeded {DRIVE_TIMEOUT} seconds") from error
        if code:
            raise StepFailed(f"Playwright exited with {code}; see drive.txt")
        return "Playwright passed: " + (self.feature or "main path")

    def cleanup(self):
        notes = []
        # Browsers and workers are descendants of the recorded drive process.
        for owned in self.state["drive"]:
            if alive(owned):
                for child in psutil.Process(owned["pid"]).children(recursive=True):
                    self.state["drive"].append(identity(child.pid))
        self.save()
        stop_recorded(self.state["drive"], notes)
        # Teardown and recovery act on whatever the shared handle names, so run
        # them only for the handle this run's launcher wrote.
        owner = self.handle_owner()
        if owner is not None and owner[1] and self.node:
            _, output = self.command("stop", name="stop")
            notes.append(output)
        survivors = wait_gone(self.state["launcher"], EXIT_TIMEOUT)
        if survivors:
            # Stop could not reach the instance (for example, still seeding).
            stop_recorded(list(reversed(self.state["launcher"])), notes)
        owner = self.handle_owner()
        if owner is not None and owner[1] and self.node:
            _, output = self.command("recover", name="recover")
            notes.append(output)
        owner = self.handle_owner()
        if owner is not None and not owner[1]:
            notes.append(
                f"data/ui-testing/instance.json belongs to {owner[0]}, "
                "which this run did not launch; left it alone"
            )
        checks = self.leftovers()
        text = "\n".join(notes + [str(check) for check in checks])
        (self.evidence / "cleanup.txt").write_text(text + "\n", encoding="utf-8")
        failed = [check.detail for check in checks if not check.ok]
        if failed:
            raise StepFailed("; ".join(failed))
        return "no recorded process, listener, handle or instance directory remains"

    def leftovers(self):
        owned = self.state["launcher"] + self.state["drive"]
        running = [o["pid"] for o in owned if alive(o)]
        # Without a launcher, whatever listens on the port is not this run's.
        launched = bool(self.state["launcher"])
        listeners = doctor.listening_addresses(self.port) if launched else []
        owner = self.handle_owner()
        ours = owner is not None and owner[1]
        instance_dir = self.state.get("instance_dir")
        return [
            doctor.Check(
                "processes",
                not running,
                f"recorded PIDs still running: {running}"
                if running
                else f"{len(owned)} recorded processes exited",
            ),
            doctor.Check(
                "port",
                not listeners,
                f"port {self.port} still listens on {listeners}"
                if listeners
                else f"port {self.port} is free"
                if launched
                else f"nothing was launched on port {self.port}",
            ),
            doctor.Check(
                "handle",
                not ours,
                "data/ui-testing/instance.json remains"
                if ours
                else "instance handle removed"
                if owner is None
                else f"the handle belongs to {owner[0]}, not this run",
            ),
            doctor.Check(
                "instance directory",
                not (instance_dir and (ROOT / instance_dir).exists()),
                f"{instance_dir} remains"
                if instance_dir and (ROOT / instance_dir).exists()
                else "instance directory removed"
                if instance_dir
                else "this run created no instance directory",
            ),
        ]

    def step(self, name, action):
        try:
            detail = action()
        except (StepFailed, OSError, psutil.Error, subprocess.SubprocessError) as error:
            self.steps.append((name, False, str(error)))
            return False
        self.steps.append((name, True, detail))
        return True

    def execute(self, drive=None):
        try:
            # Later steps run only after earlier ones pass.
            _ = (
                self.step("launch", self.launch)
                and self.step("doctor", self.doctor)
                and self.step("drive", (lambda: drive(self)) if drive else self.drive)
            )
        finally:
            self.step("cleanup", self.cleanup)
            self.write_summary()
        return all(ok for _, ok, _ in self.steps)

    def write_summary(self):
        ok = all(passed for _, passed, _ in self.steps)
        lines = [
            f"# Verify run {self.evidence.name}",
            "",
            f"Result: **{'PASS' if ok else 'FAIL'}**  ",
            f"Instance: {self.state.get('instance', 'not started')} "
            f"at {self.state['url']}  ",
            f"Feature: {self.state['feature']}",
            "",
            "| Step | Result | Detail |",
            "| --- | --- | --- |",
        ]
        for name, passed, detail in self.steps:
            cell = detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {name} | {'pass' if passed else 'FAIL'} | {cell} |")
        for title, name, tail in (
            ("Doctor", "doctor.txt", None),
            ("Cleanup", "cleanup.txt", None),
            ("Drive output (tail)", "drive.txt", 25),
            ("Launcher log (tail)", "launcher.log", 15),
        ):
            path = self.evidence / name
            if path.exists():
                text = (
                    path.read_text(encoding="utf-8", errors="replace")
                    .strip()
                    .splitlines()
                )
                body = "\n".join(text[-tail:] if tail else text)
                lines += ["", f"## {title}", "", "```text", body, "```"]
        shots = sorted(self.evidence.glob("screenshots/*.png"))
        if shots:
            lines += ["", "## Screenshots", ""]
            lines += [f"- {s.relative_to(self.evidence).as_posix()}" for s in shots]
        (self.evidence / "summary.md").write_text("\n".join(lines) + "\n", "utf-8")


def main(argv=None, drive=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--feature", choices=FEATURES, help="drive only this area")
    parser.add_argument("--port", type=int, help="default: UI_PORT, else 4173")
    parser.add_argument("--out", type=Path, default=ROOT / ".verify")
    args = parser.parse_args(argv)
    if args.port is None:
        try:
            args.port = doctor.load_launcher().ui_port()
        except ValueError as error:
            parser.error(str(error))
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    evidence = (args.out / stamp).resolve()
    evidence.mkdir(parents=True)
    run = Run(evidence, args.port, args.feature)
    ok = run.execute(drive)
    print(f"{'PASS' if ok else 'FAIL'}: evidence in {evidence}")
    print((evidence / "summary.md").read_text(encoding="utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
