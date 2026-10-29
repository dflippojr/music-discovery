"""Read the JSON files the package ships, or a path the caller names, and describe
a jsonschema violation as one line.

Each caller keeps its own except clause and error class, because each names its
failure in its own words.
"""

import json
from importlib import resources
from pathlib import Path
from typing import Any

from jsonschema import ValidationError


def load_packaged_json(name: str, path: Path | None = None) -> Any:
    """Parse the packaged file `name`, or the file at `path` when one is given."""
    if path is None:
        source = resources.files("musicdiscovery").joinpath(name)
        text = source.read_text(encoding="utf-8")
    else:
        text = path.read_text(encoding="utf-8")
    return json.loads(text)


def describe_schema_error(error: ValidationError) -> str:
    """Where the violation sits in the document, then what is wrong with it."""
    where = "/".join(str(part) for part in error.path) or "top level"
    return f"{where}: {error.message[:120]}"
