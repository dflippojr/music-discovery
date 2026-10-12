"""Summarise the demo's local feedback log (JSON Lines, one thumbs action a line).

Each line is checked against feedback-log.schema.json; a malformed line is
reported and skipped. Within each shown list the latest action on a track wins,
and an action of 0 takes the track's rating back. docs/evaluation.md explains
what these numbers can and cannot say.
"""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from musicdiscovery.evaluation import EvaluationError, confined
from musicdiscovery.study import Interval, wilson

SCHEMA_NAME = "feedback-log.schema.json"
# Older and newer names for the same score term.
TERM_KINDS = {"similarity": "audio"}


class FeedbackError(Exception):
    """Raised when the feedback log cannot be read at all."""


@dataclass(frozen=True)
class LogRead:
    events: list[dict]
    problems: list[str]  # one per skipped line, "line N: why"


@dataclass(frozen=True)
class RankerSummary:
    thumbs_up: Interval
    lists: int  # shown lists with at least one rating
    mrr: float  # mean reciprocal rank of the first thumbs-up; 0 for a list without one


@dataclass(frozen=True)
class Summary:
    events: int
    rankers: dict[str, RankerSummary]
    liked: int
    disliked: int
    terms: dict[str, tuple[int, int]]  # term kind -> (liked results, disliked results)


def _schema() -> dict:
    text = resources.files("musicdiscovery").joinpath(SCHEMA_NAME).read_text("utf-8")
    return json.loads(text)


def _line_problem(validator: Draft202012Validator, body: Any) -> str | None:
    error = min(
        validator.iter_errors(body),
        key=lambda e: list(map(str, e.path)),
        default=None,
    )
    if error is not None:
        where = "/".join(str(part) for part in error.path) or "top level"
        return f"{where}: {error.message[:120]}"
    ranks = [item["rank"] for item in body["shown"]]
    if ranks != list(range(1, len(ranks) + 1)):
        return "shown ranks must run 1, 2, 3, ... in order"
    if {"track_id": body["track_id"], "rank": body["rank"]} not in body["shown"]:
        return "the rated track is not in the shown list at its rank"
    return None


def read_log(path: Path) -> LogRead:
    """Read every line of `path`; good events are kept and bad lines described.

    Like the other commands' paths, `path` must be inside the working directory.
    """
    try:
        text = confined(path).read_text("utf-8")
    except EvaluationError as error:
        raise FeedbackError(str(error)) from error
    except (OSError, UnicodeDecodeError) as error:
        raise FeedbackError(f"cannot read {path} ({error})") from error
    validator = Draft202012Validator(_schema())
    events, problems = [], []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            body = json.loads(line)
        except ValueError as error:
            problems.append(f"line {number}: not JSON ({error})")
            continue
        if (problem := _line_problem(validator, body)) is not None:
            problems.append(f"line {number}: {problem}")
            continue
        events.append(body)
    return LogRead(events, problems)


def _list_key(event: dict) -> tuple:
    return (
        event["ranker"],
        tuple(event["seed_track_ids"]),
        tuple(event["likes"]),
        tuple(event["dislikes"]),
        event["exclude_seed_artist"],
        tuple(item["track_id"] for item in event["shown"]),
    )


def latest_ratings(events: list[dict]) -> dict[tuple, dict[int, dict]]:
    """Per shown list, each track's latest rating event, without taken-back ones."""
    lists: dict[tuple, dict[int, dict]] = defaultdict(dict)
    for event in events:
        rated = lists[_list_key(event)]
        if event["rating"] == 0:
            rated.pop(event["track_id"], None)
        else:
            rated[event["track_id"]] = event
    return {key: rated for key, rated in lists.items() if rated}


def term_kind(name: str) -> str:
    """'genre:Folk' -> 'genre', 'axis:energy+' -> 'axis'; 'similarity' -> 'audio'."""
    kind = name.split(":", 1)[0]
    return TERM_KINDS.get(kind, kind)


def summarise(events: list[dict]) -> Summary:
    up: Counter[str] = Counter()
    rated: Counter[str] = Counter()
    reciprocal: dict[str, list[float]] = defaultdict(list)
    terms: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    liked = disliked = 0
    for key, ratings in latest_ratings(events).items():
        ranker = key[0]
        ups = [e["rank"] for e in ratings.values() if e["rating"] == 1]
        reciprocal[ranker].append(1 / min(ups) if ups else 0.0)
        for event in ratings.values():
            rated[ranker] += 1
            is_up = event["rating"] == 1
            up[ranker] += is_up
            liked += is_up
            disliked += not is_up
            for kind in {term_kind(name) for name in event["explanation_terms"]}:
                terms[kind][0 if is_up else 1] += 1
    rankers = {
        name: RankerSummary(
            thumbs_up=wilson(up[name], rated[name]),
            lists=len(reciprocal[name]),
            mrr=sum(reciprocal[name]) / len(reciprocal[name]),
        )
        for name in sorted(reciprocal)
    }
    return Summary(
        events=len(events),
        rankers=rankers,
        liked=liked,
        disliked=disliked,
        terms={kind: (a, b) for kind, (a, b) in sorted(terms.items())},
    )


def _pct(count: int, total: int) -> str:
    return f"{100 * count / total:.0f}%" if total else "n/a"


def _span(interval: Interval) -> str:
    # Every listed ranker has at least one rating, so the interval is defined.
    return f"{100 * interval.low:.0f}% to {100 * interval.high:.0f}%"


def render(summary: Summary) -> str:
    """The three summaries as plain text tables."""
    lines = [f"{summary.events} valid events."]
    if not summary.rankers:
        lines.append("No ratings remain once taken-back ratings are removed.")
        return "\n".join(lines) + "\n"
    lines += [
        "",
        "Thumbs-up rate (latest rating of each track in each shown list)",
        f"  {'ranker':<10}{'up':>6}{'rated':>8}{'rate':>7}  95% interval",
    ]
    for name, item in summary.rankers.items():
        rate = item.thumbs_up
        lines.append(
            f"  {name:<10}{rate.count:>6}{rate.total:>8}"
            f"{_pct(rate.count, rate.total):>7}  {_span(rate)}"
        )
    lines += [
        "",
        "MRR of the first thumbs-up in each rated list (0 when a list has none)",
        f"  {'ranker':<10}{'lists':>6}{'MRR':>8}",
    ]
    for name, item in summary.rankers.items():
        lines.append(f"  {name:<10}{item.lists:>6}{item.mrr:>8.3f}")
    lines += [
        "",
        "Explanation terms shown on liked and disliked results",
        f"  {'term':<12}{'liked':>16}{'disliked':>16}",
    ]
    for kind, (on_liked, on_disliked) in summary.terms.items():
        liked = f"{on_liked}/{summary.liked} ({_pct(on_liked, summary.liked)})"
        disliked = (
            f"{on_disliked}/{summary.disliked} ({_pct(on_disliked, summary.disliked)})"
        )
        lines.append(f"  {kind:<12}{liked:>16}{disliked:>16}")
    return "\n".join(lines) + "\n"
