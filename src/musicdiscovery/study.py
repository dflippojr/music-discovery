"""Analyse the listener study: validate returned ratings files and write the report.

The analysis follows the plan in docs/listener-study.md, which was written before
any data was collected: a two-sided sign test on the overall preference, thumbs-up
rates per ranker and "new to me and liked" rates per ranker, each with a 95%
Wilson interval. Reports hold aggregates only, never session ids.
"""

import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from musicdiscovery.evaluation import confined

DEFAULT_RATINGS_DIR = Path("ratings")
SCHEMA_NAME = "study-ratings.schema.json"
RANKERS = ("baseline", "hybrid")
Z95 = 1.959963984540054
MAX_SCHEMA_PROBLEMS = 5

LIMITS = (
    "- The study is a pilot with a small convenience sample of acquaintances of the "
    "owner, not a statistically powered test; its intervals are wide and say little "
    "about people in general.",
    "- The catalog is a small, fixed set of openly licensed tracks and is biased "
    "towards the genres it holds, so a good list is limited by what exists.",
    "- Listeners judge from each track's source page, so novelty, presentation and "
    "how quickly they listened all affect ratings.",
    "- Each session is minutes long: it says nothing about long-term satisfaction "
    "or whether people would come back.",
    "- Ratings from one listener are not independent, but the intervals treat them "
    "as if they were, so they are tighter than they should be.",
    "- Listeners saw their own seeds and chosen qualities, so the two lists were "
    "compared on different tasks across people, not on one fixed set of queries.",
)


class StudyError(Exception):
    """Raised when ratings files are missing, invalid or inconsistent."""


@dataclass(frozen=True)
class Interval:
    count: int
    total: int
    low: float
    high: float

    @property
    def rate(self) -> float:
        return self.count / self.total if self.total else math.nan


@dataclass(frozen=True)
class SignTest:
    hybrid: int
    baseline: int
    none: int
    p_value: float
    share: Interval


@dataclass(frozen=True)
class Analysis:
    sessions: int
    tasks: int
    ratings: int
    preference: SignTest
    thumbs_up: dict[str, Interval]
    new_and_liked: dict[str, Interval]


def wilson(count: int, total: int) -> Interval:
    """The 95% Wilson score interval for a proportion."""
    if total == 0:
        return Interval(0, 0, math.nan, math.nan)
    p = count / total
    denom = 1 + Z95**2 / total
    centre = (p + Z95**2 / (2 * total)) / denom
    half = Z95 * math.sqrt(p * (1 - p) / total + Z95**2 / (4 * total**2)) / denom
    return Interval(count, total, max(0.0, centre - half), min(1.0, centre + half))


def sign_test_p(successes: int, trials: int) -> float:
    """Exact two-sided sign test against p = 0.5; 1.0 when there are no trials."""
    if trials == 0:
        return 1.0
    tail = min(successes, trials - successes)
    mass = sum(math.comb(trials, i) for i in range(tail + 1)) / 2**trials
    return min(1.0, 2 * mass)


def _schema() -> dict:
    text = resources.files("musicdiscovery").joinpath(SCHEMA_NAME).read_text("utf-8")
    return json.loads(text)


def _problems(validator: Draft202012Validator, body: Any) -> list[str]:
    errors = sorted(validator.iter_errors(body), key=lambda e: list(map(str, e.path)))
    lines = []
    for error in errors[:MAX_SCHEMA_PROBLEMS]:
        where = "/".join(str(part) for part in error.path) or "top level"
        lines.append(f"{where}: {error.message[:120]}")
    if len(errors) > MAX_SCHEMA_PROBLEMS:
        lines.append(f"and {len(errors) - MAX_SCHEMA_PROBLEMS} more")
    return lines


def _check_consistency(body: dict) -> str | None:
    """Rules the schema cannot say: two different rankers, labels A and B in order."""
    for position, task in enumerate(body["tasks"], start=1):
        lists = task["lists"]
        if [item["label"] for item in lists] != ["A", "B"]:
            return f"task {position}: lists must be labelled A then B"
        if {item["ranker"] for item in lists} != set(RANKERS):
            return f"task {position}: the two lists must come from different rankers"
        if task["task"] != position:
            return f"task {position}: numbered {task['task']}"
    return None


