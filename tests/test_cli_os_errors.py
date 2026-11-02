"""An OSError in any subcommand is one reported line and exit 1, not a traceback."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_evaluation import make_frame

from musicdiscovery import cli

MESSAGE = "We couldn't connect to 'https://huggingface.co'"


def _raise_os_error(*args, **kwargs):
    raise OSError(MESSAGE)


def _fails_on_catalog(monkeypatch):
    monkeypatch.setattr(cli, "Catalog", SimpleNamespace(load=_raise_os_error))


def _fails_on(name):
    return lambda monkeypatch: monkeypatch.setattr(cli, name, _raise_os_error)


def _fails_on_model_after_catalog(monkeypatch):
    # The catalog and preference load for real, so only the model load fails.
    frame = make_frame()
    Path("data/catalog").mkdir(parents=True)
    frame.to_parquet("data/catalog/catalog.parquet", index=False)
    monkeypatch.setattr(cli, "SentenceTransformerEncoder", _raise_os_error)


def _fails_on_embed_model(monkeypatch):
    monkeypatch.setattr(cli, "Catalog", SimpleNamespace(load=lambda path: []))
    monkeypatch.setattr(cli, "SentenceTransformerEncoder", _raise_os_error)


SEEDED = ["recommend", "--seed", "100"]
DESCRIBED = [*SEEDED, "--ranker", "audio-embedding", "--describe", "calm piano"]

# One row for each stage of each subcommand that reads or fetches something.
# test_every_subcommand_has_a_case fails when a new subcommand has no row here.
CASES = {
    "ingest": (["ingest"], _fails_on("ingest")),
    "recommend catalog": (SEEDED, _fails_on_catalog),
    "recommend model": (DESCRIBED, _fails_on_model_after_catalog),
    "evaluate": (["evaluate"], _fails_on_catalog),
    "embed-audio catalog": (["embed-audio"], _fails_on_catalog),
    "embed-audio model": (["embed-audio"], _fails_on_embed_model),
    "export-demo": (["export-demo", "out"], _fails_on_catalog),
    "analyze-study": (["analyze-study"], _fails_on("run_study")),
    "feedback-report": (["feedback-report", "log.jsonl"], _fails_on("read_log")),
}


# The reproduction from the issue, as given.
def test_describe_model_load_oserror(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    frame = make_frame()
    Path("data/catalog").mkdir(parents=True)
    frame.to_parquet("data/catalog/catalog.parquet", index=False)

    def offline(threads):
        raise OSError("We couldn't connect to 'https://huggingface.co'")

    monkeypatch.setattr(cli, "SentenceTransformerEncoder", offline)
    seed = str(int(frame["track_id"].iloc[0]))
    code = cli.main(
        [
            "recommend",
            "--seed",
            seed,
            "--ranker",
            "audio-embedding",
            "--describe",
            "calm piano",
        ]
    )
    assert code == 1
    assert "huggingface" in capsys.readouterr().err


def test_describe_reports_a_torch_that_cannot_load(tmp_path, monkeypatch, capsys):
    # The real encoder, with no stub of it: its constructor turns a model-load
    # failure and an ImportError into AudioEmbeddingError, but a torch whose
    # shared library won't load raises OSError from the import itself.
    monkeypatch.chdir(tmp_path)
    frame = make_frame()
    Path("data/catalog").mkdir(parents=True)
    frame.to_parquet("data/catalog/catalog.parquet", index=False)
    Path("broken").mkdir()
    Path("broken/torch.py").write_text(
        "raise OSError('libtorch_cpu.so: cannot open shared object file')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path / "broken"))
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    seed = str(int(frame["track_id"].iloc[0]))
    command = ["recommend", "--seed", seed, "--ranker", "audio-embedding"]
    assert cli.main([*command, "--describe", "calm piano"]) == 1
    assert capsys.readouterr().err == (
        "recommend: libtorch_cpu.so: cannot open shared object file\n"
    )


@pytest.mark.parametrize("case", CASES)
def test_os_error_is_one_reported_line(case, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    argv, make_it_fail = CASES[case]
    make_it_fail(monkeypatch)
    assert cli.main(argv) == 1
    captured = capsys.readouterr()
    assert captured.err == f"{argv[0]}: {MESSAGE}\n"
    assert captured.out == ""


def test_every_subcommand_has_a_case(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    listed = re.search(r"\{([^}]*)\}", capsys.readouterr().out).group(1)
    assert {argv[0] for argv, _ in CASES.values()} == set(listed.split(","))
