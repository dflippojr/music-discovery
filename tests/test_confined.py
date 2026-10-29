"""The working-directory check that evaluation and audio_embedding both expose.

Characterizes what callers see: the resolved path for a path inside the working
directory, and the exact exception type and message for one outside it.
"""

from pathlib import Path

import pytest

from musicdiscovery import audio_embedding, evaluation
from musicdiscovery.audio_embedding import AudioEmbeddingError
from musicdiscovery.evaluation import EvaluationError

CHECKS = [
    pytest.param(evaluation.confined, EvaluationError, id="evaluation"),
    pytest.param(audio_embedding.confined, AudioEmbeddingError, id="audio_embedding"),
]


@pytest.fixture
def work(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


@pytest.mark.parametrize("check", [evaluation.confined, audio_embedding.confined])
def test_a_relative_path_inside_resolves_under_the_working_directory(work, check):
    assert check(Path("out") / "run.json") == (work / "out" / "run.json").resolve()


@pytest.mark.parametrize("check", [evaluation.confined, audio_embedding.confined])
def test_an_absolute_path_inside_resolves_to_itself(work, check):
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
