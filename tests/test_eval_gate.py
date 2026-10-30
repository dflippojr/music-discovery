"""The evaluation gate's comparison logic, on synthetic numbers (#105).

No catalog is downloaded: the job in ci.yml runs the real evaluation, and these
tests feed `compare` reports shaped like `musicdiscovery evaluate`'s JSON.
"""

import contextlib
import copy
import io
import json
import runpy
import sys
import tempfile
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from musicdiscovery import eval_gate
from musicdiscovery.eval_gate import GateError, compare, main, render

ROOT = Path(__file__).resolve().parents[1]
SPLITS = ("validation", "test")
METRICS = ("genre_ndcg", "tag_overlap")


def _cell(mean, spread=0.04):
    return {"mean": mean, "lo": round(mean - spread, 6), "hi": round(mean + spread, 6)}


def make_report(scores):
    """A report with `scores[ranker][metric] = mean` on both splits."""
    return {
        "catalog": {"source_sha1": "abc", "tracks": 7995},
        "queries_sha256": "q1",
        "k": 10,
        "rankers": {
            ranker: {
                split: {metric: _cell(mean) for metric, mean in metrics.items()}
                for split in SPLITS
            }
            for ranker, metrics in scores.items()
        },
    }


SCORES = {
    "hybrid": {"genre_ndcg": 0.99, "tag_overlap": 0.25},
    "profile": {"genre_ndcg": 0.98, "tag_overlap": 0.60},
    "random": {"genre_ndcg": 0.12, "tag_overlap": 0.01},
}


@pytest.fixture
def baseline():
    return make_report(SCORES)


def run_with(baseline, **changes):
    """A copy of the baseline with `ranker.metric` point estimates replaced."""
    run = copy.deepcopy(baseline)
    for key, value in changes.items():
        ranker, metric = key.split("__")
        run["rankers"][ranker]["validation"][metric]["mean"] = value
    return run


def failures(checks):
    return {(c.ranker, c.metric): c.problem for c in checks if not c.passed}


def test_a_run_inside_the_intervals_passes(baseline):
    run = run_with(baseline, hybrid__genre_ndcg=0.97, profile__tag_overlap=0.58)
    checks = compare(baseline, run)
    assert len(checks) == 4
    assert failures(checks) == {}


def test_the_baseline_itself_passes(baseline):
    assert failures(compare(baseline, copy.deepcopy(baseline))) == {}


def test_a_drop_below_the_baseline_lower_bound_fails(baseline):
    run = run_with(baseline, hybrid__genre_ndcg=0.90)
    assert failures(compare(baseline, run)) == {
        ("hybrid", "genre_ndcg"): "below the baseline's lower bound"
    }


def test_a_ranker_that_falls_to_random_fails(baseline):
    # #86: a ranker answered a query with the wrong tracks (the lowest ids)
    # and the report carried the polluted number. Here hybrid's tag overlap
    # collapses to random's level; both rules catch it.
    run = run_with(baseline, hybrid__tag_overlap=0.01)
    checks = compare(baseline, run)
    assert failures(checks) == {
        ("hybrid", "tag_overlap"): "below the baseline's lower bound"
    }


def test_below_random_fails_even_when_the_baseline_bound_is_low(baseline):
    # A baseline whose lower bound sits under random's upper bound cannot hide
    # a ranker that is no better than random.
    baseline["rankers"]["profile"]["validation"]["tag_overlap"]["lo"] = 0.0
    run = run_with(baseline, profile__tag_overlap=0.03)
    assert failures(compare(baseline, run)) == {
        ("profile", "tag_overlap"): "below random's upper bound"
    }


@pytest.mark.parametrize("undefined", [None, float("nan")])
def test_an_undefined_score_fails(baseline, undefined):
    run = run_with(baseline, profile__genre_ndcg=undefined)
    assert failures(compare(baseline, run)) == {
        ("profile", "genre_ndcg"): "no defined score in the run"
    }


def test_a_missing_gated_ranker_fails(baseline):
    run = copy.deepcopy(baseline)
    del run["rankers"]["hybrid"]
    assert set(failures(compare(baseline, run))) == {
        ("hybrid", "genre_ndcg"),
        ("hybrid", "tag_overlap"),
    }


def test_test_split_numbers_are_never_gated(baseline):
    run = copy.deepcopy(baseline)
    for ranker in ("hybrid", "profile"):
        for metric in METRICS:
            run["rankers"][ranker]["test"][metric]["mean"] = 0.0
    assert failures(compare(baseline, run)) == {}