def load_sessions(directory: Path) -> list[dict]:
    """Read and validate every *.json file in `directory`; any bad file is an error."""
    if not directory.is_dir():
        raise StudyError(f"{directory} is not a directory")
    files = sorted(directory.glob("*.json"))
    if not files:
        raise StudyError(f"no ratings files (*.json) in {directory}")
    validator = Draft202012Validator(_schema())
    sessions, seen = [], {}
    for path in files:
        try:
            body = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError) as error:
            raise StudyError(f"{path.name}: not readable JSON ({error})") from error
        problems = _problems(validator, body)
        if problems:
            raise StudyError(
                f"{path.name} does not match {SCHEMA_NAME}:\n  " + "\n  ".join(problems)
            )
        if (problem := _check_consistency(body)) is not None:
            raise StudyError(f"{path.name}: {problem}")
        if body["session_id"] in seen:
            raise StudyError(
                f"{path.name} repeats the session id of {seen[body['session_id']]}"
            )
        seen[body["session_id"]] = path.name
        sessions.append(body)
    return sessions


def analyse(sessions: list[dict]) -> Analysis:
    preferred: Counter[str] = Counter()
    liked: Counter[str] = Counter()
    rated: Counter[str] = Counter()
    fresh_liked: Counter[str] = Counter()
    tasks = 0
    for session in sessions:
        for task in session["tasks"]:
            tasks += 1
            by_label = {item["label"]: item["ranker"] for item in task["lists"]}
            preferred[by_label.get(task["preferred"], "none")] += 1
            for item in task["lists"]:
                for result in item["results"]:
                    rated[item["ranker"]] += 1
                    if result["rating"] == 1:
                        liked[item["ranker"]] += 1
                        if result["new_to_me"]:
                            fresh_liked[item["ranker"]] += 1
    decisive = preferred["hybrid"] + preferred["baseline"]
    test = SignTest(
        hybrid=preferred["hybrid"],
        baseline=preferred["baseline"],
        none=preferred["none"],
        p_value=sign_test_p(preferred["hybrid"], decisive),
        share=wilson(preferred["hybrid"], decisive),
    )
    return Analysis(
        sessions=len(sessions),
        tasks=tasks,
        ratings=sum(rated.values()),
        preference=test,
        thumbs_up={r: wilson(liked[r], rated[r]) for r in RANKERS},
        new_and_liked={r: wilson(fresh_liked[r], rated[r]) for r in RANKERS},
    )


def _pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{100 * value:.1f}%"


def _row(label: str, interval: Interval) -> str:
    span = f"{_pct(interval.low)} to {_pct(interval.high)}"
    return (
        f"| {label} | {interval.count} / {interval.total} | "
        f"{_pct(interval.rate)} | {span} |"
    )


def _rates_table(title: str, rates: dict[str, Interval]) -> list[str]:
    return [
        f"### {title}",
        "",
        "| Ranker | Count | Rate | 95% interval |",
        "| --- | --- | --- | --- |",
        *(_row(name.capitalize(), rates[name]) for name in RANKERS),
        "",
    ]


def render_report(result: Analysis, report_date: date) -> str:
    pref = result.preference
    decisive = pref.hybrid + pref.baseline
    lines = [
        f"# Listener study, {report_date.isoformat()}",
        "",
        "A pilot, not a statistically powered test. The analysis follows the plan in "
        "`docs/listener-study.md`, written before any data was collected.",
        "",
        f"{result.sessions} session{'' if result.sessions == 1 else 's'}, "
        f"{result.tasks} tasks, {result.ratings} track ratings.",
        "",
        "## Overall preference",
        "",
        f"Out of {result.tasks} tasks, listeners preferred the hybrid list in "
        f"{pref.hybrid}, the baseline list in {pref.baseline} and had no preference "
        f"in {pref.none}. Ties are left out of the test.",
        "",
    ]
    if decisive:
        lines += [
            f"- Hybrid preference share: {_pct(pref.share.rate)} of {decisive} "
            f"decided tasks (95% interval {_pct(pref.share.low)} to "
            f"{_pct(pref.share.high)}).",
            f"- Two-sided sign test against an even split: p = {pref.p_value:.3f}.",
            "",
        ]
    else:
        lines += ["No task had a decided preference, so there is no share to test.", ""]
    lines += ["## Track ratings", ""]
    lines += _rates_table("Thumbs-up rate", result.thumbs_up)
    lines += _rates_table("New to me and liked", result.new_and_liked)
    lines += ["## Limits", "", *LIMITS, ""]
    return "\n".join(lines)


def run(ratings_dir: Path, out_dir: Path, report_date: date) -> Path:
    """Validate the files in `ratings_dir` and write the report; returns its path."""
    sessions = load_sessions(ratings_dir)
    out_dir = confined(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"listener-study-{report_date.isoformat()}.md"
    path.write_text(render_report(analyse(sessions), report_date), encoding="utf-8")
    return path
