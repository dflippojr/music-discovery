"""Shipped code names the encoding of every text file it opens (#208).

Without one, Python uses the locale encoding: cp1252 on a Windows console. A
report that names a non-ASCII path then fails after the whole run, and a
hand-edited UTF-8 file is misread. Ruff's PLW1514 misses most such calls
because it cannot tell that `confined(path)` returns a `Path`, so this check
reads the syntax instead and flags every text-mode call that leaves the
encoding out, whatever the receiver is.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHIPPED = [ROOT / "src" / "musicdiscovery", ROOT / "scripts"]
TEXT_MODE = set("rwxat+")


def _arg(call: ast.Call, position: int, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return call.args[position] if len(call.args) > position else None


def _is_text_mode(mode: ast.expr | None) -> bool:
    """True for a missing mode or a literal text mode such as "r" or "w+"."""
    if mode is None:
        return True
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return False  # a computed mode, or not a mode at all (ZipFile.open)
    return bool(mode.value) and set(mode.value) <= TEXT_MODE


def missing_encoding(call: ast.Call) -> bool:
    """True when `call` opens a text file at the locale encoding."""
    func = call.func
    if isinstance(func, ast.Name) and func.id == "open":
        return _is_text_mode(_arg(call, 1, "mode")) and not _arg(call, 3, "encoding")
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "read_text":
        return _arg(call, 0, "encoding") is None
    if func.attr == "write_text":
        return _arg(call, 1, "encoding") is None
    if func.attr == "open":
        return _is_text_mode(_arg(call, 0, "mode")) and not _arg(call, 2, "encoding")
    return False


def offenders(source: str, label: str) -> list[str]:
    return [
        f"{label}:{node.lineno}: {ast.unparse(node)}"
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and missing_encoding(node)
    ]


def test_shipped_code_names_the_encoding_of_every_text_file():
    found = []
    for root in SHIPPED:
        for path in sorted(root.rglob("*.py")):
            label = path.relative_to(ROOT).as_posix()
            found += offenders(path.read_text(encoding="utf-8"), label)
    assert not found, "pass encoding='utf-8':\n" + "\n".join(found)


@pytest.mark.parametrize(
    "source",
    [
        "path.read_text()",
        "confined(path).write_text(text)",
        "path.write_text(text, errors='strict')",
        "open(name)",
        "open(name, 'w')",
        "open(name, mode='a+')",
        "path.open()",
        "path.open('r')",
        "path.open(mode='w', newline='')",
    ],
)
def test_a_call_without_an_encoding_is_flagged(source):
    assert offenders(source, "x") == [f"x:1: {source}"]


@pytest.mark.parametrize(
    "source",
    [
        "path.read_text(encoding='utf-8')",
        "path.read_text('utf-8-sig')",
        "path.write_text(text, 'utf-8')",
        "path.write_text(text, encoding='utf-8')",
        "path.read_bytes()",
        "open(name, 'rb')",
        "open(name, 'w', -1, 'utf-8')",
        "open(name, encoding='utf-8')",
        "path.open('wb')",
        "path.open('w', -1, 'utf-8')",
        "path.open(mode, encoding='utf-8')",
        "archive.open(PREFIX + 'tracks.csv')",
        "archive.open('tracks.csv')",
        "av.open(io.BytesIO(data))",
        "open(name, mode)",
        "print(x)",
        "handlers[key]()",
    ],
)
def test_a_call_with_an_encoding_or_binary_mode_passes(source):
    assert offenders(source, "x") == []
