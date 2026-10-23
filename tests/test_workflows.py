"""Checks on how the workflows wire gate-guard (#104), review-v3 (#126) and the
evaluation gate (#105)."""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
# The one dflippojr/.github commit both guards load, workflow and script alike.
GUARD_SHA = "27bdb76481888567a6c524ebbb19bf4a6c7e7e20"
GUARD_WORKFLOWS = "dflippojr/.github/.github/workflows"
READ_PERMISSIONS = (
    r"^permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n"
)
REVIEW_WORKFLOW = "dflippojr/agent-harness/.github/workflows/review.yml@review-v3"
# The label the owner's review runner registers under (#126).
REVIEW_RUNS_ON = '["self-hosted","Windows","X64","music-discovery-review"]'


def _code(path: Path) -> str:
    """The workflow without comment lines, so a key named in a comment doesn't count."""
    lines = path.read_text(encoding="utf-8").splitlines()
    code = [line for line in lines if not line.lstrip().startswith("#")]
    return "\n".join(code) + "\n"


def _jobs(workflow: str) -> dict[str, str]:
    """Each top-level job's body, keyed by its ID."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_trusted_gate_guard_runs_from_main_and_runs_no_pull_request_code():
    # Under pull_request a pull request supplies its own judge, so it could
    # swap the guard for a green no-op. pull_request_target reads this caller
    # from main, and the called workflow only fetches the pull request's
    # commits as git objects. Its gate-guard check is the one main requires.
    target = _code(WORKFLOWS / "gate-guard-target.yml")
    header = target.split("\njobs:\n", 1)[0]
    assert re.search(
        r"^on:\n  pull_request_target:\n"
        r"    types: \[opened, synchronize, reopened, edited, labeled, unlabeled, "
        r"closed\]\n",
        header,
        re.M,
    )
    assert "pull_request:" not in target
    assert "workflow_dispatch" not in target
    assert re.search(READ_PERMISSIONS + r"  checks: write\n", header, re.M)
    group = "group: gate-guard-target-${{ github.event.pull_request.head.sha }}"
    assert group in header
    assert "cancel-in-progress: true" in header
    jobs = _jobs(target)
    assert set(jobs) == {"judge"}
    judge = jobs["judge"]
    assert f"    uses: {GUARD_WORKFLOWS}/gate-guard-target.yml@{GUARD_SHA}\n" in judge
    assert f"      guard-ref: {GUARD_SHA}\n" in judge
    for unsafe in (
        "steps:",
        "run:",
        "actions/checkout",
        "secrets",
        "self-hosted",
        "runs-on",
        "github.event.pull_request.title",
        "github.head_ref",
    ):
        assert unsafe not in judge, unsafe


def test_ci_gate_needs_the_pinned_gate_guard_with_owner_only_labels():
    # ci.yml's copy is an early signal inside gate, not the judge: a pull
    # request can rewrite it. It loads the same reviewed script as the trusted
    # guard, and counts only a label the owner applied.
    ci = _code(WORKFLOWS / "ci.yml")
    on = r"^on:\n  push:\n    branches: \[main\]\n  pull_request:\n"
    assert re.search(on, ci, re.M)
    assert "pull_request_target" not in ci
    assert re.search(READ_PERMISSIONS, ci.split("\njobs:\n", 1)[0], re.M)
    jobs = _jobs(ci)
    guard = jobs["gate-guard"]
    assert f"    uses: {GUARD_WORKFLOWS}/gate-guard.yml@{GUARD_SHA}\n" in guard
    assert f"      guard-ref: {GUARD_SHA}\n" in guard
    assert "      require-owner-label-actor: true\n" in guard
    assert "self-hosted" not in guard
    assert "    needs: [test, demo, ui, eval-gate, gate-guard]\n" in jobs["gate"]
    assert "    if: always()\n" in jobs["gate"]


def test_every_dflippojr_github_reference_uses_the_one_guard_sha():
    refs = set()
    for path in WORKFLOWS.glob("*.yml"):
        refs.update(re.findall(r"dflippojr/\.github/\S+@(\S+)", _code(path)))
    assert refs == {GUARD_SHA}


def test_review_caller_runs_review_v3_only_from_main_on_trusted_triggers():
    # pull_request_target reads this caller from main, and the owner's runner
    # hook permits only this file on main. A pull_request trigger would run a
    # fork's copy of the caller on the self-hosted runner, so it stays out.
    review = _code(WORKFLOWS / "review.yml")
    header = review.split("\njobs:\n", 1)[0]
    assert re.search(
        r"^on:\n  pull_request_target:\n    types: \[opened\]\n  workflow_dispatch:\n",
        header,
        re.M,
    )
    assert "pull_request:" not in review
    # A called workflow can't hold more than its caller grants, so issues: read
    # has to be here for review-v3 to fetch the spec.
    assert re.search(
        r"^permissions:\n  contents: read\n  pull-requests: write\n"
        r"  checks: write\n  issues: read\n",
        header,
        re.M,
    )
    jobs = _jobs(review)
    assert set(jobs) == {"review"}
    job = jobs["review"]
    assert "head.repo.full_name == github.repository" in job
    assert "dependabot[bot]" in job
    assert f"    uses: {REVIEW_WORKFLOW}\n" in job
    assert f"      runs_on: '{REVIEW_RUNS_ON}'\n" in job
    assert "      tooling_ref: review-v3\n" in job
    assert "secrets" not in job


def test_eval_gate_always_reports_and_gate_requires_it():
    # The job runs on every pull request, so gate can require it; the called
    # workflow decides inside whether the diff makes it "not applicable".
    ci = _code(WORKFLOWS / "ci.yml")
    job = _jobs(ci)["eval-gate"]
    assert "    uses: ./.github/workflows/eval-gate.yml\n" in job
    assert "    if:" not in job
    assert "eval-gate" in _jobs(ci)["gate"]


def test_eval_gate_workflow_verifies_caches_and_compares_the_archive():
    gate = _code(WORKFLOWS / "eval-gate.yml")
    assert "workflow_call:" in gate
    assert "pull_request" not in gate.split("\njobs:\n", 1)[0]
    assert "runs-on: ubuntu-latest" in gate
    assert "self-hosted" not in gate
    # The key is the archive's documented SHA1; ingest verifies it, and the
    # cache is saved only after ingest succeeded.
    sha1 = "f0df49ffe5f2a6008d7dc83c6915b31835dfe733"
    assert f"FMA_SHA1: {sha1}" in gate
    assert "key: fma-metadata-${{ env.FMA_SHA1 }}" in gate
    assert sha1 in (ROOT / "docs" / "catalog.md").read_text(encoding="utf-8")
    assert "steps.ingest.outcome == 'success'" in gate
    for ranker in ("baseline", "hybrid", "profile", "random", "popular"):
        assert f"--ranker {ranker}" in gate
    assert "python -m musicdiscovery.eval_gate reports/baseline.json" in gate
    assert "name: eval-run" in gate
    assert "not applicable" in gate


def test_eval_gate_runs_nightly_on_main_through_the_same_workflow():
    nightly = _code(WORKFLOWS / "eval-nightly.yml")
    assert re.search(r"^on:\n  schedule:\n    - cron: ", nightly, re.M)
    assert "pull_request" not in nightly
    job = _jobs(nightly)["eval-gate"]
    assert "    uses: ./.github/workflows/eval-gate.yml\n" in job
    assert "      always: true\n" in job


def _step(workflow: str, name: str) -> str:
    """The body of the step called `name`, up to the next step."""
    for step in re.split(r"^      - ", workflow, flags=re.M)[1:]:
        if re.match(rf"name: {re.escape(name)}\n", step):
            return step
    raise AssertionError(f"no step named {name!r}")


def _script(step: str) -> str:
    """The shell text of a step's `run: |` block."""
    block = step.split("        run: |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line.strip() and not line.startswith(" " * 10):
            break
        lines.append(line)
    return textwrap.dedent("\n".join(lines)) + "\n"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def _bash() -> str:
    """A Bash that runs the workflow's scripts as the hosted runner does.

    On Windows that is Git for Windows' Bash, never System32's WSL launcher, which
    a bare `bash` resolves to first. Fails, rather than skips, when there is none.
    """
    if os.name != "nt":
        found = shutil.which("bash")
    else:
        git = shutil.which("git")
        roots = list(Path(git).resolve().parents)[:3] if git else []
        roots.append(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git")
        bashes = (root / "bin" / "bash.exe" for root in roots)
        found = next((str(bash) for bash in bashes if bash.is_file()), None)
    assert found, "no usable Bash to run the eval-gate scope step (Git Bash on Windows)"
    return found


def _scope(tmp_path: Path, changed: str, base: str | None = None, **env: str):
    """Run the eval-gate scope step on a two-commit repo; return (exit, output, log)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    target = repo / changed
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("two\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "head")
    gate = _code(WORKFLOWS / "eval-gate.yml")
    script = _script(_step(gate, "Decide whether the ranker path changed"))
    output, summary = tmp_path / "output", tmp_path / "summary"
    output.touch()
    summary.touch()
    # The caller's environment keeps PATH and SYSTEMROOT valid on every OS; drop
    # GIT_* so an outer repository can't redirect the diff.
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    done = subprocess.run(
        [_bash(), "-e", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            **inherited,
            "HOME": str(tmp_path),
            "ALWAYS": "false",
            "EVENT": "pull_request",
            "BASE_SHA": base or base_sha,
            # Forward slashes, which Bash reads on Windows too.
            "GITHUB_OUTPUT": output.as_posix(),
            "GITHUB_STEP_SUMMARY": summary.as_posix(),
            **env,
        },
        check=False,
    )
    log = f"exit {done.returncode}\nstdout:\n{done.stdout}\nstderr:\n{done.stderr}"
    return done.returncode, output.read_text(encoding="utf-8"), log


def test_eval_gate_scope_runs_when_the_diff_touches_the_ranker_path(tmp_path):
    code, output, log = _scope(tmp_path, "src/musicdiscovery/hybrid.py")
    assert (code, output) == (0, "run=true\n"), log


def test_eval_gate_scope_skips_when_the_diff_touches_nothing_relevant(tmp_path):
    code, output, log = _scope(tmp_path, "docs/other.md")
    assert (code, output) == (0, "run=false\n"), log


def test_eval_gate_scope_runs_when_the_diff_cannot_be_computed(tmp_path):
    # An unavailable base object is an error, not an irrelevant pull request:
    # the gate fails closed and evaluates.
    code, output, log = _scope(tmp_path, "docs/other.md", base="0" * 40)
    assert (code, output) == (0, "run=true\n"), log


def test_eval_gate_uploads_the_report_even_from_a_hidden_directory():
    gate = _code(WORKFLOWS / "eval-gate.yml")
    step = _step(gate, "Upload the run's report")
    path = re.search(r"^          path: (\S+)$", step, re.M).group(1)
    assert "if: always() && steps.scope.outputs.run == 'true'" in step
    # upload-artifact skips dot-directories unless told otherwise, which is how
    # the first hosted run uploaded nothing.
    if any(part.startswith(".") for part in Path(path).parts):
        assert "          include-hidden-files: true\n" in step
    # A missing report fails the job instead of passing without evidence.
    assert "          if-no-files-found: error\n" in step


# scripts/ci-local.sh is how AGENTS.md says to prove a change, so it must run
# what the test, demo and ui jobs of ci.yml run, in the same order (#137).
CI_LOCAL = ROOT / "scripts" / "ci-local.sh"
LOCAL_JOBS = ("test", "demo", "ui")
SETUP = "one-time setup, listed in the script's header and AGENTS.md"
# Each command of those jobs that ci-local.sh doesn't run, and why.
CI_ONLY = {
    ("test", "actions/checkout"): "CI checks out the commit; locally it's the tree",
    ("test", "actions/setup-python"): "the developer provides Python 3.12",
    ("test", 'python -m pip install -e ".[dev]"'): SETUP,
    ("test", "actions/upload-artifact"): "uploads coverage for SonarCloud",
    ("demo", "actions/checkout"): "CI checks out the commit; locally it's the tree",
    ("demo", "actions/setup-python"): "the developer provides Python 3.12",
    ("demo", "actions/setup-node"): "the developer provides Node",
    ("demo", 'python -m pip install -e ".[dev]"'): SETUP,
    ("demo", 'test "$sha" = "$GITHUB_SHA"'): (
        "GITHUB_SHA exists only in CI; the script compares with HEAD instead"
    ),
    ("demo", "npm ci"): SETUP,
    ("demo", "npx playwright install --with-deps chromium"): SETUP,
    ("demo", "actions/upload-artifact"): "uploads coverage for SonarCloud",
    ("ui", "actions/checkout"): "CI checks out the commit; locally it's the tree",
    ("ui", "actions/setup-python"): "the developer provides Python 3.12",
    ("ui", "actions/setup-node"): "the developer provides Node",
    ("ui", 'python -m pip install -e ".[dev]"'): SETUP,
    ("ui", "npm ci"): SETUP,
    ("ui", "npx playwright install --with-deps chromium webkit"): SETUP,
    ("ui", "actions/upload-artifact"): "uploads reports and traces as artifacts",
}


def _ci_commands(job: str) -> list[tuple[str, str]]:
    """(step label, command) for each step of a ci.yml job, in order.

    A `uses:` step is one command, the action without its version. A `run:`
    step is one command per line, after its `env:` assignments as a shell
    writes them.
    """
    body = _jobs(_code(WORKFLOWS / "ci.yml"))[job].split("    steps:\n", 1)[1]
    commands = []
    for step in re.split(r"^      - ", body, flags=re.M)[1:]:
        step = " " * 8 + step
        uses = re.search(r"^        uses: ([^@\s]+)", step, re.M)
        name = re.search(r"^        name: (.+)$", step, re.M)
        label = name.group(1) if name else uses.group(1)
        if uses:
            commands.append((label, uses.group(1)))
            continue
        env = re.search(r"^        env:\n((?:          .*\n)+)", step, re.M)
        prefix = ""
        if env:
            pairs = (line.strip().split(": ", 1) for line in env.group(1).splitlines())
            prefix = "".join(f"{key}={value} " for key, value in pairs)
        run = re.search(r"^        run: (.*)$", step, re.M).group(1)
        lines = _script(step).splitlines() if run == "|" else [run]
        for line in filter(None, (line.strip() for line in lines)):
            # A workflow command annotates the run; a local shell would print
            # it literally, so the script leaves it out.
            commands.append((label, prefix + line.replace("::error::", "")))
    return commands


def _local_commands(job: str) -> list[str]:
    """The command lines of `job_<job>` in ci-local.sh, without the `run` wrapper."""
    script = CI_LOCAL.read_text(encoding="utf-8")
    found = re.search(rf"^job_{job}\(\) {{\n(.*?)^}}\n", script, re.M | re.S)
    assert found, f"scripts/ci-local.sh has no job_{job}"
    lines = (line.strip() for line in found.group(1).splitlines())
    return [line.removeprefix("run ") for line in lines if line and line[0] != "#"]


@pytest.mark.parametrize("job", LOCAL_JOBS)
def test_ci_local_runs_every_command_of_the_ci_job_in_ci_order(job):
    local = _local_commands(job)
    at = 0
    for label, command in _ci_commands(job):
        if (job, command) in CI_ONLY:
            continue
        where = f"scripts/ci-local.sh job_{job}, for ci.yml step {label!r}"
        assert command in local, f"{where}: no line runs {command!r}"
        assert command in local[at:], f"{where}: {command!r} runs out of CI order"
        at = local.index(command, at) + 1


def test_ci_only_names_only_commands_ci_yml_still_runs():
    # A stale entry would let a later step of the same text pass unchecked.
    commands = {(job, c) for job in LOCAL_JOBS for _, c in _ci_commands(job)}
    assert not set(CI_ONLY) - commands, sorted(set(CI_ONLY) - commands)


def _stamp_check(tmp_path: Path, stamp: str, tracked_change: bool, untracked: bool):
    """Run job_demo's stamp check on a one-commit repo; return (exit, log)."""
    script = CI_LOCAL.read_text(encoding="utf-8")
    body = re.search(r"^job_demo\(\) \{\n(.*?)^\}\n", script, re.M | re.S).group(1)
    lines = body.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("sha="))
    end = next(i for i, line in enumerate(lines) if "test:parity" in line)
    check = textwrap.dedent("\n".join(lines[start:end])) + "\n"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("one\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    head = _git(repo, "rev-parse", "HEAD")
    if tracked_change:
        (repo / "README.md").write_text("two\n", encoding="utf-8")
    if untracked:
        (repo / "new.txt").write_text("new\n", encoding="utf-8")
    bundle = repo / ".demo-fixture"
    bundle.mkdir()
    sha = stamp.replace("HEAD", head)
    (bundle / "SOURCE.json").write_text(json.dumps({"sha": sha}), encoding="utf-8")
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    # The interpreter running the tests answers to `python`, as .venv's does.
    path = os.pathsep.join([str(Path(sys.executable).parent), inherited["PATH"]])
    done = subprocess.run(
        [_bash(), "-e", "-c", check],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**inherited, "HOME": str(tmp_path), "PATH": path},
        check=False,
    )
    return done.returncode, f"exit {done.returncode}\n{done.stdout}{done.stderr}"


@pytest.mark.parametrize(
    ("stamp", "tracked_change", "untracked", "passes"),
    [
        ("HEAD", False, False, True),
        ("HEAD", False, True, True),
        ("HEAD-dirty", True, False, True),
        ("HEAD", True, False, False),
        ("HEAD-dirty", False, False, False),
        ("0" * 40, False, False, False),
    ],
)
def test_ci_local_stamp_check_wants_head_dirty_only_with_tracked_changes(
    tmp_path, stamp, tracked_change, untracked, passes
):
    code, log = _stamp_check(tmp_path, stamp, tracked_change, untracked)
    assert (code == 0) == passes, log
