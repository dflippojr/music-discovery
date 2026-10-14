"""Command-line entry point for music discovery."""

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path

from musicdiscovery import __version__
from musicdiscovery.audio_embedding import (
    DEFAULT_ARCHIVE,
    DEFAULT_VECTORS,
    AudioEmbeddingError,
    AudioEmbeddingRanker,
    SentenceTransformerEncoder,
    describe_vector,
    embed_archive,
)
from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.demo import TARGET_BYTES, DemoError, export_demo
from musicdiscovery.evaluation import (
    BENCHMARKS,
    DEFAULT_PROFILES,
    DEFAULT_QUERIES,
    DEFAULT_REPORT_DIR,
    RANKERS,
    EvaluationError,
    generate_queries,
    make_ranker,
    run,
    write_profiles,
    write_queries,
)
from musicdiscovery.feedback import FeedbackError, read_log, render, summarise
from musicdiscovery.hybrid import HybridConfigError
from musicdiscovery.ingest import CATALOG_NAME, MANIFEST_NAME, IngestError, ingest
from musicdiscovery.preference import DEFAULT_K, Preference, PreferenceError
from musicdiscovery.profile_ranker import ProfileRankerError
from musicdiscovery.profiles import generate_profiles
from musicdiscovery.study import DEFAULT_RATINGS_DIR, StudyError
from musicdiscovery.study import run as run_study

MAX_REPORTED_LINES = 20


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
        if args.describe is not None and args.ranker != "audio-embedding":
            raise PreferenceError("--describe needs --ranker audio-embedding")
        if args.ranker == "audio-embedding":
            description = None
            if args.describe is not None:
                encoder = SentenceTransformerEncoder(threads=args.threads)
                description = describe_vector(args.describe, encoder)
            ranker = AudioEmbeddingRanker(path=args.vectors, description=description)
        else:
            ranker = make_ranker(args.ranker)
        results = ranker.rank(preference, catalog)
        if not results and isinstance(ranker, AudioEmbeddingRanker):
            missing = ranker.seeds_without_vector(preference, catalog)
            if missing and len(missing) == len(preference.seeds):
                print(
                    "recommend: no seed has an audio vector "
                    f"({', '.join(str(t) for t in missing)}); nothing to rank on",
                    file=sys.stderr,
                )
    except (
        AudioEmbeddingError,
        CatalogError,
        HybridConfigError,
        PreferenceError,
        ProfileRankerError,
    ) as error:
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
        if args.generate_profiles:
            write_profiles(args.profiles, generate_profiles(catalog), catalog)
            print(f"evaluate: wrote profiles to {args.profiles}")
            return 0
        report_date = date.fromisoformat(args.date) if args.date else date.today()
        report, numbers = run(
            catalog,
            args.queries,
            args.ranker or ["baseline", "hybrid", "profile", "random", "popular"],
            args.out_dir,
            report_date,
            catalog_dir / MANIFEST_NAME,
            args.benchmark,
            args.profiles,
        )
    except (
        AudioEmbeddingError,
        CatalogError,
        EvaluationError,
        HybridConfigError,
        PreferenceError,
        ProfileRankerError,
        ValueError,
    ) as error:
        print(f"evaluate: {error}", file=sys.stderr)
        return 1
    print(f"evaluate: wrote {report} and {numbers}")
    return 0


def _run_embed_audio(args: argparse.Namespace) -> int:
    try:
        catalog = Catalog.load(args.data_dir / "catalog" / CATALOG_NAME)
        encoder = SentenceTransformerEncoder(threads=args.threads)
        result = embed_archive(
            catalog,
            args.archive,
            args.vectors,
            encoder,
            limit=args.limit,
            sample_seed=args.sample_seed,
            batch_size=args.batch_size,
            log=lambda line: print(f"embed-audio: {line}", flush=True),
        )
    except (AudioEmbeddingError, CatalogError, OSError) as error:
        print(f"embed-audio: {error}", file=sys.stderr)
        return 1
    print(
        f"embed-audio: embedded {result.embedded} of {result.total} tracks, "
        f"{result.failed} failed, {result.skipped} already done; wrote {args.vectors}"
    )
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


def _run_feedback_report(args: argparse.Namespace) -> int:
    try:
        log = read_log(args.log)
    except FeedbackError as error:
        print(f"feedback-report: {error}", file=sys.stderr)
        return 1
    for problem in log.problems[:MAX_REPORTED_LINES]:
        print(f"feedback-report: skipped {problem}", file=sys.stderr)
    if len(log.problems) > MAX_REPORTED_LINES:
        more = len(log.problems) - MAX_REPORTED_LINES
        print(f"feedback-report: skipped {more} more malformed lines", file=sys.stderr)
    print(render(summarise(log.events)), end="")
    if not log.events:
        print("feedback-report: no valid events in the log", file=sys.stderr)
        return 1
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
        choices=["baseline", "hybrid", "profile", "profile-diverse", "audio-embedding"],
        default="baseline",
        help="ranker to use (default: baseline)",
    )
    recommend.add_argument(
        "--describe",
        metavar="TEXT",
        help="audio-embedding only: describe the qualities you like in words",
    )
    recommend.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    recommend.add_argument(
        "--threads", type=int, default=4, help="CPU threads for --describe"
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
    evaluate.add_argument(
        "--benchmark",
        choices=BENCHMARKS,
        default="queries",
        help="queries (default), profiles (held-out preferences) or all",
    )
    evaluate.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    evaluate.add_argument(
        "--generate-profiles",
        action="store_true",
        help="write the seeded listener profile file for this catalog and exit",
    )
    embed = subparsers.add_parser(
        "embed-audio",
        help="Embed the FMA small clips with EmbeddingGemma 2 and cache the vectors",
    )
    embed.add_argument("--data-dir", type=Path, default=Path("data"))
    embed.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    embed.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    embed.add_argument(
        "--limit", type=int, help="embed a seeded sample of this many tracks"
    )
    embed.add_argument("--sample-seed", type=int, default=0)
    embed.add_argument("--batch-size", type=int, default=4)
    embed.add_argument("--threads", type=int, default=4, help="CPU threads")
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

    feedback = subparsers.add_parser(
        "feedback-report",
        help="Summarise a feedback log downloaded from the demo page",
    )
    feedback.add_argument("log", type=Path, help="the downloaded .jsonl feedback log")

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

    if args.command == "embed-audio":
        return _run_embed_audio(args)

    if args.command == "export-demo":
        return _run_export_demo(args)

    if args.command == "analyze-study":
        return _run_analyze_study(args)

    if args.command == "feedback-report":
        return _run_feedback_report(args)

    print(f"{args.command}: not implemented yet", file=sys.stderr)
    return 1
