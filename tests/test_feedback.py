"""Check the feedback log reader and `feedback-report` on synthetic JSON Lines."""

import json
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from musicdiscovery import feedback
from musicdiscovery.cli import main

SHOWN = [100, 101, 102, 103]
# Where str.splitlines() also breaks lines; JSON leaves the last three raw.
LINE_BREAKS = "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"


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


@pytest.mark.parametrize(
    ("location", "good"),
    [
        (("properties", "time"), "2026-10-12T10:00:00Z"),
        (("properties", "explanation_terms", "items"), "genre:Folk"),
        (("$defs", "quality"), "genre:Folk"),
    ],
)
def test_a_trailing_newline_is_not_a_valid_value(location, good):
    # jsonschema matches with re.search, where $ also matches before a final newline.
    node = feedback._schema()
    for key in location:
        node = node[key]
    validator = Draft202012Validator({"type": "string", "pattern": node["pattern"]})
    assert validator.is_valid(good)
    assert not validator.is_valid(good + "\n")


def test_a_line_with_a_trailing_newline_in_a_value_is_skipped(tmp_path):
    path = write_log(
        tmp_path / "feedback.jsonl",
        event(100, 1),
        {**event(101, 1), "time": "2026-10-12T10:00:00Z\n"},
        event(102, -1, terms=("genre:Folk\n",)),
    )
    result = feedback.read_log(path)
    assert [e["track_id"] for e in result.events] == [100]
    assert [p.split(":")[0] for p in result.problems] == ["line 2", "line 3"]
    assert "time" in result.problems[0]
    assert "explanation_terms" in result.problems[1]


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


def test_crlf_and_bom_logs_are_read(tmp_path):
    path = tmp_path / "feedback.jsonl"
    text = "".join(json.dumps(event(i, 1)) + "\r\n" for i in (100, 101))
    path.write_bytes(("\ufeff" + text).encode("utf-8"))
    result = feedback.read_log(path)
    assert len(result.events) == 2
    assert result.problems == []


def test_a_line_separator_inside_a_term_does_not_split_the_event(tmp_path):
    path = tmp_path / "feedback.jsonl"
    tags = ["tag:Live\u2028Session", "tag:caf\x85", "tag:Pop\u2029Rock"]
    lines = [
        json.dumps(event(100, 1, terms=(tag,)), ensure_ascii=False) for tag in tags
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = feedback.read_log(path)
    assert result.problems == []
    assert [e["explanation_terms"] for e in result.events] == [[tag] for tag in tags]


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    tag=st.text(
        alphabet=st.one_of(
            st.characters(exclude_characters="\n", exclude_categories=("Cs",)),
            st.sampled_from(LINE_BREAKS),
        ),
        min_size=1,
        max_size=60,
    )
)
def test_no_character_inside_a_term_splits_the_event(tag):
    # A term may hold any character but \n, so no reader may split on the others.
    Path("feedback.jsonl").write_text(
        json.dumps(event(100, 1, terms=(f"tag:{tag}",)), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = feedback.read_log(Path("feedback.jsonl"))
    assert result.problems == []
    assert [e["explanation_terms"] for e in result.events] == [[f"tag:{tag}"]]


def test_skipped_line_numbers_count_newline_separated_lines(tmp_path):
    path = tmp_path / "feedback.jsonl"
    first = json.dumps(
        event(100, 1, terms=("tag:Live\u2028Session",)), ensure_ascii=False
    )
    path.write_text(
        first + "\n{not json\n" + json.dumps(event(101, -1)) + "\n", encoding="utf-8"
    )
    result = feedback.read_log(path)
    assert len(result.events) == 2
    assert [p.split(":")[0] for p in result.problems] == ["line 2"]


def test_a_lone_cr_does_not_end_a_line(tmp_path):
    # JSON Lines end a line at \n only; a bare \r is not a separator.
    path = tmp_path / "feedback.jsonl"
    path.write_bytes(
        (json.dumps(event(100, 1)) + "\r" + json.dumps(event(101, 1)) + "\n").encode()
    )
    result = feedback.read_log(path)
    assert result.events == []
    assert [p.split(":")[0] for p in result.problems] == ["line 1"]


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


def rated_by_track(events):
    (ratings,) = feedback.latest_ratings(events).values()
    return {track: e["rating"] for track, e in ratings.items()}


def test_the_latest_time_wins_whatever_the_file_order():
    old = event(100, 1, time="2026-10-12T10:00:00Z")
    new = event(100, -1, time="2026-10-12T11:00:00Z")
    for events in ([old, new], [new, old]):
        assert rated_by_track(events) == {100: -1}


def test_an_older_take_back_does_not_erase_a_newer_rating():
    rating = event(100, 1, time="2026-10-12T11:00:00Z")
    take_back = event(100, 0, time="2026-10-12T10:00:00Z")
    for events in ([rating, take_back], [take_back, rating]):
        assert rated_by_track(events) == {100: 1}


def test_a_newer_take_back_erases_an_older_rating_in_either_file_order():
    rating = event(100, 1, time="2026-10-12T10:00:00Z")
    take_back = event(100, 0, time="2026-10-12T11:00:00Z")
    for events in ([rating, take_back], [take_back, rating]):
        assert feedback.latest_ratings(events) == {}


def test_equal_times_resolve_to_the_later_input_position():
    up = event(100, 1)
    down = event(100, -1)
    assert up["time"] == down["time"]
    assert rated_by_track([up, down]) == {100: -1}
    assert rated_by_track([down, up]) == {100: 1}
    assert feedback.latest_ratings([up, event(100, 0)]) == {}


def test_summaries_use_the_time_order_not_the_file_order():
    old = event(100, 1, time="2026-10-12T10:00:00Z")
    new = event(100, -1, time="2026-10-12T11:00:00Z")
    forward = feedback.summarise([old, new])
    backward = feedback.summarise([new, old])
    assert backward == forward
    assert (forward.liked, forward.disliked) == (0, 1)
    assert forward.rankers["hybrid"].mrr == 0.0
