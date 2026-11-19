"""The report: catalog version, Markdown rendering, and `run`, which writes it."""

import hashlib
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from musicdiscovery import paths
from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation.benchmark import evaluate, evaluate_profiles
from musicdiscovery.evaluation.metrics import METRIC_NOTES, METRICS, PROFILE_METRICS
from musicdiscovery.evaluation.profiles_io import DEFAULT_PROFILES, load_profiles
from musicdiscovery.evaluation.queries import load_queries
from musicdiscovery.profiles import SPLITS
from musicdiscovery.rankers import EvaluationError

DATASET_VERSION = "FMA dataset v1 (archive files dated 2017-04-01), small subset"
BENCHMARKS = ("queries", "profiles", "all")
DEFAULT_REPORT_DIR = Path("reports")


def catalog_version(manifest_path: Path | None, catalog: Catalog) -> dict:
    """What the report says about the catalog, from ingest's manifest if present."""
    info: dict = {"dataset": DATASET_VERSION, "tracks": len(catalog)}
    if manifest_path is not None and manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise EvaluationError(
                f"cannot read ingest manifest {manifest_path} ({error})"
            ) from error
        if not isinstance(manifest, dict):
            raise EvaluationError(
                f"{manifest_path} is not an ingest manifest (expected a JSON object); "
                "run `musicdiscovery ingest` again"
            )
        info["ingest_version"] = manifest.get("ingest_version")
        info["source_sha1"] = manifest.get("source_sha1")
        info["subset"] = manifest.get("subset")
    return info


LIMITS = """\
These numbers come from proxies, not listeners. Read them with the following in mind.

- **Genre labels are a proxy for relevance, not ground truth.** A result counts as
  same-genre when it shares the seed's top genre. That rewards obvious picks, says
  nothing about whether a listener enjoys a result, and cannot show that anything
  was discovered. A ranker that returns the same sound-alikes every time scores well.
  The `popular` reference ranker returns only the seed's genre, so it scores 1.0 on
  the same-genre rate by construction, and `hybrid` and `profile` score on genre
  ids and tags.
- **Held-out tag overlap hides half of each track's tags from the rankers**, so it
  does not reward using a label it is scored against. It is still a proxy: tags are
  noisy, sparse (tracks with fewer than two tags have nothing to hide, and queries
  seeded on them are skipped), and correlated with genre, so a ranker that finds
  same-genre tracks gains through the visible tags too. A tag a listener would not
  care about counts as much as one they would. When a query likes a tag that is
  held out for its seed, that like is dropped for this metric.
- **Queries are synthetic.** Seeds are random catalog tracks and likes are sampled
  from the seed's own genre, tags and a feature axis. Real listeners are messier.
- **Tag overlap** depends on how well people tagged tracks; only queries whose seed
  has tags count, and many tracks have none.
- **Preference adherence** checks that results move a coarse librosa feature in the
  requested direction relative to the seed. It does not check that the change is
  audible or wanted.
- **Popularity** is the mean listen-count percentile of results; a catalog average
  is about 0.5. High values mean a ranker leans on what is already popular.
- **Coverage** is a single number per split with no interval: resampling queries
  can only shrink the set of tracks recommended, so a bootstrap would be biased.
- **Intervals** are bootstrap 95% intervals over queries only. They do not cover
  the choice of queries, the catalog or the ranker settings, and the reference
  rankers are not tuned.
- **The validation split is for choosing settings; the test split is for reporting.**
  Do not tune on test.
- Listener studies are the next step (issue #8); this report does not replace them.
"""


def _cell(score: dict) -> str:
    if score["mean"] is None:
        return "n/a"
    if score["lo"] is None:
        return f"{score['mean']:.3f}"
    return f"{score['mean']:.3f} [{score['lo']:.3f}, {score['hi']:.3f}]"


MIN_SCORED_QUERIES = 30


