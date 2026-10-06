"""Command-line entry point for music discovery."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

from musicdiscovery import __version__
from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.demo import TARGET_BYTES, DemoError, export_demo
from musicdiscovery.evaluation import (
    DEFAULT_QUERIES,
    DEFAULT_REPORT_DIR,
    RANKERS,
    EvaluationError,
    generate_queries,
    make_ranker,
    run,
    write_queries,
)
from musicdiscovery.hybrid import HybridConfigError
from musicdiscovery.ingest import CATALOG_NAME, MANIFEST_NAME, IngestError, ingest
from musicdiscovery.preference import DEFAULT_K, Preference, PreferenceError
from musicdiscovery.study import DEFAULT_RATINGS_DIR, StudyError
from musicdiscovery.study import run as run_study


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
        results = make_ranker(args.ranker).rank(preference, catalog)
    except (CatalogError, HybridConfigError, PreferenceError) as error:
        print(f"recommend: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
        return 0
    for rank, item in enumerate(results, start=1):
        print(f"{rank:>2}. {item.artist} - {item.title} (id {item.track_id})")
        print(f"      {'; '.join(item.explanations)}")
    return 0


def _run_evaluate(args: argparse.Namespace) -> int:
    try:
        catalog_dir = args.data_dir / "catalog"
        catalog = Catalog.load(catalog_dir / CATALOG_NAME)
        if args.generate_queries:
            write_queries(args.queries, generate_queries(catalog), catalog)
            print(f"evaluate: wrote queries to {args.queries}")
            return 0
        report_date = date.fromisoformat(args.date) if args.date else date.today()
        report, numbers = run(
            catalog,
            args.queries,
            args.ranker or ["baseline", "hybrid", "random", "popular"],
            args.out_dir,
            report_date,
            catalog_dir / MANIFEST_NAME,
        )
    except (
        CatalogError,
        EvaluationError,
        HybridConfigError,
        PreferenceError,
        ValueError,
    ) as error:
        print(f"evaluate: {error}", file=sys.stderr)
        return 1
    print(f"evaluate: wrote {report} and {numbers}")
    return 0


def _run_export_demo(args: argparse.Namespace) -> int:
    try:
        catalog = Catalog.load(args.data_dir / "catalog" / CATALOG_NAME)
        size = export_demo(catalog, args.out_dir)
    except (CatalogError, DemoError, EvaluationError, OSError) as error:
        print(f"export-demo: {error}", file=sys.stderr)
        return 1
    for name, (raw, packed) in size.files.items():
        print(
            f"export-demo: {name:<13}{raw / 1024:>9.1f} KiB"
            f"{packed / 1024:>9.1f} KiB gzip"
        )
    print(
        f"export-demo: bundle {size.raw / 1024:.1f} KiB, "
        f"{size.compressed / 1024:.1f} KiB compressed "
        f"(target under {TARGET_BYTES // 1024} KiB), wrote {args.out_dir}"
    )
    print(
        f"export-demo: first load {size.initial / 1024:.1f} KiB compressed, "
        f"deferred {size.deferred / 1024:.1f} KiB"
    )
    if size.compressed >= TARGET_BYTES:
        print("export-demo: bundle is over the size target", file=sys.stderr)
        return 1
    return 0


def _run_analyze_study(args: argparse.Namespace) -> int:
    try:
        report_date = date.fromisoformat(args.date) if args.date else date.today()
        report = run_study(args.ratings_dir, args.out_dir, report_date)
    except (StudyError, EvaluationError, OSError, ValueError) as error:
        print(f"analyze-study: {error}", file=sys.stderr)
        return 1
    print(f"analyze-study: wrote {report}")
    return 0


def _make_streams_safe() -> None:
    """Degrade to ``?`` instead of crashing on consoles that cannot encode a name."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse commands and report features that are not implemented yet."""
    _make_streams_safe()
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
    recommend.add_argument(
        "--ranker",
        choices=["baseline", "hybrid"],
        default="baseline",
        help="ranker to use (default: baseline)",
    )
    recommend.add_argument("--json", action="store_true", help="print JSON")
    evaluate = subparsers.add_parser(
        "evaluate", help="Score rankers on stored queries and write a report"
    )
    evaluate.add_argument("--data-dir", type=Path, default=Path("data"))
    evaluate.add_argument(
        "--ranker",
        action="append",
        choices=sorted(RANKERS),
        help="ranker to score; repeat for several (default: all)",
    )
    evaluate.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    evaluate.add_argument("--out-dir", type=Path, default=DEFAULT_REPORT_DIR)
    evaluate.add_argument("--date", help="report date, YYYY-MM-DD (default: today)")
    evaluate.add_argument(
        "--generate-queries",
        action="store_true",
        help="write the seeded query file for this catalog and exit",
    )
    export = subparsers.add_parser(
        "export-demo", help="Write the static demo bundle for the browser"
    )
    export.add_argument("out_dir", type=Path, help="directory to write the bundle to")
    export.add_argument("--data-dir", type=Path, default=Path("data"))

    study = subparsers.add_parser(
        "analyze-study",
        help="Validate listener-study ratings files and write the report",
    )
    study.add_argument(
        "ratings_dir",
        type=Path,
        nargs="?",
        default=DEFAULT_RATINGS_DIR,
        help="directory of returned ratings files (default: ratings)",
    )
    study.add_argument("--out-dir", type=Path, default=DEFAULT_REPORT_DIR)
    study.add_argument("--date", help="report date, YYYY-MM-DD (default: today)")

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "ingest":
        return _run_ingest(args.data_dir)

    if args.command == "recommend":
        return _run_recommend(args)

    if args.command == "evaluate":
        return _run_evaluate(args)

    if args.command == "export-demo":
        return _run_export_demo(args)

    if args.command == "analyze-study":
        return _run_analyze_study(args)

    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1
