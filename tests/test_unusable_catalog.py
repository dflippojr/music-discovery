"""A catalog file that loads but that rankers cannot use is reported, not raised."""

from pathlib import Path

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_evaluation import make_frame
from test_ingest import _tracks_csv, _write_archive

from musicdiscovery import cli
from musicdiscovery.catalog import METADATA_COLUMNS, Catalog, CatalogError
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
        "export-demo": ["export-demo"],
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


# The class of bug: whatever a damaged file holds, a command either works or
# prints one line. It never raises. Each law below draws one kind of damage.
COLUMNS = list(make_frame(per_genre=1).columns)
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
    frame = make_frame(per_genre=8)
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


def _write_or_none(frame, path):
    """Some junk cannot be written to parquet at all, so no such file can exist."""
    try:
        frame.to_parquet(path, index=False)
    except (ValueError, TypeError, OverflowError) as error:
        return error
    except Exception as error:  # pyarrow's own errors
        if "pyarrow" in type(error).__module__:
            return error
        raise
    return None


@pytest.mark.parametrize(
    "argv",
    [
        ["recommend", "--seed", "100", "--ranker", "baseline"],
        ["recommend", "--seed", "100", "--ranker", "hybrid"],
        ["recommend", "--seed", "100", "--ranker", "profile"],
        ["recommend", "--seed", "100", "--ranker", "baseline", "--like", "tag:x"],
        ["evaluate", "--generate-queries"],
        ["evaluate", "--generate-profiles"],
        ["export-demo"],
    ],
    ids=" ".join,
)
@settings(
    max_examples=40,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(frame=damaged())
def test_a_damaged_catalog_never_raises(tmp_path, monkeypatch, capsys, argv, frame):
    monkeypatch.chdir(tmp_path)
    target = Path("data/catalog")
    target.mkdir(parents=True, exist_ok=True)
    if _write_or_none(frame, target / "catalog.parquet"):
        return
    code = cli.main(argv)
    captured = capsys.readouterr()
    assert code in (0, 1)
    if code == 1:
        assert captured.err.count("\n") == 1, captured.err