def _sparsity_note(results: dict) -> list[str]:
    """A warning, when tags are too sparse for a stable held-out interval."""
    skipped = results["heldout"]["queries_skipped"]
    scored = {split: results["queries"][split] - skipped[split] for split in SPLITS}
    if min(scored.values()) >= MIN_SCORED_QUERIES:
        return []
    return [
        f"**Tags are sparse:** only {scored['validation']} validation and "
        f"{scored['test']} test queries have a seed with two or more tags, so the "
        "held-out tag intervals are wide and rest on few queries. Read that metric "
        "as weak evidence, and do not rank rankers whose intervals overlap.",
        "",
    ]


def _version_lines(version: dict) -> list[str]:
    lines = [
        "## Catalog version",
        "",
        f"- Dataset: {version['dataset']} (see `docs/catalog.md`)",
        f"- Tracks: {version['tracks']}",
    ]
    if "ingest_version" in version:
        lines += [
            f"- Ingest version: {version['ingest_version']}",
            f"- Source archive SHA1: `{version['source_sha1']}`",
        ]
    lines.append("")
    return lines


def render_report(
    results: dict,
    version: dict,
    report_date: date,
    queries_path: str,
    extra: Sequence[str] = (),
) -> str:
    """The query benchmark report; `extra` lines go just before the limits."""
    names = list(results["rankers"])
    lines = [
        f"# Offline evaluation, {report_date.isoformat()}",
        "",
        f"Rankers: {', '.join(names)}. k = {results['k']}. Queries: "
        f"{results['queries']['validation']} validation and "
        f"{results['queries']['test']} test, from `{queries_path}`. "
        "Cells are the mean with a bootstrap 95% interval "
        f"({results['bootstrap_resamples']} resamples over queries).",
        "",
        f"Held-out tags: {results['heldout']['fraction']:.0%} of each track's tags "
        f"are hidden from the rankers, split with seed {results['heldout']['seed']}. "
        "Queries skipped because the seed has fewer than two tags: "
        f"{results['heldout']['queries_skipped']['validation']} validation, "
        f"{results['heldout']['queries_skipped']['test']} test.",
        "",
        *_sparsity_note(results),
        *_version_lines(version),
    ]
    for metric, title in METRICS.items():
        lines += [
            f"## {title}",
            "",
            "| Ranker | Validation | Test |",
            "| --- | --- | --- |",
        ]
        for name in names:
            scores = results["rankers"][name]
            lines.append(
                f"| {name} | {_cell(scores['validation'][metric])} "
                f"| {_cell(scores['test'][metric])} |"
            )
        if metric in METRIC_NOTES:
            lines += ["", METRIC_NOTES[metric]]
        if metric == "popularity":
            lines += ["", f"Catalog mean: {results['catalog_mean_popularity']:.3f}."]
        if metric == "adherence":
            lines += [
                "",
                f"{results['queries_with_axis_likes']} of "
                f"{sum(results['queries'].values())} queries ask for an axis "
                "direction.",
            ]
        lines.append("")
    lines += [*extra, "## Limits", "", LIMITS]
    return "\n".join(lines)


PROFILE_LIMITS = """Limits of the held-out preference benchmark:

- **Synthetic tastes reward rankers that share the generator's features.** Each
  profile's interactions are drawn from a hidden taste built from a top genre,
  catalog tags and a librosa feature axis, so a ranker that scores on those same
  columns (`hybrid` and `profile` use genre ids and tags, `popular` filters on the
  top genre) is favoured by construction. A high score shows that a ranker
  recovers the generator's taste, not that a listener would like the results.
- **`profile` is trained on this generator.** Its weights are fit on the
  validation profiles, which come from the same generator as the test profiles,
  so its test score measures how well it learned the generator, not real taste.
- **Interactions are not listens.** Strengths are synthetic play fractions: taste
  draws are mostly strong and noise draws mostly weak. The weak-signal threshold
  drops plays below it from the history and from the positives; a dropped weak
  play is not shown to rankers and is not excluded from their results.
- **Rankers see the full catalog**, including the tags the generator used, and
  get the filtered history as seeds with no liked qualities.
- **Intervals** are bootstrap 95% intervals over profiles only; they do not cover
  the generator's settings or the choice of catalog.
- Listener studies are the next step (issue #8); this benchmark does not replace
  them.
"""


