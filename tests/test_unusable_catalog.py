"""A catalog file that loads but that rankers cannot use is reported, not raised."""

from pathlib import Path
from traceback import format_tb

import pandas as pd
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from pyarrow import ArrowException
from test_evaluation import make_frame
from test_ingest import _tracks_csv, _write_archive

from musicdiscovery import cli
from musicdiscovery.catalog import METADATA_COLUMNS, Catalog, CatalogError
from musicdiscovery.evaluation import (
    DEFAULT_QUERIES,
    generate_queries,
    write_queries,
)
from musicdiscovery.ingest import ingest

NAN_FIRST = [float("nan")]
INF_FIRST = [float("inf")]


def _with_feature(first):
    return lambda f: f.assign(mfcc_mean_00=first + [0.5] * (len(f) - 1))


DAMAGE = {
    "no listens": (lambda f: f.drop(columns="listens"), "listens"),
    "no genre ids": (lambda f: f.drop(columns="genre_ids"), "genre_ids"),
    "text feature": (lambda f: f.assign(notes="x"), "notes"),
    "nan feature": (_with_feature(NAN_FIRST), "mfcc_mean_00"),
    "inf feature": (_with_feature(INF_FIRST), "mfcc_mean_00"),
}
CHANGES = [change for change, _ in DAMAGE.values()]


def _write(change, name="data/catalog/catalog.parquet"):
    frame = make_frame()
    Path(name).parent.mkdir(parents=True, exist_ok=True)
    change(frame).to_parquet(name, index=False)
    return str(int(frame["track_id"].iloc[0]))


# The reproduction from the issue, as given.
@pytest.mark.parametrize(
    "change, args",
    [
        (lambda f: f.drop(columns="listens"), ["--ranker", "hybrid"]),
        (lambda f: f.drop(columns="genre_ids"), ["--ranker", "hybrid"]),
        (lambda f: f.assign(notes="x"), []),
        (
            lambda f: f.assign(mfcc_mean_00=[float("nan")] + [0.5] * (len(f) - 1)),
            [],
        ),
    ],
)
def test_unusable_catalog_is_reported(tmp_path, monkeypatch, change, args):
    monkeypatch.chdir(tmp_path)
    frame = make_frame()
    Path("data/catalog").mkdir(parents=True)
    change(frame).to_parquet("data/catalog/catalog.parquet", index=False)
    seed = str(int(frame["track_id"].iloc[0]))
    assert cli.main(["recommend", "--seed", seed, *args]) == 1


@pytest.mark.parametrize("case", DAMAGE)
@pytest.mark.parametrize(
    "command",
    ["recommend", "evaluate", "evaluate-generate", "export-demo"],
)
def test_every_command_reports_it_in_one_line(
    tmp_path, monkeypatch, capsys, case, command
):
    monkeypatch.chdir(tmp_path)
    change, named = DAMAGE[case]
    seed = _write(change)
    argv = {
        "recommend": ["recommend", "--seed", seed, "--ranker", "hybrid"],
        "evaluate": ["evaluate"],
        "evaluate-generate": ["evaluate", "--generate-queries"],
        "export-demo": ["export-demo", "out"],
    }[command]
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert captured.err.startswith(f"{argv[0]}: ")
    assert named in captured.err
    assert captured.err.endswith("; run `musicdiscovery ingest`\n")


@pytest.mark.parametrize("case", DAMAGE)
def test_ingest_rebuilds_such_a_file(tmp_path, case):
    path, sha1 = _write_archive(tmp_path / "source" / "fma.zip", _tracks_csv())
    data_dir = tmp_path / "data"
    assert not ingest(data_dir, url=path.as_uri(), sha1=sha1).skipped
    catalog_path = data_dir / "catalog" / "catalog.parquet"
    frame = pd.read_parquet(catalog_path)
    assert ingest(data_dir, url=path.as_uri(), sha1=sha1).skipped

    change, _ = DAMAGE[case]
    if "mfcc_mean_00" not in frame:
        frame["mfcc_mean_00"] = 1.0
    change(frame).to_parquet(catalog_path, index=False)

    assert not ingest(data_dir, url=path.as_uri(), sha1=sha1).skipped
    assert Catalog.load(catalog_path).track_ids.tolist() == [1, 2]


