"""Check the installed CLI contract without datasets or audio."""

import shutil
import subprocess
import sysconfig
from importlib.metadata import version

import pytest

from musicdiscovery.cli import main


def test_installed_version():
    # Not next to sys.executable: a Windows install outside a venv puts it in Scripts.
    script = shutil.which("musicdiscovery", path=sysconfig.get_path("scripts"))
    assert script is not None
    result = subprocess.run(
        [script, "--version"], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == version("musicdiscovery")
    assert result.stderr == ""


@pytest.mark.parametrize("argv", [[], ["--help"], ["ingest", "--help"]])
def test_help(argv, capsys):
    if argv:
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 0
    else:
        assert main(argv) == 0
    assert "usage: musicdiscovery" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["unknown"], ["ingest", "--unknown"]])
def test_invalid_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err
