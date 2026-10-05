"""Command-line entry point for music discovery."""

import argparse
import sys
from collections.abc import Sequence

from musicdiscovery import __version__


def main(argv: Sequence[str] | None = None) -> int:
    """Parse commands and report features that are not implemented yet."""
    parser = argparse.ArgumentParser(prog="musicdiscovery", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    for command in ("ingest", "recommend", "evaluate"):
        subparsers.add_parser(
            command, help=f"{command.capitalize()} (not implemented yet)"
        )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1