def render_profiles_section(section: dict) -> list[str]:
    """The "Held-out preferences" section of the report."""
    names = list(section["rankers"])
    count = section["profiles"]
    lines = [
        "## Held-out preferences",
        "",
        f"Profiles: {count['validation']} validation and {count['test']} test, from "
        f"`{section['profiles_path']}` (profile seed {section['profile_seed']}). "
        f"Rankers: {', '.join(names)}. k = {section['k']}. Each ranker gets a "
        "profile's history as seeds and is scored on the profile's held-out liked "
        "tracks. Cells are the mean with a bootstrap 95% interval "
        f"({section['bootstrap_resamples']} resamples over profiles).",
        "",
        f"Weak-signal threshold: interactions with a play fraction below "
        f"{section['weak_signal_threshold']} are dropped from history and positives. "
        "Dropped: "
        f"{section['weak_history_dropped']['validation']} validation and "
        f"{section['weak_history_dropped']['test']} test history plays, "
        f"{section['weak_heldout_dropped']['validation']} validation and "
        f"{section['weak_heldout_dropped']['test']} test held-out plays. Kept: "
        f"{section['history_tracks']['validation']} and "
        f"{section['history_tracks']['test']} history seeds, "
        f"{section['positives']['validation']} and {section['positives']['test']} "
        "held-out positives.",
        "",
    ]
    for metric, title in PROFILE_METRICS.items():
        lines += [
            f"### {title}",
            "",
            "| Ranker | Validation | Test |",
            "| --- | --- | --- |",
        ]
        for name in names:
            scores = section["rankers"][name]
            lines.append(
                f"| {name} | {_cell(scores['validation'][metric])} "
                f"| {_cell(scores['test'][metric])} |"
            )
        lines.append("")
    return [*lines, PROFILE_LIMITS]


def _sha256(path: Path) -> str:
    """Hash a stored JSON file with LF line endings, so a Windows checkout
    (CRLF) and a Linux one record the same hash and reports stay comparable."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _relative(path: Path) -> str:
    """The path as report text. At an ASCII locale with UTF-8 mode off, Python
    decodes a non-ASCII argument to lone surrogates that no codec can write, so
    recover its bytes and read them as UTF-8 (#208)."""
    text = path.relative_to(Path.cwd().resolve()).as_posix()
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def run(
    catalog: Catalog,
    queries_path: Path,
    ranker_names: Sequence[str],
    out_dir: Path,
    report_date: date,
    manifest_path: Path | None = None,
    benchmark: str = "queries",
    profiles_path: Path = DEFAULT_PROFILES,
) -> tuple[Path, Path]:
    """Evaluate and write `offline-<date>.md` and `.json`; return their paths.

    `benchmark` is `queries` (the default), `profiles` (held-out preferences) or
    `all`, which adds the held-out preferences section to the query report.
    """
    if benchmark not in BENCHMARKS:
        raise EvaluationError(f"unknown benchmark {benchmark!r}; choose {BENCHMARKS}")
    out_dir = paths.confined(out_dir, EvaluationError)
    names = list(dict.fromkeys(ranker_names))
    version = catalog_version(manifest_path, catalog)
    results: dict = {}
    extra: list[str] = []
    if benchmark != "queries":
        profiles_path = paths.confined(profiles_path, EvaluationError)
        profiles = load_profiles(profiles_path, catalog)
        section = evaluate_profiles(catalog, profiles, names)
        section["profiles_path"] = _relative(profiles_path)
        section["profiles_sha256"] = _sha256(profiles_path)
        results["held_out_preferences"] = section
        extra = render_profiles_section(section)
    if benchmark == "profiles":
        heading = [f"# Offline evaluation, {report_date.isoformat()}", ""]
        text = "\n".join([*heading, *_version_lines(version), *extra])
    else:
        queries_path = paths.confined(queries_path, EvaluationError)
        queries = load_queries(queries_path, catalog)
        results.update(evaluate(catalog, queries, names))
        results["queries_sha256"] = _sha256(queries_path)
        text = render_report(
            results, version, report_date, _relative(queries_path), extra
        )
    results["catalog"] = version
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"offline-{report_date.isoformat()}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    md_path.write_text(text, encoding="utf-8")
    return md_path, json_path
