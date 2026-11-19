"""The ranker registry is the one list of ranker names (#255)."""

import re
from pathlib import Path

import numpy as np
import pytest

from musicdiscovery import cli, eval_gate, evaluation, rankers
from musicdiscovery.audio_embedding import DEFAULT_VECTORS, AudioEmbeddingRanker
from musicdiscovery.rankers import (
    ARMS,
    EVALUATED,
    RANKERS,
    REFERENCE_RANKERS,
    EvaluationError,
    make_ranker,
)
from musicdiscovery.ranking import BaselineRanker

# What `main` offered before the registry; changing a list is a CLI change.
RECOMMEND_CHOICES = [
    "baseline",
    "hybrid",
    "profile",
    "profile-diverse",
    "audio-embedding",
]
EVALUATE_DEFAULT = ["baseline", "hybrid", "profile", "random", "popular"]


def _choices(capsys, command: str) -> list[str]:
    with pytest.raises(SystemExit):
        cli.main([command, "--help"])
    text = capsys.readouterr().out
    return re.search(r"--ranker \{([^}]*)\}", text).group(1).split(",")


def test_every_entry_is_an_arm_or_a_reference_ranker():
    assert set(ARMS) | set(REFERENCE_RANKERS) == set(RANKERS)
    assert not set(ARMS) & set(REFERENCE_RANKERS)
    assert set(EVALUATED) <= set(RANKERS)


def test_recommend_offers_the_registry_arms(capsys):
    assert _choices(capsys, "recommend") == list(ARMS) == RECOMMEND_CHOICES


def test_evaluate_offers_every_registry_entry(capsys):
    assert _choices(capsys, "evaluate") == sorted(RANKERS)


def test_evaluate_scores_the_evaluated_entries_by_default(monkeypatch, tmp_path):
    seen = {}

    def run(catalog, queries, names, *rest):
        seen["names"] = list(names)
        return Path("report.md"), Path("numbers.json")

    monkeypatch.setattr(cli.Catalog, "load", staticmethod(lambda path: None))
    monkeypatch.setattr(cli, "run", run)
    assert cli.main(["evaluate", "--data-dir", str(tmp_path)]) == 0
    assert seen["names"] == list(EVALUATED) == EVALUATE_DEFAULT


def test_evaluation_re_exports_the_registry():
    assert evaluation.RANKERS is RANKERS
    assert evaluation.make_ranker is make_ranker
    assert evaluation.EvaluationError is EvaluationError
    assert evaluation.RandomRanker is rankers.RandomRanker
    assert evaluation.PopularRanker is rankers.PopularRanker


@pytest.mark.parametrize("name", list(RANKERS))
def test_every_entry_builds_a_fresh_ranker(name):
    first, second = make_ranker(name), make_ranker(name)
    assert first is not second
    assert callable(first.rank)


def test_options_go_only_to_the_entries_that_take_them():
    description = ("bright", np.ones(3, dtype=np.float32))
    vectors = Path("elsewhere.npz")
    audio = make_ranker("audio-embedding", vectors=vectors, description=description)
    assert isinstance(audio, AudioEmbeddingRanker)
    assert audio.path == vectors
    assert audio.description is description
    plain = make_ranker("audio-embedding")
    assert plain.path == DEFAULT_VECTORS
    assert plain.description is None
    # The CLI offers the options whatever ranker it names.
    baseline = make_ranker("baseline", vectors=vectors, description=description)
    assert type(baseline) is BaselineRanker


def test_an_unknown_name_is_refused_with_the_choices():
    with pytest.raises(EvaluationError, match=r"unknown ranker 'nope'; choose from"):
        make_ranker("nope")


def test_the_gate_judges_registered_arms_against_a_reference_ranker():
    assert set(eval_gate.GATED_RANKERS) <= set(ARMS)
    assert set(eval_gate.GATED_RANKERS) <= set(EVALUATED)
    assert eval_gate.REFERENCE_RANKER in REFERENCE_RANKERS
    assert eval_gate.REFERENCE_RANKER in EVALUATED
