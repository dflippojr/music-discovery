"""The working-directory check every module calls as `paths.confined` (#255).

Characterizes what callers see: the resolved path for a path inside the working
directory, and the exact exception type and message for one outside it, with
each error class a caller passes.
"""

from functools import partial
from pathlib import Path

import pytest

from musicdiscovery import paths
from musicdiscovery.audio_embedding import AudioEmbeddingError
from musicdiscovery.demo import DemoError
from musicdiscovery.evaluation import EvaluationError
from musicdiscovery.feedback import FeedbackError
from musicdiscovery.study import StudyError

ERRORS = [AudioEmbeddingError, DemoError, EvaluationError, FeedbackError, StudyError]
CHECKS = [
    pytest.param(partial(paths.confined, error=error), error, id=error.__name__)
    for error in ERRORS
]


@pytest.fixture
def work(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.mark.parametrize(("check", "error"), CHECKS)
def test_a_relative_path_inside_resolves_under_the_working_directory(
    work, check, error
):
    assert check(Path("out") / "run.json") == (work / "out" / "run.json").resolve()


@pytest.mark.parametrize(("check", "error"), CHECKS)
def test_an_absolute_path_inside_resolves_to_itself(work, check, error):
    inside = work / "run.json"
    assert check(inside) == inside.resolve()


@pytest.mark.parametrize(("check", "error"), CHECKS)
def test_a_relative_path_outside_is_refused_with_the_callers_error(work, check, error):
    outside = Path("..") / "run.json"
    with pytest.raises(error) as refused:
        check(outside)
    # type() rather than isinstance: a subclass of the caller's error must not pass.
    assert type(refused.value) is error
    # The message names the path as it renders on this platform (``..\run.json`` on
    # Windows), so build the expected text from the same Path rather than a literal.
    assert str(refused.value) == f"{outside} is outside the working directory"


@pytest.mark.parametrize(("check", "error"), CHECKS)
def test_an_absolute_path_outside_is_refused_with_the_callers_error(work, check, error):
    outside = work.parent / "run.json"
    with pytest.raises(error) as refused:
        check(outside)
    assert type(refused.value) is error
    assert str(refused.value) == f"{outside} is outside the working directory"
