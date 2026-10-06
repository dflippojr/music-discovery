"""Listener study analysis on synthetic ratings; no real participant data."""

import json
import math
import random
import re
from datetime import date
from pathlib import Path

import pytest

from musicdiscovery import study
from musicdiscovery.cli import main

ROOT = Path(__file__).resolve().parent.parent
TIME = "2026-10-06T14:03:09Z"
TOP_LEVEL_FIELDS = {
    "format",
    "version",
    "session_id",
    "started_at",
    "exported_at",
    "tasks",
}


def make_session(index: int, preferences: list[str], up_rates=(0.4, 0.7), seed=0):
    """A valid synthetic file; list A is hybrid in even tasks, baseline in odd."""
    rng = random.Random(seed + index)
    tasks = []
    for position, preferred in enumerate(preferences, start=1):
        order = ["hybrid", "baseline"] if position % 2 == 0 else ["baseline", "hybrid"]
        lists = []
        for label, ranker in zip("AB", order, strict=True):
            rate = up_rates[ranker == "hybrid"]
            results = [
                {
                    "track_id": 1000 + 10 * position + i,
                    "rating": 1 if rng.random() < rate else -1,
                    "new_to_me": rng.random() < 0.5,
                }
                for i in range(5)
            ]
            lists.append({"label": label, "ranker": ranker, "results": results})
        tasks.append(
            {
                "task": position,
                "seed_track_id": 500 + position,
                "likes": ["genre:Folk", "more:energy"],
                "dislikes": ["tag:punk"],
                "started_at": TIME,
                "completed_at": TIME,
                "lists": lists,
                "preferred": preferred,
            }
        )
    return {
        "format": "music-discovery-study",
        "version": 1,
        "session_id": f"{index:032x}",
        "started_at": TIME,
        "exported_at": TIME,
        "tasks": tasks,
    }


def write(directory: Path, name: str, body) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


@pytest.fixture
def ratings(tmp_path):
    folder = tmp_path / "ratings"
    # Task 2 and 4 put hybrid in list A; the others put it in list B.
    for i, prefs in enumerate(
        [["B", "A", "B", "A", "none"], ["B", "B", "B", "A"], ["B", "A", "A", "A", "B"]]
    ):
        write(folder, f"s{i}.json", make_session(i, prefs))
    return folder


def test_wilson_matches_known_values():
    interval = study.wilson(8, 10)
    assert interval.low == pytest.approx(0.4902, abs=1e-3)
    assert interval.high == pytest.approx(0.9433, abs=1e-3)
    assert study.wilson(0, 0).total == 0 and math.isnan(study.wilson(0, 0).low)
    assert 0 <= study.wilson(0, 5).low and study.wilson(5, 5).high == 1.0
    assert math.isnan(study.wilson(0, 0).rate)


def test_sign_test_is_exact_and_two_sided():
    assert study.sign_test_p(5, 5) == pytest.approx(2 / 32)
    assert study.sign_test_p(0, 5) == pytest.approx(2 / 32)
    assert study.sign_test_p(5, 10) == 1.0
    assert study.sign_test_p(9, 10) == pytest.approx(22 / 1024)
    assert study.sign_test_p(0, 0) == 1.0


def test_analysis_counts_preferences_by_ranker_not_position(ratings):
    result = study.analyse(study.load_sessions(ratings))
    pref = result.preference
    assert (pref.hybrid, pref.baseline, pref.none) == (11, 2, 1)
    assert result.sessions == 3 and result.tasks == 14 and result.ratings == 140
    assert pref.p_value == pytest.approx(study.sign_test_p(11, 13))
    assert result.thumbs_up["hybrid"].total == 70
    assert result.thumbs_up["hybrid"].rate > result.thumbs_up["baseline"].rate
    assert result.new_and_liked["hybrid"].count <= result.thumbs_up["hybrid"].count


def test_cli_writes_the_report_with_limits(ratings, tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["analyze-study", str(ratings), "--date", "2026-10-20"]) == 0
    report = tmp_path / "reports" / "listener-study-2026-10-20.md"
    assert str(report) in capsys.readouterr().out
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Listener study, 2026-10-20")
    for heading in ("## Overall preference", "## Track ratings", "## Limits"):
        assert heading in text
    assert "Two-sided sign test" in text and "p = " in text
    assert "| Hybrid |" in text and "| Baseline |" in text
    assert "pilot" in text
    for session in ("0" * 32, f"{1:032x}"):
        assert session not in text


def test_report_handles_no_decided_preference(tmp_path):
    folder = tmp_path / "r"
    write(folder, "a.json", make_session(1, ["none"] * 4))
    result = study.analyse(study.load_sessions(folder))
    text = study.render_report(result, date(2026, 10, 20))
    assert "No task had a decided preference" in text