def test_an_ungated_ranker_is_ignored(baseline):
    baseline["rankers"]["audio-embedding"] = baseline["rankers"]["random"]
    run = copy.deepcopy(baseline)
    run["rankers"]["audio-embedding"]["validation"]["genre_ndcg"]["mean"] = 0.0
    assert failures(compare(baseline, run)) == {}


@pytest.mark.parametrize(
    "path, value",
    [
        (("catalog", "source_sha1"), "other"),
        (("catalog", "tracks"), 7000),
        (("queries_sha256",), "q2"),
    ],
)
def test_a_run_on_another_catalog_or_queries_is_not_comparable(baseline, path, value):
    run = copy.deepcopy(baseline)
    target = run
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(GateError, match="not comparable"):
        compare(baseline, run)


@pytest.mark.parametrize("which", ["run", "baseline"])
@pytest.mark.parametrize("cutoff", [1, 5, 20, "10", None])
def test_a_report_with_another_cutoff_is_not_comparable(baseline, which, cutoff):
    # The metrics are labelled @10; a k=1 run must not pass against them.
    run = copy.deepcopy(baseline)
    target = run if which == "run" else baseline
    target["k"] = cutoff
    with pytest.raises(GateError, match="cutoff"):
        compare(baseline, run)


@pytest.mark.parametrize("which", ["run", "baseline"])
def test_a_report_without_a_cutoff_is_not_comparable(baseline, which):
    run = copy.deepcopy(baseline)
    del (run if which == "run" else baseline)["k"]
    with pytest.raises(GateError, match="cutoff"):
        compare(baseline, run)


def test_a_baseline_without_a_gated_ranker_is_an_error(baseline):
    del baseline["rankers"]["hybrid"]
    with pytest.raises(GateError, match="no validation genre_ndcg for hybrid"):
        compare(baseline, make_report(SCORES))


def _damage(split, metric, key, how):
    """Break the ``key`` entry of ``split[metric]`` the way a bad report might."""
    if how == "not a mapping":
        split[metric] = [0.0]
    elif how == "missing":
        del split[metric][key]
    else:
        split[metric][key] = how


# A bound that is null, absent, NaN, or whose cell is not a mapping (#163).
BAD_BOUNDS = [None, "missing", float("nan"), "not a mapping"]
BAD_BOUND_IDS = ["null", "missing", "nan", "not-a-mapping"]


@pytest.mark.parametrize("how", BAD_BOUNDS, ids=BAD_BOUND_IDS)
def test_a_baseline_without_an_interval_is_an_error(checkout, baseline, capsys, how):
    _damage(baseline["rankers"]["hybrid"]["validation"], "genre_ndcg", "lo", how)
    message = "baseline has no interval for hybrid/genre_ndcg"
    with pytest.raises(GateError, match=message):
        compare(baseline, make_report(SCORES))
    assert main(setup_files(checkout, baseline, make_report(SCORES))) == 1
    captured = capsys.readouterr()
    assert f"eval-gate: {message}" in captured.err
    assert f"**FAIL**: {message}" in captured.out


@pytest.mark.parametrize("how", BAD_BOUNDS, ids=BAD_BOUND_IDS)
def test_a_run_without_a_random_interval_is_an_error(checkout, baseline, capsys, how):
    run = make_report(SCORES)
    _damage(run["rankers"]["random"]["validation"], "tag_overlap", "hi", how)
    message = "run has no interval for random/tag_overlap"
    with pytest.raises(GateError, match=message):
        compare(baseline, run)
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert f"eval-gate: {message}" in captured.err
    assert f"**FAIL**: {message}" in captured.out


@pytest.mark.parametrize("how", ["missing", "not a mapping"])
def test_a_run_cell_without_a_mean_fails_its_check(checkout, baseline, capsys, how):
    # #170: the mean is read from the cell, so a cell without one is a failing
    # check, not a KeyError or TypeError that the gate would print as a traceback.
    run = make_report(SCORES)
    _damage(run["rankers"]["hybrid"]["validation"], "genre_ndcg", "mean", how)
    checks = compare(baseline, run)
    bad = [c for c in checks if not c.passed]
    assert [(c.ranker, c.metric) for c in bad] == [("hybrid", "genre_ndcg")]
    assert "no defined score" in bad[0].problem
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "**FAIL**: no defined score in the run" in captured.out


