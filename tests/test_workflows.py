"""Checks on how the workflows wire gate-guard (#104) and review-v3 (#126)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
# The one dflippojr/.github commit both guards load, workflow and script alike.
GUARD_SHA = "e2c7fae8508be0359f3abaf136caade46ee7479c"
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
    assert "    needs: [test, demo, ui, gate-guard]\n" in jobs["gate"]
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
