"""The evaluation gate: compare a fresh offline run with the committed baseline.

CI runs ``python -m musicdiscovery.eval_gate reports/baseline.json RUN.json``
after ``musicdiscovery evaluate`` on the real FMA small catalog. Only the
validation split is gated; test-split numbers are reported by ``evaluate`` and
never judged here. See "The evaluation gate" in ``docs/evaluation.md``.
"""

import argparse
import json
import math
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

GATED_RANKERS = ("hybrid", "profile")
REFERENCE_RANKER = "random"
SPLIT = "validation"
METRICS = {"genre_ndcg": "same-genre nDCG@10", "tag_overlap": "tag overlap@10"}
# Runs are comparable only on the same catalog and the same stored queries.
SAME_KEYS = (("catalog", "source_sha1"), ("catalog", "tracks"), ("queries_sha256",))
# The metrics are labelled @10, so both reports must have been cut off at 10.
CUTOFF = 10


class GateError(Exception):
    """Raised when the baseline or the run cannot be compared."""


@dataclass(frozen=True)
class Check:
    ranker: str
    metric: str
    value: float | None
    baseline_lower: float | None
    random_upper: float | None
    problem: str

    @property
    def passed(self) -> bool:
        return not self.problem


def _defined(value: object) -> bool:
    return isinstance(value, int | float) and not math.isnan(value)


def _cell(report: dict, ranker: str, metric: str) -> dict:
    try:
        cell = report["rankers"][ranker][SPLIT][metric]
    except KeyError:
        raise GateError(f"no {SPLIT} {metric} for {ranker}") from None
    return cell


def _bound(report: dict, who: str, ranker: str, metric: str, key: str) -> float | None:
    """The interval bound ``key`` of a cell, or None when it is not a number.

    A bound that is missing, null or NaN is a GateError (#163). Any other
    non-number is None, so its check fails (#175).
    """
    cell = _cell(report, ranker, metric)
    value = cell.get(key) if isinstance(cell, dict) else None
    if _defined(value):
        return value
    if value is None or isinstance(value, float):  # a NaN bound
        raise GateError(f"{who} has no interval for {ranker}/{metric}")
    return None


def _same(baseline: dict, run: dict) -> None:
    for keys in SAME_KEYS:
        found = []
        for report in (baseline, run):
            for key in keys:
                report = report.get(key) if isinstance(report, dict) else None
            found.append(report)
        if found[0] != found[1]:
            raise GateError(
                f"the run and the baseline differ in {'.'.join(keys)} "
                f"({found[1]!r} vs {found[0]!r}); they are not comparable"
            )


def _cutoff(baseline: dict, run: dict) -> None:
    for name, report in (("baseline", baseline), ("run", run)):
        k = report.get("k") if isinstance(report, dict) else None
        if type(k) is not int or k != CUTOFF:
            raise GateError(
                f"the {name}'s cutoff k is {k!r}, not {CUTOFF}; "
                "the reports are not comparable"
            )


def compare(baseline: dict, run: dict) -> list[Check]:
    """One check per gated ranker and metric on the validation split.

    Both reports must have been cut off at ``CUTOFF`` (10). A check fails when
    the run's point estimate is undefined, is below the lower bound of the
    baseline's 95% interval, or is below the upper bound of ``random``'s
    interval in the same run. A run cell with no ``mean``, one that is not a
    mapping, or one whose ``mean`` is not a number, has an undefined point
    estimate, so its check fails. A bound that is not a number fails its check
    too, but a missing, null or NaN bound is a GateError.
    """
    _cutoff(baseline, run)
    _same(baseline, run)
    checks = []
    for ranker in GATED_RANKERS:
        for metric in METRICS:
            lower = _bound(baseline, "baseline", ranker, metric, "lo")
            random_upper = _bound(run, "run", REFERENCE_RANKER, metric, "hi")
            try:
                cell = _cell(run, ranker, metric)
            except GateError:
                cell = None
            mean = cell.get("mean") if isinstance(cell, dict) else None
            # render() formats the value, so a mean that is not a number is undefined.
            value = mean if isinstance(mean, int | float) else None
            problem = ""
            if not _defined(value):
                problem = "no defined score in the run"
            elif lower is None:
                problem = "the baseline's lower bound is not a number"
            elif value < lower:
                problem = "below the baseline's lower bound"
            elif random_upper is None:
                problem = "random's upper bound is not a number"
            elif value < random_upper:
                problem = "below random's upper bound"
            checks.append(Check(ranker, metric, value, lower, random_upper, problem))
    return checks


def _shown(value: float | None) -> str:
    return "undefined" if value is None else f"{value:.3f}"


def render(checks: Sequence[Check]) -> str:
    """The pass/fail table for the job summary."""
    lines = [
        f"### Evaluation gate ({SPLIT} split)",
        "",
        "| Ranker | Metric | Run | Baseline lower bound | Random upper bound "
        "| Result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for check in checks:
        result = "pass" if check.passed else f"**FAIL**: {check.problem}"
        lines.append(
            f"| {check.ranker} | {METRICS[check.metric]} | {_shown(check.value)} | "
            f"{_shown(check.baseline_lower)} | {_shown(check.random_upper)} "
            f"| {result} |"
        )
    failed = [check for check in checks if not check.passed]
    verdict = f"{len(failed)} of {len(checks)} checks failed" if failed else "all pass"
    return "\n".join([*lines, "", f"Verdict: {verdict}.", ""])


def _inside(path: Path, root: Path, where: str) -> Path:
    """``path`` resolved, or a GateError when it resolves outside ``root``."""
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise GateError(f"refusing to read {path}: it is outside {where}")
    return resolved


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError(f"cannot read {path}: {error}") from None


def load_baseline(pointer: Path) -> dict:
    """The committed report that ``reports/baseline.json`` points at.

    The report must be in the pointer's own directory.
    """
    target = _load(pointer).get("report")
    if not isinstance(target, str):
        raise GateError(f"{pointer} has no 'report' file name")
    report = _inside(
        pointer.parent / target, pointer.parent, f"the directory of {pointer}"
    )
    return _load(report)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_gate", description=__doc__)
    parser.add_argument("baseline", type=Path, help="reports/baseline.json")
    parser.add_argument("run", type=Path, help="the offline-<date>.json to judge")
    args = parser.parse_args(argv)
    try:
        # Both files are read from inside the checkout the gate runs in.
        cwd = Path.cwd()
        baseline = load_baseline(_inside(args.baseline, cwd, "the working directory"))
        checks = compare(
            baseline, _load(_inside(args.run, cwd, "the working directory"))
        )
    except GateError as error:
        print(f"eval-gate: {error}", file=sys.stderr)
        print(f"### Evaluation gate\n\n**FAIL**: {error}\n")
        return 1
    print(render(checks), end="")
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