@pytest.mark.parametrize("how", ["0.5", [0.5]], ids=["string", "list"])
def test_a_run_cell_with_a_non_numeric_mean_fails_its_check(
    checkout, baseline, capsys, how
):
    # #175: a mean that is not a number is an undefined score. Formatting the raw
    # value in render() raised ValueError, so the gate printed no table at all.
    run = make_report(SCORES)
    _damage(run["rankers"]["hybrid"]["validation"], "genre_ndcg", "mean", how)
    checks = compare(baseline, run)
    bad = [c for c in checks if not c.passed]
    assert [(c.ranker, c.metric) for c in bad] == [("hybrid", "genre_ndcg")]
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert "| hybrid | same-genre nDCG@10 | undefined |" in captured.out
    assert "**FAIL**: no defined score in the run" in captured.out


def test_a_non_numeric_baseline_bound_fails_its_check_in_a_row(
    checkout, baseline, capsys
):
    # #175: a string bound is not missing, so it is not a GateError (#163). Its
    # check fails, and render() shows the bound as undefined in that check's row.
    _damage(baseline["rankers"]["hybrid"]["validation"], "genre_ndcg", "lo", "0.5")
    run = make_report(SCORES)
    assert failures(compare(baseline, run)) == {
        ("hybrid", "genre_ndcg"): "the baseline's lower bound is not a number"
    }
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert (
        "| hybrid | same-genre nDCG@10 | 0.990 | undefined | 0.160 "
        "| **FAIL**: the baseline's lower bound is not a number |"
    ) in captured.out


def test_a_non_numeric_random_bound_fails_its_checks_in_a_row(
    checkout, baseline, capsys
):
    # #175: the same for random's upper bound, which every metric's check uses.
    run = make_report(SCORES)
    _damage(run["rankers"]["random"]["validation"], "tag_overlap", "hi", "0.5")
    assert failures(compare(baseline, run)) == {
        ("hybrid", "tag_overlap"): "random's upper bound is not a number",
        ("profile", "tag_overlap"): "random's upper bound is not a number",
    }
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert "Traceback" not in captured.err
    assert (
        "| hybrid | tag overlap@10 | 0.250 | 0.210 | undefined "
        "| **FAIL**: random's upper bound is not a number |"
    ) in captured.out


def test_main_exits_one_when_the_cutoffs_differ(checkout, baseline, capsys):
    run = copy.deepcopy(baseline)
    run["k"] = 1
    assert main(setup_files(checkout, baseline, run)) == 1
    assert "cutoff" in capsys.readouterr().err


def test_render_marks_each_row(baseline):
    run = run_with(baseline, hybrid__genre_ndcg=0.90)
    table = render(compare(baseline, run))
    assert "| hybrid | same-genre nDCG@10 | 0.900 | 0.950 | 0.160 | **FAIL**" in table
    assert "| profile | tag overlap@10 | 0.600 | 0.560 | 0.050 | pass |" in table
    assert "Verdict: 1 of 4 checks failed." in table
    assert "Verdict: all pass." in render(compare(baseline, baseline))
    undefined = render(compare(baseline, run_with(baseline, hybrid__genre_ndcg=None)))
    assert "| undefined |" in undefined