def test_ingest_drops_a_track_with_an_infinite_feature(tmp_path):
    """Otherwise ingest would publish a catalog that its own loader refuses."""
    path = tmp_path / "source" / "fma.zip"
    path, sha1 = _write_archive(path, _tracks_csv(), {1: "inf,2.5,3.5"})
    data_dir = tmp_path / "data"
    result = ingest(data_dir, url=path.as_uri(), sha1=sha1)
    assert result.kept == 1
    assert result.dropped_missing_features == 2
    catalog = Catalog.load(data_dir / "catalog" / "catalog.parquet")
    assert catalog.track_ids.tolist() == [2]


def test_a_small_in_memory_frame_is_not_validated():
    frame = pd.DataFrame(
        {
            "track_id": [1],
            "artist": ["a"],
            "genre_top": ["g"],
            "tags": [[]],
            "mfcc_mean_00": [float("nan")],
        }
    )
    assert len(Catalog(frame)) == 1


@pytest.mark.parametrize(
    "change, match",
    [
        (lambda f: f.drop(columns="artist"), "missing columns"),
        (lambda f: f.assign(track_id=1), "duplicate"),
        (lambda f: f[list(c for c in f if c in METADATA_COLUMNS)], "no feature"),
    ],
)
def test_load_tells_the_reader_to_ingest_for_every_refusal(tmp_path, change, match):
    path = tmp_path / "catalog.parquet"
    change(make_frame(per_genre=2)).to_parquet(path, index=False)
    with pytest.raises(CatalogError, match=f"{match}.*run `musicdiscovery ingest`"):
        Catalog.load(path)


@pytest.mark.parametrize(
    "column, values, named",
    [
        ("track_id", "text ids", "track_id"),
        ("listens", "text", "listens"),
        ("listens", "nan", "listens"),
        ("tags", "text", "tags"),
        ("tags", "null item", "tags"),
        ("genre_ids", "null cell", "genre_ids"),
        ("genre_ids", "text item", "genre_ids"),
    ],
)
def test_load_checks_the_metadata_rankers_read(tmp_path, column, values, named):
    frame = make_frame(per_genre=2)
    n = len(frame)
    frame[column] = {
        "text ids": [f"id{i}" for i in range(n)],
        "text": ["x"] * n,
        "nan": [float("nan")] * n,
        "null item": [["a", None]] * n,
        "null cell": [None] * n,
        "text item": [["x"]] * n,
    }[values]
    path = tmp_path / "catalog.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(CatalogError, match=f"{named}.*run `musicdiscovery ingest`"):
        Catalog.load(path)


def test_load_refuses_a_missing_nullable_track_id(tmp_path):
    frame = make_frame(per_genre=2)
    frame["track_id"] = frame["track_id"].astype("Int64")
    frame.iloc[0, frame.columns.get_loc("track_id")] = pd.NA
    path = tmp_path / "catalog.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(CatalogError, match="track_id.*run `musicdiscovery ingest`"):
        Catalog.load(path)


def test_many_bad_columns_are_counted_not_listed(tmp_path):
    frame = make_frame(per_genre=2)
    for i in range(7):
        frame[f"stray_{i}"] = "x"
    path = tmp_path / "catalog.parquet"
    frame.to_parquet(path, index=False)
    with pytest.raises(CatalogError, match="stray_4 and 2 more; run"):
        Catalog.load(path)


