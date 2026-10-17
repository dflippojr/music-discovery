"""Check the feedback log reader and `feedback-report` on synthetic JSON Lines."""

import json
import re

import pytest

from musicdiscovery import feedback
from musicdiscovery.cli import main

SHOWN = [100, 101, 102, 103]


def event(track_id, rating, ranker="hybrid", terms=("audio",), shown=SHOWN, **extra):
    return {
        "version": 1,
        "time": "2026-10-12T10:00:00Z",
        "ranker": ranker,
        "seed_track_ids": [120],
        "likes": ["genre:Folk"],
        "dislikes": ["more:energy"],
        "exclude_seed_artist": False,
        "shown": [{"track_id": t, "rank": i} for i, t in enumerate(shown, start=1)],
        "track_id": track_id,
        "rank": shown.index(track_id) + 1,
        "rating": rating,
        "explanation_terms": list(terms),
        **extra,
    }


def lines_of(*items):
    return [(i if isinstance(i, str) else json.dumps(i)) + "\n" for i in items]


def write_log(path, *items):
    path.write_text("".join(lines_of(*items)), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def work_in_tmp(tmp_path, monkeypatch):
    """Logs must be inside the working directory, so work where they are written."""
    monkeypatch.chdir(tmp_path)


def test_cli_refuses_a_log_outside_the_working_directory(tmp_path, capsys, monkeypatch):
    outside = write_log(tmp_path / "feedback.jsonl", event(100, 1))
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert main(["feedback-report", str(outside)]) == 1
    assert "outside the working directory" in capsys.readouterr().err
    assert main(["feedback-report", "../feedback.jsonl"]) == 1


@pytest.fixture
def log(tmp_path):
    return write_log(
        tmp_path / "feedback.jsonl",
        # Hybrid list: thumbs down at 1, up at 3; the down at 1 is taken back.
        event(100, -1, terms=("audio", "genre:Folk")),
        event(102, 1, terms=("metadata", "popularity")),
        event(100, 0),
        event(101, -1, terms=("similarity", "axis:energy-")),
        "{not json",
        "",
        event(103, 1, ranker="random"),
        # Baseline list with no thumbs-up.
        event(100, -1, ranker="baseline", terms=("similarity",)),
        # Second hybrid list (different shown order): up at rank 1.
        event(103, 1, shown=[103, 100], terms=("audio", "tag:punk")),
        event(100, 1, rank=3),
    )


def test_read_log_keeps_good_events_and_describes_bad_lines(log):
    result = feedback.read_log(log)
    assert len(result.events) == 6
    assert [p.split(":")[0] for p in result.problems] == ["line 5", "line 7", "line 10"]
    assert "not JSON" in result.problems[0]
    assert "ranker" in result.problems[1]
    assert "not in the shown list" in result.problems[2]


def test_a_deeply_nested_line_is_reported_not_a_crash(tmp_path):
    nested = "[" * 100_000 + "]" * 100_000
    path = write_log(tmp_path / "nested.jsonl", event(100, 1), nested, event(101, -1))
    result = feedback.read_log(path)
    assert len(result.events) == 2
    assert len(result.problems) == 1
    assert result.problems[0].startswith("line 2: not JSON")


def test_a_bom_prefixed_log_is_read(tmp_path, capsys):
    path = tmp_path / "feedback.jsonl"
    path.write_text("\ufeff" + json.dumps(event(100, 1)) + "\n", encoding="utf-8")
    assert main(["feedback-report", "feedback.jsonl"]) == 0
    assert "1 valid events." in capsys.readouterr().out


def test_summaries(log):
    summary = feedback.summarise(feedback.read_log(log).events)
    hybrid, baseline = summary.rankers["hybrid"], summary.rankers["baseline"]
    assert (hybrid.thumbs_up.count, hybrid.thumbs_up.total) == (2, 3)
    assert (baseline.thumbs_up.count, baseline.thumbs_up.total) == (0, 1)
    assert hybrid.lists == 2
    assert hybrid.mrr == pytest.approx((1 / 3 + 1) / 2)
    assert baseline.mrr == 0
    assert (summary.liked, summary.disliked) == (2, 2)
    assert summary.terms == {
        "audio": (1, 2),
        "axis": (0, 1),
        "metadata": (1, 0),
        "popularity": (1, 0),
        "tag": (1, 0),
    }


def test_the_latest_action_wins_within_a_list():
    events = [event(100, 1), event(100, -1), event(101, 1), event(101, 0)]
    (ratings,) = feedback.latest_ratings(events).values()
    assert {t: e["rating"] for t, e in ratings.items()} == {100: -1}
    assert feedback.latest_ratings([event(100, 1), event(100, 0)]) == {}


def test_shown_ranks_must_be_in_order(tmp_path):
    bad = event(100, 1)
    bad["shown"][1]["rank"] = 5
    result = feedback.read_log(write_log(tmp_path / "log.jsonl", bad))
    assert result.events == []
    assert "ranks must run" in result.problems[0]


def test_cli_prints_the_three_summaries_and_reports_bad_lines(log, capsys):
    assert main(["feedback-report", str(log)]) == 0
    out, err = capsys.readouterr()
    assert "6 valid events." in out
    assert "Thumbs-up rate" in out
    assert "  hybrid         2       3    67%  21% to 94%" in out
    assert "MRR of the first thumbs-up" in out
    assert "  hybrid         2   0.667" in out
    assert "  baseline       1   0.000" in out
    assert "Explanation terms shown on liked and disliked results" in out
    assert re.search(r"^  audio +1/2 \(50%\) +2/2 \(100%\)$", out, re.MULTILINE)
    assert err.count("feedback-report: skipped line") == 3


def test_cli_limits_how_many_bad_lines_it_lists(tmp_path, capsys):
    path = write_log(tmp_path / "log.jsonl", event(100, 1), *["[]"] * 25)
    assert main(["feedback-report", str(path)]) == 0
    err = capsys.readouterr().err
    assert err.count("skipped line") == 20
    assert "skipped 5 more malformed lines" in err


def test_cli_fails_without_valid_events(tmp_path, capsys):
    path = tmp_path / "log.jsonl"
    path.write_text("nonsense\n", encoding="utf-8")
    assert main(["feedback-report", str(path)]) == 1
    out, err = capsys.readouterr()
    assert "0 valid events." in out
    assert "No ratings remain" in out
    assert "no valid events" in err


def test_cli_reports_a_missing_or_unreadable_file(tmp_path, capsys):
    assert main(["feedback-report", str(tmp_path / "missing.jsonl")]) == 1
    assert "cannot read" in capsys.readouterr().err
    binary = tmp_path / "binary.jsonl"
    binary.write_bytes(b"\xff\xfe\x00")
    assert main(["feedback-report", str(binary)]) == 1
    assert "cannot read" in capsys.readouterr().err


def test_render_without_a_thumbs_down_still_reads():
    summary = feedback.summarise([event(100, 1)])
    text = feedback.render(summary)
    assert "1/1 (100%)" in text
    assert "0/0 (n/a)" in text