def write(path, report):
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def setup_files(tmp_path, baseline, run):
    write(tmp_path / "offline-base.json", baseline)
    pointer = write(tmp_path / "baseline.json", {"report": "offline-base.json"})
    return [str(pointer), str(write(tmp_path / "run.json", run))]


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A working directory to run the gate from, with a sibling outside it."""
    inside = tmp_path / "checkout"
    inside.mkdir()
    monkeypatch.chdir(inside)
    return inside


@pytest.mark.parametrize("how", ["dotdot", "absolute"])
@pytest.mark.parametrize("which", [0, 1])
def test_main_refuses_an_argument_outside_the_working_directory(
    checkout, baseline, capsys, how, which
):
    args = setup_files(checkout, baseline, baseline)
    outside = checkout.parent / Path(args[which]).name
    outside.write_bytes(Path(args[which]).read_bytes())
    args[which] = str(Path("..") / outside.name if how == "dotdot" else outside)

    assert main(args) == 1

    captured = capsys.readouterr()
    assert "outside the working directory" in captured.err
    assert "**FAIL**" in captured.out
    assert "Verdict" not in captured.out


@pytest.mark.parametrize("how", ["dotdot", "absolute"])
def test_the_baseline_report_must_sit_next_to_its_pointer(checkout, baseline, how):
    reports = checkout / "reports"
    reports.mkdir()
    outside = write(checkout / "offline-base.json", baseline)
    target = "../offline-base.json" if how == "dotdot" else str(outside)
    pointer = write(reports / "baseline.json", {"report": target})

    with pytest.raises(GateError, match="outside"):
        eval_gate.load_baseline(pointer)
    assert main([str(pointer), str(write(checkout / "run.json", baseline))]) == 1


def test_main_accepts_relative_paths_inside_the_working_directory(
    checkout, baseline, capsys
):
    (checkout / "reports").mkdir()
    (checkout / ".eval-run").mkdir()
    write(checkout / "reports" / "offline-base.json", baseline)
    write(checkout / "reports" / "baseline.json", {"report": "offline-base.json"})
    write(checkout / ".eval-run" / "offline-run.json", baseline)

    assert (
        main(["reports/baseline.json", ".eval-run/../.eval-run/offline-run.json"]) == 0
    )
    assert "Verdict: all pass." in capsys.readouterr().out


def test_main_exits_zero_on_a_pass_and_prints_the_table(checkout, baseline, capsys):
    assert main(setup_files(checkout, baseline, baseline)) == 0
    assert "Verdict: all pass." in capsys.readouterr().out


def test_main_exits_one_on_a_regression(checkout, baseline, capsys):
    run = run_with(baseline, profile__genre_ndcg=0.5)
    assert main(setup_files(checkout, baseline, run)) == 1
    assert "**FAIL**" in capsys.readouterr().out


def test_main_exits_one_when_runs_are_not_comparable(checkout, baseline, capsys):
    run = copy.deepcopy(baseline)
    run["queries_sha256"] = "q2"
    assert main(setup_files(checkout, baseline, run)) == 1
    captured = capsys.readouterr()
    assert "not comparable" in captured.err
    assert "**FAIL**: the run and the baseline differ" in captured.out


def test_main_reports_unreadable_inputs(checkout, baseline, capsys):
    args = setup_files(checkout, baseline, baseline)
    (checkout / "run.json").write_text("{", encoding="utf-8")
    assert main(args) == 1
    assert "cannot read" in capsys.readouterr().err
    write(checkout / "baseline.json", {"nope": 1})
    assert main(args) == 1
    assert "no 'report' file name" in capsys.readouterr().err


def test_the_module_runs_as_a_script(checkout, baseline, monkeypatch):
    monkeypatch.setattr(
        "sys.argv", ["eval_gate", *setup_files(checkout, baseline, baseline)]
    )
    monkeypatch.delitem(sys.modules, "musicdiscovery.eval_gate")
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("musicdiscovery.eval_gate", run_name="__main__")
    assert exit_info.value.code == 0


def test_the_committed_baseline_points_at_a_report_that_passes_itself():
    # Catches a pointer to a missing file, and a report the rules cannot read.
    pointer = ROOT / "reports" / "baseline.json"
    report = eval_gate.load_baseline(pointer)
    assert failures(compare(report, report)) == {}


def gate_files(mutate):
    base = json.loads(
        (ROOT / "reports/offline-2026-10-22.json").read_text(encoding="utf-8")
    )
    Path("reports").mkdir()
    Path("reports/b.json").write_text(json.dumps(base), encoding="utf-8")
    Path("reports/baseline.json").write_text('{"report": "b.json"}', encoding="utf-8")
    Path("run.json").write_text(json.dumps(mutate(base)), encoding="utf-8")


@pytest.mark.parametrize("rankers", [None, []])
def test_gate_reports_malformed_rankers(tmp_path, monkeypatch, capsys, rankers):
    # #209: _cell caught only KeyError, so these were TypeError tracebacks.
    monkeypatch.chdir(tmp_path)
    gate_files(lambda r: {**r, "rankers": rankers})
    assert eval_gate.main(["reports/baseline.json", "run.json"]) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "eval-gate: no validation" in captured.err


def test_gate_reports_non_object_pointer(tmp_path, monkeypatch, capsys):
    # #209: load_baseline called .get on a JSON list, an AttributeError traceback.
    monkeypatch.chdir(tmp_path)
    gate_files(lambda r: r)
    Path("reports/baseline.json").write_text("[1]", encoding="utf-8")
    assert eval_gate.main(["reports/baseline.json", "run.json"]) == 1
    captured = capsys.readouterr()
    assert "FAIL" in captured.out
    assert "no 'report' file name" in captured.err


def gate_output(args):
    """main's exit code and output, on streams that refuse anything but ASCII.

    A console's encoding can be narrower than UTF-8, and a lone surrogate from a
    JSON string cannot be written to any of them.
    """
    streams = [io.TextIOWrapper(io.BytesIO(), encoding="ascii") for _ in range(2)]
    with contextlib.redirect_stdout(streams[0]), contextlib.redirect_stderr(streams[1]):
        code = main(args)
    return code, streams[0], streams[1]


def written(stream):
    stream.flush()
    return stream.buffer.getvalue().decode("ascii")


def assert_reports_a_reason(code, out, err):
    assert code in (0, 1)
    if code == 1:
        assert "**FAIL**" in written(out)
    if "**FAIL**" in written(out) and "Verdict" not in written(out):
        assert written(err).startswith("eval-gate: ")


def paths_in(value, prefix=()):
    """The path of ``value`` and of everything inside it."""
    yield prefix
    if isinstance(value, dict):
        for key, inner in value.items():
            yield from paths_in(inner, (*prefix, key))


def replaced(document, path, value):
    """A copy of ``document`` with ``value`` at ``path``."""
    if not path:
        return value
    result = copy.deepcopy(document)
    target = result
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return result


GOOD_REPORT = make_report(SCORES)
GOOD_POINTER = {"report": "offline-base.json"}
# JSON loads each of these, and each one has broken the gate or could. The NUL and
# the lone surrogates are in a file name a pointer can hold (#209).
AWKWARD_JSON = st.sampled_from(
    [10**400, -(10**400), "a\x00b", "\ud800", "\udc80", float("inf"), float("nan")]
)
ANY_JSON = st.recursive(
    AWKWARD_JSON | st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=6,
)


@given(
    which=st.sampled_from(["baseline", "run"]),
    path=st.sampled_from(list(paths_in(GOOD_REPORT))),
    value=ANY_JSON,
)
def test_any_json_anywhere_in_a_report_is_a_verdict_not_a_traceback(which, path, value):
    # The class behind #163, #170, #175 and #209: whatever a report holds, the gate
    # ends in a verdict or a FAIL line. Any exception from main fails this test.
    bad = replaced(GOOD_REPORT, path, value)
    baseline, run = (bad, GOOD_REPORT) if which == "baseline" else (GOOD_REPORT, bad)
    with tempfile.TemporaryDirectory() as folder, pytest.MonkeyPatch.context() as patch:
        patch.chdir(folder)
        assert_reports_a_reason(*gate_output(setup_files(Path(folder), baseline, run)))


@given(path=st.sampled_from(list(paths_in(GOOD_POINTER))), value=ANY_JSON)
def test_any_json_in_the_pointer_is_a_fail_line_not_a_traceback(path, value):
    with tempfile.TemporaryDirectory() as folder, pytest.MonkeyPatch.context() as patch:
        patch.chdir(folder)
        args = setup_files(Path(folder), GOOD_REPORT, GOOD_REPORT)
        write(Path(args[0]), replaced(GOOD_POINTER, path, value))
        code, out, err = gate_output(args)
        assert code == 1
        assert_reports_a_reason(code, out, err)


@pytest.mark.parametrize("huge", [10**400, -(10**400)], ids=["positive", "negative"])
def test_an_integer_too_large_for_a_float_is_an_undefined_score(
    checkout, baseline, huge
):
    run = run_with(baseline, hybrid__genre_ndcg=huge)
    assert failures(compare(baseline, run)) == {
        ("hybrid", "genre_ndcg"): "no defined score in the run"
    }
    code, out, _ = gate_output(setup_files(checkout, baseline, run))
    assert code == 1
    assert "| hybrid | same-genre nDCG@10 | undefined |" in written(out)


def test_an_integer_too_large_for_a_float_fails_a_bound_check(baseline):
    baseline["rankers"]["hybrid"]["validation"]["genre_ndcg"]["lo"] = 10**400
    assert failures(compare(baseline, make_report(SCORES))) == {
        ("hybrid", "genre_ndcg"): "the baseline's lower bound is not a number"
    }


@pytest.mark.parametrize(
    "name",
    ["a\x00b", "\ud800", "\udc80", "caf\xe9"],
    ids=["nul", "lone", "bytes", "latin"],
)
def test_a_pointer_to_an_impossible_file_name_is_a_fail_line(checkout, baseline, name):
    args = setup_files(checkout, baseline, baseline)
    write(Path(args[0]), {"report": name})
    code, out, err = gate_output(args)
    assert code == 1
    assert "**FAIL**" in written(out)
    assert written(err).startswith("eval-gate: ")


@pytest.mark.parametrize("depth", [6_000, 200_000])
def test_a_report_nested_too_deep_is_a_fail_line(checkout, baseline, depth):
    # json.loads refuses the deepest with RecursionError; a shallower one loads but
    # can still overflow the comparison or the repr in the message.
    args = setup_files(checkout, baseline, baseline)
    Path(args[1]).write_text(
        json.dumps({**baseline, "queries_sha256": "@"}).replace(
            '"@"', "[" * depth + "]" * depth
        ),
        encoding="utf-8",
    )
    code, out, err = gate_output(args)
    assert code == 1
    assert "**FAIL**" in written(out)
    assert written(err).startswith("eval-gate: ")