def test_default_ratings_directory_is_ignored_by_git():
    assert (
        study.DEFAULT_RATINGS_DIR.as_posix() + "/"
        in (ROOT / ".gitignore").read_text().splitlines()
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda b: b.update(name="Ada"), "name"),
        (lambda b: b.__setitem__("session_id", "not-an-id"), "session_id"),
        (lambda b: b["tasks"].pop(), "tasks"),
        (lambda b: b["tasks"][0]["lists"][0]["results"][0].update(rating=0), "rating"),
        (lambda b: b["tasks"][0].update(notes="free text"), "notes"),
        (lambda b: b["tasks"][0].update(preferred="hybrid"), "preferred"),
        (lambda b: b["tasks"][0]["likes"].append("my email is a@b.c"), "likes"),
    ],
)
def test_files_that_fail_the_schema_are_rejected(tmp_path, capsys, change, message):
    body = make_session(1, ["A", "B", "A", "B"])
    change(body)
    folder = tmp_path / "r"
    write(folder, "bad.json", body)
    with pytest.raises(study.StudyError) as caught:
        study.load_sessions(folder)
    assert "bad.json does not match study-ratings.schema.json" in str(caught.value)
    assert message in str(caught.value)
    assert main(["analyze-study", str(folder), "--out-dir", str(tmp_path / "o")]) == 1
    assert "analyze-study: bad.json" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def test_inconsistent_or_unreadable_files_are_rejected(tmp_path):
    folder = tmp_path / "r"
    same = make_session(1, ["A"] * 4)
    for item in same["tasks"][0]["lists"]:
        item["ranker"] = "hybrid"
    write(folder, "same.json", same)
    with pytest.raises(study.StudyError, match="different rankers"):
        study.load_sessions(folder)
    (folder / "same.json").unlink()

    swapped = make_session(1, ["A"] * 4)
    swapped["tasks"][1]["lists"].reverse()
    write(folder, "swapped.json", swapped)
    with pytest.raises(study.StudyError, match="labelled A then B"):
        study.load_sessions(folder)
    (folder / "swapped.json").unlink()

    renumbered = make_session(1, ["A"] * 4)
    renumbered["tasks"][0]["task"] = 2
    write(folder, "renumbered.json", renumbered)
    with pytest.raises(study.StudyError, match="numbered 2"):
        study.load_sessions(folder)
    (folder / "renumbered.json").unlink()

    (folder / "broken.json").write_text("{nope", encoding="utf-8")
    with pytest.raises(study.StudyError, match="not readable JSON"):
        study.load_sessions(folder)


def test_duplicate_sessions_and_missing_directories_are_rejected(tmp_path):
    folder = tmp_path / "r"
    write(folder, "a.json", make_session(1, ["A"] * 4))
    write(folder, "b.json", make_session(1, ["B"] * 4))
    with pytest.raises(study.StudyError, match="repeats the session id of a.json"):
        study.load_sessions(folder)
    with pytest.raises(study.StudyError, match="not a directory"):
        study.load_sessions(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(study.StudyError, match="no ratings files"):
        study.load_sessions(empty)


def test_many_schema_problems_are_summarised(tmp_path):
    body = make_session(1, ["A"] * 4)
    for task in body["tasks"]:
        for item in task["lists"]:
            for result in item["results"]:
                result["rating"] = 0
    folder = tmp_path / "r"
    write(folder, "bad.json", body)
    with pytest.raises(study.StudyError, match=r"and \d+ more"):
        study.load_sessions(folder)


def test_cli_refuses_a_report_directory_outside_the_working_directory(
    ratings, tmp_path, capsys, monkeypatch
):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert main(["analyze-study", str(ratings), "--out-dir", str(tmp_path / "x")]) == 1
    assert "outside the working directory" in capsys.readouterr().err


def test_schema_allows_only_the_documented_fields():
    schema = study._schema()
    assert set(schema["properties"]) == TOP_LEVEL_FIELDS
    banned = {"name", "email", "ip", "ip_address", "comment", "notes", "user_agent"}

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert not banned & set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    task = schema["$defs"]["task"]["properties"]
    assert set(task) == {
        "task",
        "seed_track_id",
        "likes",
        "dislikes",
        "started_at",
        "completed_at",
        "lists",
        "preferred",
    }
    assert set(schema["$defs"]["result"]["properties"]) == {
        "track_id",
        "rating",
        "new_to_me",
    }


def test_the_protocol_document_carries_the_report_limits_and_plan():
    text = " ".join((ROOT / "docs" / "listener-study.md").read_text("utf-8").split())
    for heading in ("## Consent", "## Analysis plan", "## Limits", "## Data handling"):
        assert heading in text
    for line in study.LIMITS:
        assert line in text


def test_the_synthetic_fixture_is_valid():
    body = make_session(2, ["A", "B", "none", "A", "B", "A"])
    assert study.Draft202012Validator(study._schema()).is_valid(body)


def test_the_study_page_shows_the_consent_text_in_the_protocol():
    doc = (ROOT / "docs" / "listener-study.md").read_text("utf-8")
    quoted = " ".join(line[2:] for line in doc.splitlines() if line.startswith("> "))
    script = (ROOT / "src" / "musicdiscovery" / "demo" / "study-lib.js").read_text(
        "utf-8"
    )
    consent = re.search(r'CONSENT =\s*"(.*)";', script).group(1)
    assert consent == quoted
