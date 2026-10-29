"""Pin the messages the JSON loaders and schema checks show today (#182).

Each loader reads a packaged file or a named one, parses it and wraps the failure in
its caller's error class; the schema checks describe each violation as
"<where>: <message>". Callers print these texts, so this file fixes them for a
missing file, bad JSON and a schema violation. It passes on the base commit, and the
refactor that shares one loader and one schema-error line must keep it passing.
"""

import json
from pathlib import Path

import pytest

from musicdiscovery import feedback, study
from musicdiscovery.hybrid import HybridConfig, HybridConfigError
from musicdiscovery.profile_ranker import ProfileModel, ProfileRankerError


@pytest.fixture(autouse=True)
def work_in_tmp(tmp_path, monkeypatch):
    """read_log only reads inside the working directory, so work where the logs are."""
    monkeypatch.chdir(tmp_path)


def event(**changes):
    """A valid feedback event, with the given fields replaced."""
    body = {
        "version": 1,
        "time": "2026-10-12T10:00:00Z",
        "ranker": "hybrid",
        "seed_track_ids": [120],
        "likes": ["genre:Folk"],
        "dislikes": ["more:energy"],
        "exclude_seed_artist": False,
        "shown": [{"track_id": 100 + i, "rank": i + 1} for i in range(4)],
        "track_id": 100,
        "rank": 1,
        "rating": 1,
        "explanation_terms": ["audio"],
    }
    return {**body, **changes}


def test_hybrid_config_missing_file_is_wrapped(tmp_path):
    with pytest.raises(HybridConfigError) as refused:
        HybridConfig.load(tmp_path / "missing.json")
    cause = refused.value.__cause__
    assert type(cause) is FileNotFoundError
    assert str(refused.value) == f"cannot read hybrid config: {cause}"


def test_hybrid_config_bad_json_is_wrapped(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(HybridConfigError) as refused:
        HybridConfig.load(path)
    assert str(refused.value) == (
        "cannot read hybrid config: Expecting value: line 1 column 1 (char 0)"
    )


def test_hybrid_config_unknown_field_is_wrapped(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"nope": 1}', encoding="utf-8")
    with pytest.raises(HybridConfigError) as refused:
        HybridConfig.load(path)
    assert str(refused.value) == (
        "cannot read hybrid config: "
        "HybridConfig.__init__() got an unexpected keyword argument 'nope'"
    )


def test_profile_model_missing_file_is_wrapped(tmp_path):
    with pytest.raises(ProfileRankerError) as refused:
        ProfileModel.load(tmp_path / "missing.json")
    cause = refused.value.__cause__
    assert type(cause) is FileNotFoundError
    assert str(refused.value) == f"cannot read profile model: {cause!r}"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        pytest.param(
            "not json",
            "cannot read profile model: "
            "JSONDecodeError('Expecting value: line 1 column 1 (char 0)')",
            id="bad-json",
        ),
        pytest.param(
            "{}",
            "cannot read profile model: KeyError('weights')",
            id="no-weights",
        ),
        pytest.param(
            "[]",
            "cannot read profile model: "
            "TypeError('list indices must be integers or slices, not str')",
            id="list-payload",
        ),
    ],
)
def test_profile_model_unreadable_payload_is_wrapped(tmp_path, text, message):
    path = tmp_path / "model.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ProfileRankerError) as refused:
        ProfileModel.load(path)
    assert str(refused.value) == message


def test_profile_model_wrong_features_is_refused_unwrapped(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"weights": {"audio": 1.0}}), encoding="utf-8")
    with pytest.raises(ProfileRankerError) as refused:
        ProfileModel.load(path)
    assert str(refused.value) == (
        "model weights must name exactly "
        "['audio', 'metadata', 'text', 'popularity'], got ['audio']"
    )


def test_read_log_missing_file_is_wrapped():
    path = Path("missing.jsonl")
    with pytest.raises(feedback.FeedbackError) as refused:
        feedback.read_log(path)
    cause = refused.value.__cause__
    assert type(cause) is FileNotFoundError
    assert str(refused.value) == f"cannot read {path} ({cause})"


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        pytest.param(
            "not json",
            "line 1: not JSON (Expecting value: line 1 column 1 (char 0))",
            id="bad-json",
        ),
        pytest.param(
            json.dumps(event(rating=5)),
            "line 1: rating: 5 is not one of [1, -1, 0]",
            id="enum-violation",
        ),
        pytest.param(
            json.dumps(event(track_id="x", rating="up")),
            "line 1: rating: 'up' is not one of [1, -1, 0]",
            id="first-by-path-wins",
        ),
        pytest.param(
            json.dumps({}),
            "line 1: top level: 'version' is a required property",
            id="missing-field",
        ),
        pytest.param(
            json.dumps(event(shown=[{"track_id": 100, "rank": 2}])),
            "line 1: shown ranks must run 1, 2, 3, ... in order",
            id="ranks-out-of-order",
        ),
    ],
)
def test_read_log_describes_a_bad_line(line, problem):
    Path("log.jsonl").write_text(line + "\n", encoding="utf-8")
    assert feedback.read_log(Path("log.jsonl")).problems == [problem]


def test_read_log_cuts_a_long_schema_message_to_120_characters():
    Path("log.jsonl").write_text(
        json.dumps(event(rating="a" * 200)) + "\n", encoding="utf-8"
    )
    message = f"{'a' * 200!r} is not one of [1, -1, 0]"
    (problem,) = feedback.read_log(Path("log.jsonl")).problems
    assert problem == f"line 1: rating: {message[:120]}"


def test_load_sessions_missing_directory_is_refused(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(study.StudyError) as refused:
        study.load_sessions(missing)
    assert str(refused.value) == f"{missing} is not a directory"


def test_load_sessions_bad_json_is_refused_by_name(tmp_path):
    folder = tmp_path / "ratings"
    folder.mkdir()
    (folder / "bad.json").write_text("not json", encoding="utf-8")
    with pytest.raises(study.StudyError) as refused:
        study.load_sessions(folder)
    assert str(refused.value) == (
        "bad.json: not readable JSON (Expecting value: line 1 column 1 (char 0))"
    )


def test_load_sessions_schema_violation_lists_five_problems_then_a_count(tmp_path):
    folder = tmp_path / "ratings"
    folder.mkdir()
    (folder / "bad.json").write_text("{}", encoding="utf-8")
    with pytest.raises(study.StudyError) as refused:
        study.load_sessions(folder)
    assert str(refused.value) == (
        "bad.json does not match study-ratings.schema.json:\n"
        "  top level: 'format' is a required property\n"
        "  top level: 'version' is a required property\n"
        "  top level: 'session_id' is a required property\n"
        "  top level: 'started_at' is a required property\n"
        "  top level: 'exported_at' is a required property\n"
        "  and 1 more"
    )
