"""Checks on how CI wires the shared gate guard from dflippojr/.github (#104)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
# The one dflippojr/.github commit both guards load, workflow and script alike.
GUARD_SHA = "e2c7fae8508be0359f3abaf136caade46ee7479c"


def _code(path: Path) -> str:
    """The workflow without its comment lines, so a key named in a comment doesn't count."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#")) + "\n"


def _jobs(workflow: str) -> dict[str, str]:
    """Each top-level job's body, keyed by its ID."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([\w-]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_trusted_gate_guard_runs_from_the_default_branch_and_runs_no_pull_request_code():
    # Under pull_request a pull request supplies its own judge, so it could
    # swap the guard for a green no-op. pull_request_target reads this caller
    # from main, and the called workflow only fetches the pull request's
    # commits as git objects. Its gate-guard check is the one main requires.
    target = _code(WORKFLOWS / "gate-guard-target.yml")
    header = target.split("\njobs:\n", 1)[0]
    assert re.search(
        r"^on:\n  pull_request_target:\n"
        r"    types: \[opened, synchronize, reopened, edited, labeled, unlabeled\]\n",
        header,
        re.M,
    )
    assert "pull_request:" not in target
    assert "workflow_dispatch" not in target
    assert re.search(
        r"^permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n  checks: write\n",
        header,
        re.M,
    )
    assert "group: gate-guard-target-${{ github.event.pull_request.head.sha }}" in header
    assert "cancel-in-progress: true" in header
    jobs = _jobs(target)
    assert set(jobs) == {"judge"}
    judge = jobs["judge"]
    assert f"    uses: dflippojr/.github/.github/workflows/gate-guard-target.yml@{GUARD_SHA}\n" in judge
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
    assert re.search(r"^on:\n  push:\n    branches: \[main\]\n  pull_request:\n", ci, re.M)
    assert "pull_request_target" not in ci
    header = ci.split("\njobs:\n", 1)[0]
    assert re.search(r"^permissions:\n  contents: read\n  issues: read\n  pull-requests: read\n", header, re.M)
    jobs = _jobs(ci)
    guard = jobs["gate-guard"]
    assert f"    uses: dflippojr/.github/.github/workflows/gate-guard.yml@{GUARD_SHA}\n" in guard
    assert f"      guard-ref: {GUARD_SHA}\n" in guard
    assert "      require-owner-label-actor: true\n" in guard
    assert "self-hosted" not in guard
    assert "    needs: [test, demo, ui, gate-guard]\n" in jobs["gate"]
    assert "    if: always()\n" in jobs["gate"]


def test_every_dflippojr_github_reference_uses_the_one_guard_sha():
    refs = set()
    for path in WORKFLOWS.glob("*.yml"):
        refs.update(re.findall(r"dflippojr/\.github/\S+@(\S+)", _code(path)))
    assert refs == {GUARD_SHA}