@pytest.mark.parametrize(
    "argv", [["evaluate", "--generate-profiles"], ["export-demo", "out"]]
)
def test_a_missing_axis_column_is_reported(tmp_path, monkeypatch, capsys, argv):
    """Axis columns stay optional at load: only the commands that need them refuse."""
    monkeypatch.chdir(tmp_path)
    _write(lambda f: f.drop(columns="rmse_mean_01"))
    assert cli.main(argv) == 1
    err = capsys.readouterr().err
    assert err == f"{argv[0]}: catalog has no column 'rmse_mean_01' for axis 'energy'\n"


# The class of bug: whatever a damaged file holds, a command either works or
# prints one line. It never raises. Each law below draws one kind of damage.
BASE_FRAME = make_frame(per_genre=70)  # large enough to generate queries from
UNDAMAGED = Catalog(BASE_FRAME)
QUERIES = generate_queries(UNDAMAGED, per_split=2)
COLUMNS = list(BASE_FRAME.columns)
JUNK = {
    "text": "x",
    "null": None,
    "number": 3.5,
    "infinite": float("inf"),
    "nan": float("nan"),
    "integer": 7,
    "flag": True,
    "empty list": [],
    "list of text": ["x"],
    "list of numbers": [1.5],
    "list of nulls": [None],
}


def _damage_column(frame, column, junk):
    frame = frame.copy()
    frame[column] = [junk] * len(frame)
    return frame


def _damage_cell(frame, column, junk):
    frame = frame.copy()
    values = list(frame[column])
    values[0] = junk
    frame[column] = values
    return frame


@st.composite
def damaged(draw):
    frame = BASE_FRAME
    for _ in range(draw(st.integers(1, 2))):
        how = draw(st.sampled_from(["drop", "column", "cell", "extra"]))
        column = draw(st.sampled_from([c for c in COLUMNS if c in frame]))
        junk = JUNK[draw(st.sampled_from(sorted(JUNK)))]
        if how == "drop":
            frame = frame.drop(columns=column)
        elif how == "column":
            frame = _damage_column(frame, column, junk)
        elif how == "cell":
            frame = _damage_cell(frame, column, junk)
        else:
            frame = _damage_column(frame, "stray", junk)
    return frame


COMMANDS = [
    ["recommend", "--seed", "100", "--ranker", "baseline"],
    ["recommend", "--seed", "100", "--ranker", "hybrid"],
    ["recommend", "--seed", "100", "--ranker", "profile"],
    ["recommend", "--seed", "100", "--like", "tag:loud", "--like", "more:energy"],
    ["evaluate", "--generate-queries"],
    ["evaluate", "--generate-profiles"],
    ["evaluate", "--ranker", "hybrid", "--ranker", "popular"],
    ["export-demo", "out"],
]


def _run(frame, argv, capsys):
    """Write `frame` and run `argv` in the working directory, as a user would."""
    target = Path("data/catalog")
    target.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(target / "catalog.parquet", index=False)
    if argv[:2] == ["evaluate", "--ranker"]:
        write_queries(DEFAULT_QUERIES, QUERIES, UNDAMAGED)
    code = cli.main(argv)
    return code, capsys.readouterr()


@pytest.mark.parametrize("argv", COMMANDS, ids=" ".join)
def test_an_undamaged_catalog_passes_every_command(tmp_path, monkeypatch, capsys, argv):
    """Without this, the law below would hold for a command that always refuses."""
    monkeypatch.chdir(tmp_path)
    code, captured = _run(BASE_FRAME, argv, capsys)
    assert code == 0, captured.err


@pytest.mark.parametrize("argv", COMMANDS, ids=" ".join)
@settings(
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(frame=damaged())
def test_a_damaged_catalog_never_raises(tmp_path, monkeypatch, capsys, argv, frame):
    monkeypatch.chdir(tmp_path)
    try:
        code, captured = _run(frame, argv, capsys)
    except (ValueError, TypeError, OverflowError, ArrowException) as error:
        # Some junk cannot be in a parquet file at all, so no such file can exist.
        assume("to_parquet" not in "".join(format_tb(error.__traceback__)))
        raise
    assert code in (0, 1)
    if code == 1:
        assert captured.err.count("\n") == 1, captured.err
