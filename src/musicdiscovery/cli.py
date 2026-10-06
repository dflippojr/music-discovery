"""Command-line entry point for music discovery."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from musicdiscovery import __version__
from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.ingest import CATALOG_NAME, IngestError, ingest
from musicdiscovery.preference import DEFAULT_K, Preference, PreferenceError
from musicdiscovery.ranking import BaselineRanker


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


def _run_recommend(args: argparse.Namespace) -> int:
    try:
        catalog = Catalog.load(args.data_dir / "catalog" / CATALOG_NAME)
        preference = Preference.from_strings(
            args.seed, args.like, args.dislike, args.k, args.exclude_seed_artist
        )
        results = BaselineRanker().rank(preference, catalog)
    except (CatalogError, PreferenceError) as error:
        print(f"recommend: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return 0
    for rank, item in enumerate(results, start=1):
        print(f"{rank:>2}. {item.artist} - {item.title} (id {item.track_id})")
        print(f"      {'; '.join(item.explanations)}")
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
    recommend = subparsers.add_parser(
        "recommend", help="Rank catalog tracks from seeds and preferences"
    )
    recommend.add_argument("--data-dir", type=Path, default=Path("data"))
    recommend.add_argument(
        "--seed", type=int, action="append", required=True, help="seed track id"
    )
    recommend.add_argument(
        "--like",
        action="append",
        default=[],
        metavar="QUALITY",
        help="genre:<name>, tag:<name>, more:<axis> or less:<axis>",
    )
    recommend.add_argument(
        "--dislike",
        action="append",
        default=[],
        metavar="QUALITY",
        help="same forms as --like",
    )
    recommend.add_argument("-k", type=int, default=DEFAULT_K, help="result count")
    recommend.add_argument(
        "--exclude-seed-artist", action="store_true", help="skip the seed artist"
    )
    recommend.add_argument("--json", action="store_true", help="print JSON")
    subparsers.add_parser("evaluate", help="Evaluate (not implemented yet)")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "ingest":
        return _run_ingest(args.data_dir)

    if args.command == "recommend":
        return _run_recommend(args)

    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1
