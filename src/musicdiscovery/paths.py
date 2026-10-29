"""Keep user-supplied paths inside the working directory.

One check for every module that reads or writes a path the user names. It takes
the error class to raise, so the modules that raise their own error can share it
without importing each other's.
"""

from pathlib import Path


def confined(path: Path, error: type[Exception]) -> Path:
    """Resolve a user-supplied path and keep it inside the working directory.

    Raises ``error`` when the resolved path is outside the working directory.
    """
    resolved = (Path.cwd() / path).resolve()
    if not resolved.is_relative_to(Path.cwd().resolve()):
        raise error(f"{path} is outside the working directory")
    return resolved
