"""Command-line entry point for music discovery."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from musicdiscovery import __version__
from musicdiscovery.ingest import IngestError, ingest


def _run_ingest(data_dir: Path) -> int:
    try:
        result = ingest(data_dir)
    except IngestError as error:
        print(f"ingest: {error}", file=sys.stderr)
        return 1
    if result.skipped:
        print(f"ingest: catalog is up to date ({result.kept} tracks); nothing to do")
        return 0
    print(
        f"ingest: kept {result.kept} of {result.subset_tracks} tracks; dropped "
        f"{result.dropped_missing_license} without a license and "
        f"{result.dropped_missing_features} without features"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse commands and report features that are not implemented yet."""
    parser = argparse.ArgumentParser(prog="musicdiscovery", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    ingest_parser = subparsers.add_parser(
        "ingest", help="Download FMA metadata and write the cleaned catalog"
    )
    ingest_parser.add_argument(
        "--data-dir", type=Path, default=Path("data"), help="cache and output directory"
    )
    for command in ("recommend", "evaluate"):
        subparsers.add_parser(
            command, help=f"{command.capitalize()} (not implemented yet)"
        )

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "ingest":
        return _run_ingest(args.data_dir)

    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1
