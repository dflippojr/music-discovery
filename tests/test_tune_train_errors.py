"""tune and train report bad evaluate-format input in one line, as evaluate does (#211).

The class of bug: a command reads the same files as `evaluate` but catches fewer
errors, so input that `evaluate` reports in one line crashes it with a traceback.
The properties below edit valid files at random and run the command on each edit.
"""

import contextlib
import copy
import io
import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_evaluation import make_frame

from musicdiscovery import profile_training, tuning
from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation import (
    EvaluationError,
    generate_queries,
    load_profiles,
    load_queries,
    write_profiles,
    write_queries,
)
from musicdiscovery.preference import PreferenceError
from musicdiscovery.profiles import generate_profiles

# One config, so that a query file that loads does not run the whole default grid.
ONE_CONFIG = {
    "metadata_weight": (0.5,),
    "popularity_weight": (0.05,),
    "mmr_lambda": (0.2,),
    "genre_redundancy": (0.25,),
}
# Each of these parses as JSON (Python's reader takes NaN and Infinity) and none is
# what the fields expect.
ODD_VALUES = st.sampled_from(
    [
        None,
        True,
        0,
        -1,
        999_999,
        0.01,
        float("inf"),
        float("nan"),
        "",
        "nonsense",
        "genre:nonsense",
        [],
        [[]],
        {},
    ]
)
QUERY_FIELDS = st.sampled_from(
    [("id",), ("split",), ("seed",), ("likes",), ("likes", 0)]
)
PROFILE_FIELDS = st.sampled_from(
    [
        ("id",),
        ("split",),
        ("taste",),
        ("taste", "genre"),
        ("taste", "tags"),
        ("taste", "axis"),
        ("history",),
        ("history", 0),
        ("history", 0, 0),
        ("history", 0, 1),
        ("heldout",),
        ("heldout", 0),
        ("heldout", 0, 0),
        ("heldout", 0, 1),
    ]
)
RECORDS = st.integers(0, 29)


def write_catalog() -> Catalog:
    frame = make_frame()
    Path("data/catalog").mkdir(parents=True)
    frame.to_parquet("data/catalog/catalog.parquet", index=False)
    return Catalog(frame)


def test_tune_reports_unknown_seed(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    catalog = write_catalog()
    write_queries(Path("q.json"), generate_queries(catalog, per_split=5), catalog)
    payload = json.loads(Path("q.json").read_text())
    payload["queries"][0]["seed"] = 999_999
    Path("q.json").write_text(json.dumps(payload))
    assert tuning.main(["--queries", "q.json"]) == 1
    assert capsys.readouterr().err == "tune: unknown track ids: [999999]\n"


def test_train_reports_profile_without_history(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    catalog = write_catalog()
    write_profiles(Path("p.json"), generate_profiles(catalog, per_split=5), catalog)
    payload = json.loads(Path("p.json").read_text())
    for pair in payload["profiles"][0]["history"]:
        pair[1] = 0.01
    Path("p.json").write_text(json.dumps(payload))
    assert profile_training.main(["--profiles", "p.json"]) == 1
    assert capsys.readouterr().err == "train: profile 0 has no history above 0.3\n"


def test_train_reports_a_preference_error(tmp_path, monkeypatch, capsys):
    # The file checks stop this reaching train today, so raise it where train would.
    monkeypatch.chdir(tmp_path)
    catalog = write_catalog()
    write_profiles(Path("p.json"), generate_profiles(catalog, per_split=5), catalog)

    def refuse(*_):
        raise PreferenceError("unknown track ids: [999999]")

    monkeypatch.setattr(profile_training, "train", refuse)
    assert profile_training.main(["--profiles", "p.json"]) == 1
    assert capsys.readouterr().err == "train: unknown track ids: [999999]\n"


@pytest.mark.parametrize(
    ("load", "records", "write"),
    [
        (load_queries, "queries", "q.json"),
        (load_profiles, "profiles", "p.json"),
    ],
)
def test_loaders_report_an_infinite_id(tmp_path, monkeypatch, load, records, write):
    monkeypatch.chdir(tmp_path)
    catalog = write_catalog()
    if records == "queries":
        write_queries(Path(write), generate_queries(catalog, per_split=5), catalog)
    else:
        write_profiles(Path(write), generate_profiles(catalog, per_split=5), catalog)
    payload = json.loads(Path(write).read_text())
    payload[records][0]["id"] = float("inf")
    Path(write).write_text(json.dumps(payload))
    with pytest.raises(EvaluationError, match=f"cannot read {records}"):
        load(Path(write), catalog)


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    """A catalog with valid query and profile files, for edits to start from."""
    with pytest.MonkeyPatch.context() as patch:
        patch.chdir(tmp_path_factory.mktemp("errors"))
        catalog = write_catalog()
        write_queries(Path("q.json"), generate_queries(catalog, per_split=5), catalog)
        write_profiles(Path("p.json"), generate_profiles(catalog, per_split=5), catalog)
        real_tune = tuning.tune
        patch.setattr(tuning, "tune", lambda c, q: real_tune(c, q, ONE_CONFIG))
        yield


def edited(source: str, records: str, index: int, field: tuple, value) -> None:
    """Write edited.json: `source` with one field of one record set to `value`."""
    payload = copy.deepcopy(json.loads(Path(source).read_text()))
    target = payload[records][index % len(payload[records])]
    for step in field[:-1]:
        target = target[step]
    if isinstance(target, list) and not target:
        target.append(value)
    else:
        target[field[-1]] = value
    Path("edited.json").write_text(json.dumps(payload))


def assert_one_line_or_success(name: str, command, args: list[str]) -> None:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        status = command(args)
    text = err.getvalue()
    assert status in (0, 1)
    if status == 1:
        assert text.startswith(f"{name}: ") and text.count("\n") == 1, text


@given(index=RECORDS, field=QUERY_FIELDS, value=ODD_VALUES)
def test_tune_reports_any_edited_query_in_one_line(files, index, field, value):
    edited("q.json", "queries", index, field, value)
    assert_one_line_or_success("tune", tuning.main, ["--queries", "edited.json"])


@given(index=RECORDS, field=PROFILE_FIELDS, value=ODD_VALUES)
def test_train_reports_any_edited_profile_in_one_line(files, index, field, value):
    edited("p.json", "profiles", index, field, value)
    assert_one_line_or_success(
        "train", profile_training.main, ["--profiles", "edited.json"]
    )
