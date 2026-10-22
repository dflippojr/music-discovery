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
    baseline_lower: float
    random_upper: float
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
    interval in the same run.
    """
    _cutoff(baseline, run)
    _same(baseline, run)
    checks = []
    for ranker in GATED_RANKERS:
        for metric in METRICS:
            lower = _cell(baseline, ranker, metric)["lo"]
            random_upper = _cell(run, REFERENCE_RANKER, metric)["hi"]
            try:
                value = _cell(run, ranker, metric)["mean"]
            except GateError:
                value = None
            problem = ""
            if not _defined(value):
                problem = "no defined score in the run"
            elif value < lower:
                problem = "below the baseline's lower bound"
            elif value < random_upper:
                problem = "below random's upper bound"
            checks.append(Check(ranker, metric, value, lower, random_upper, problem))
    return checks


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
        value = "undefined" if check.value is None else f"{check.value:.3f}"
        result = "pass" if check.passed else f"**FAIL**: {check.problem}"
        lines.append(
            f"| {check.ranker} | {METRICS[check.metric]} | {value} | "
            f"{check.baseline_lower:.3f} | {check.random_upper:.3f} | {result} |"
        )
    failed = [check for check in checks if not check.passed]
    verdict = f"{len(failed)} of {len(checks)} checks failed" if failed else "all pass"
    return "\n".join([*lines, "", f"Verdict: {verdict}.", ""])


def _load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError(f"cannot read {path}: {error}") from None


def load_baseline(pointer: Path) -> dict:
    """The committed report that ``reports/baseline.json`` points at."""
    target = _load(pointer).get("report")
    if not isinstance(target, str):
        raise GateError(f"{pointer} has no 'report' file name")
    return _load(pointer.parent / target)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval_gate", description=__doc__)
    parser.add_argument("baseline", type=Path, help="reports/baseline.json")
    parser.add_argument("run", type=Path, help="the offline-<date>.json to judge")
    args = parser.parse_args(argv)
    try:
        checks = compare(load_baseline(args.baseline), _load(args.run))
    except GateError as error:
        print(f"eval-gate: {error}", file=sys.stderr)
        print(f"### Evaluation gate\n\n**FAIL**: {error}\n")
        return 1
    print(render(checks), end="")
    return 0 if all(check.passed for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
