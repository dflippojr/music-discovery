"""Offline evaluation: reference rankers, stored queries, metrics and the report."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.preference import AXES, Preference, Quality
from musicdiscovery.ranking import BaselineRanker, Ranker, Recommendation

K = 10
QUERY_SEED = 20261005
BOOTSTRAP_SEED = 7
BOOTSTRAP_RESAMPLES = 1000
QUERIES_PER_SPLIT = 100
SPLITS = ("validation", "test")
DATASET_VERSION = "FMA dataset v1 (archive files dated 2017-04-01), small subset"
DEFAULT_QUERIES = Path("eval") / "queries.json"
DEFAULT_REPORT_DIR = Path("reports")


class EvaluationError(Exception):
    """Raised when queries or rankers cannot be used."""


# --- reference rankers -------------------------------------------------------


def _bare(catalog: Catalog, rows: Sequence[int]) -> list[Recommendation]:
    frame = catalog.frame
    return [
        Recommendation(
            track_id=int(catalog.track_ids[row]),
            title=str(frame["title"].iloc[row]) if "title" in frame else "",
            artist=str(frame["artist"].iloc[row]),
            score=0.0,
            terms=(),
            explanations=(),
        )
        for row in rows
    ]


def _allowed(preference: Preference, catalog: Catalog) -> np.ndarray:
    seed_rows = [catalog.position[s] for s in preference.seeds]
    allowed = np.ones(len(catalog), dtype=bool)
    allowed[seed_rows] = False
    if preference.exclude_seed_artist:
        artists = catalog.frame["artist"]
        allowed &= ~artists.isin(artists.iloc[seed_rows]).to_numpy()
    return allowed


class RandomRanker:
    """Uniformly random tracks; the same preference always gives the same list."""

    def __init__(self, seed: int = 0):
        self.seed = seed

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        rng = np.random.default_rng([self.seed, *preference.seeds])
        candidates = np.flatnonzero(_allowed(preference, catalog))
        picked = rng.permutation(candidates)[: preference.k]
        return _bare(catalog, picked.tolist())


class PopularRanker:
    """The most listened-to tracks in the first seed's top genre."""

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        allowed = _allowed(preference, catalog)
        genre = catalog.frame["genre_top"].iloc[catalog.position[preference.seeds[0]]]
        if isinstance(genre, str):
            allowed &= catalog.has_genre(genre)
        candidates = np.flatnonzero(allowed)
        listens = catalog.frame["listens"].to_numpy(dtype=np.float64)[candidates]
        order = np.lexsort((catalog.track_ids[candidates], -listens))
        return _bare(catalog, candidates[order[: preference.k]].tolist())


RANKERS: dict[str, Callable[[], Ranker]] = {
    "baseline": BaselineRanker,
    "random": RandomRanker,
    "popular": PopularRanker,
}


def make_ranker(name: str) -> Ranker:
    if name not in RANKERS:
        raise EvaluationError(f"unknown ranker {name!r}; choose from {sorted(RANKERS)}")
    return RANKERS[name]()


# --- queries -----------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    """One synthetic listener: a seed track and some liked qualities."""

    id: int
    split: str
    seed: int
    likes: tuple[str, ...]

    def preference(self, k: int = K) -> Preference:
        return Preference.from_strings([self.seed], self.likes, k=k)


def generate_queries(
    catalog: Catalog,
    per_split: int = QUERIES_PER_SPLIT,
    seed: int = QUERY_SEED,
) -> list[Query]:
    """Sample seeds and likes with a fixed seed; one catalog gives one list."""
    rng = np.random.default_rng(seed)
    total = per_split * len(SPLITS)
    if total > len(catalog):
        raise EvaluationError(f"catalog of {len(catalog)} tracks is too small")
    rows = rng.choice(len(catalog), size=total, replace=False)
    axes = sorted(AXES)
    queries = []
    for index, row in enumerate(rows.tolist()):
        record = catalog.frame.iloc[row]
        likes: list[str] = []
        if rng.random() < 0.7:
            direction = "more" if rng.random() < 0.5 else "less"
            likes.append(f"{direction}:{axes[int(rng.integers(len(axes)))]}")
        if isinstance(record["genre_top"], str) and rng.random() < 0.5:
            likes.append(f"genre:{record['genre_top']}")
        tags = sorted(record["tags"])
        if tags and rng.random() < 0.5:
            likes.append(f"tag:{tags[int(rng.integers(len(tags)))]}")
        if not likes:
            likes.append(f"more:{axes[int(rng.integers(len(axes)))]}")
        queries.append(
            Query(
                id=index,
                split=SPLITS[index // per_split],
                seed=int(catalog.track_ids[row]),
                likes=tuple(likes),
            )
        )
    return queries


def write_queries(path: Path, queries: Sequence[Query], catalog: Catalog) -> None:
    payload = {
        "query_seed": QUERY_SEED,
        "k": K,
        "catalog_tracks": len(catalog),
        "queries": [asdict(q) for q in queries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")


def load_queries(path: Path, catalog: Catalog) -> list[Query]:
    if not path.exists():
        raise EvaluationError(
            f"no queries at {path}; create them with `musicdiscovery evaluate "
            "--generate-queries`"
        )
    try:
        payload = json.loads(path.read_text())
        queries = [
            Query(int(q["id"]), str(q["split"]), int(q["seed"]), tuple(q["likes"]))
            for q in payload["queries"]
        ]
        expected = payload.get("catalog_tracks")
    except (ValueError, KeyError, TypeError) as error:
        raise EvaluationError(f"cannot read queries at {path}: {error!r}") from error
    if expected is not None and expected != len(catalog):
        raise EvaluationError(
            f"queries were made for a catalog of {expected} tracks, "
            f"this one has {len(catalog)}"
        )
    for split in SPLITS:
        if not any(q.split == split for q in queries):
            raise EvaluationError(f"queries have no {split} split")
    if any(q.split not in SPLITS for q in queries):
        raise EvaluationError(f"query splits must be one of {SPLITS}")
    return queries


# --- metrics -----------------------------------------------------------------

METRICS: dict[str, str] = {
    "genre_precision": "Genre precision@10",
    "genre_ndcg": "Genre nDCG@10",
    "tag_overlap": "Tag overlap@10 (mean Jaccard with the seed)",
    "adherence": "Preference adherence@10",
    "artist_diversity": "Artist diversity (distinct artists / list length)",
    "popularity": "Popularity (mean listen percentile of results)",
    "coverage": "Catalog coverage",
}


class _Context:
    """Per-catalog lookups shared by every query and ranker."""

    def __init__(self, catalog: Catalog):
        frame = catalog.frame
        self.catalog = catalog
        self.genres = frame["genre_top"].astype("string").to_numpy(dtype=object)
        self.tags = [frozenset(t) for t in frame["tags"]]
        self.artists = frame["artist"].astype(str).to_numpy()
        self.percentile = frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
        self.relevant_total = {
            g: int(np.sum(self.genres == g))
            for g in set(self.genres)
            if isinstance(g, str)
        }


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def query_metrics(
    ctx: _Context, query: Query, results: Sequence[Recommendation], k: int = K
) -> tuple[dict[str, float], np.ndarray]:
    """Per-query metric values (nan where undefined) and the recommended rows."""
    catalog = ctx.catalog
    rows = np.array([catalog.position[r.track_id] for r in results], dtype=int)
    seed_row = catalog.position[query.seed]
    nan = float("nan")
    out = dict.fromkeys(METRICS, nan)
    out.pop("coverage")
    if not len(rows):
        return out, rows

    seed_genre = ctx.genres[seed_row]
    if isinstance(seed_genre, str):
        hits = (ctx.genres[rows] == seed_genre).astype(float)
        out["genre_precision"] = float(hits.mean())
        gains = hits / np.log2(np.arange(2, len(hits) + 2))
        available = min(k, ctx.relevant_total[seed_genre] - 1)
        ideal = (1.0 / np.log2(np.arange(2, available + 2))).sum()
        out["genre_ndcg"] = float(gains.sum() / ideal) if ideal else nan

    seed_tags = ctx.tags[seed_row]
    if seed_tags:
        out["tag_overlap"] = float(
            np.mean([_jaccard(seed_tags, ctx.tags[r]) for r in rows])
        )

    shares = []
    for text in query.likes:
        quality = Quality.parse(text)
        if quality.kind != "axis":
            continue
        column = catalog.frame[AXES[quality.name].column].to_numpy(dtype=np.float64)
        moved = (column[rows] - column[seed_row]) * quality.direction > 0
        shares.append(float(moved.mean()))
    if shares:
        out["adherence"] = float(np.mean(shares))

    out["artist_diversity"] = len(set(ctx.artists[rows])) / len(rows)
    out["popularity"] = float(ctx.percentile[rows].mean())
    return out, rows


def _mean(values: np.ndarray) -> float:
    valid = values[~np.isnan(values)]
    return float(valid.mean()) if len(valid) else float("nan")


def _interval(values: np.ndarray, resamples: np.ndarray) -> tuple[float, float, float]:
    """Mean and bootstrap 95% interval of per-query values (nan entries skipped)."""
    picked = values[resamples]
    valid = ~np.isnan(picked)
    counts = valid.sum(axis=1)
    sums = np.where(valid, picked, 0.0).sum(axis=1)
    means = sums[counts > 0] / counts[counts > 0]
    if not len(means):
        nan = float("nan")
        return nan, nan, nan
    lo, hi = np.percentile(means, [2.5, 97.5])
    return _mean(values), float(lo), float(hi)


def _round(value: float) -> float | None:
    return None if np.isnan(value) else round(float(value), 6)


def evaluate(
    catalog: Catalog,
    queries: Sequence[Query],
    ranker_names: Sequence[str],
    k: int = K,
    resamples: int = BOOTSTRAP_RESAMPLES,
) -> dict:
    """Score each ranker on every query; return the numbers as a JSON-ready dict."""
    ctx = _Context(catalog)
    rankers = {name: make_ranker(name) for name in ranker_names}
    table: dict[str, dict] = {}
    for name, ranker in rankers.items():
        per_query = {m: np.full(len(queries), np.nan) for m in METRICS}
        recommended = np.zeros((len(queries), len(catalog)), dtype=bool)
        for i, query in enumerate(queries):
            results = ranker.rank(query.preference(k), catalog)
            values, rows = query_metrics(ctx, query, results, k)
            for metric, value in values.items():
                per_query[metric][i] = value
            recommended[i, rows] = True
        table[name] = {}
        for split in SPLITS:
            index = np.flatnonzero([q.split == split for q in queries])
            rng = np.random.default_rng(BOOTSTRAP_SEED)
            draws = rng.integers(len(index), size=(resamples, len(index)))
            scores = {}
            for metric in METRICS:
                if metric == "coverage":
                    # Resampling queries only ever shrinks the union, so a
                    # bootstrap interval would be biased low: report the point.
                    point = float(recommended[index].any(axis=0).mean())
                    triple = (point, float("nan"), float("nan"))
                else:
                    triple = _interval(per_query[metric][index], draws)
                scores[metric] = {
                    "mean": _round(triple[0]),
                    "lo": _round(triple[1]),
                    "hi": _round(triple[2]),
                }
            table[name][split] = scores
    return {
        "k": k,
        "bootstrap_resamples": resamples,
        "queries": {s: sum(q.split == s for q in queries) for s in SPLITS},
        "queries_with_axis_likes": sum(
            any(t.startswith(("more:", "less:")) for t in q.likes) for q in queries
        ),
        "catalog_tracks": len(catalog),
        "catalog_mean_popularity": _round(float(ctx.percentile.mean())),
        "rankers": table,
    }


# --- catalog version and report ----------------------------------------------


def catalog_version(manifest_path: Path | None, catalog: Catalog) -> dict:
    """What the report says about the catalog, from ingest's manifest if present."""
    info: dict = {"dataset": DATASET_VERSION, "tracks": len(catalog)}
    if manifest_path is not None and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        info["ingest_version"] = manifest.get("ingest_version")
        info["source_sha1"] = manifest.get("source_sha1")
        info["subset"] = manifest.get("subset")
    return info


LIMITS = """\
These numbers come from proxies, not listeners. Read them with the following in mind.

- **Genre labels are a proxy for relevance, not ground truth.** A result counts as
  relevant when it shares the seed's top genre. That rewards obvious picks, says
  nothing about whether a listener enjoys a result, and cannot show that anything
  was discovered. A ranker that returns the same sound-alikes every time scores well.
  The `popular` reference ranker returns only the seed's genre, so it scores 1.0 on
  genre precision by construction.
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


def render_report(
    results: dict, version: dict, report_date: date, queries_path: str
) -> str:
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
    lines += ["## Limits", "", LIMITS]
    return "\n".join(lines)


def run(
    catalog: Catalog,
    queries_path: Path,
    ranker_names: Sequence[str],
    out_dir: Path,
    report_date: date,
    manifest_path: Path | None = None,
) -> tuple[Path, Path]:
    """Evaluate and write `offline-<date>.md` and `.json`; return their paths."""
    queries = load_queries(queries_path, catalog)
    results = evaluate(catalog, queries, list(dict.fromkeys(ranker_names)))
    version = catalog_version(manifest_path, catalog)
    results["catalog"] = version
    results["queries_sha1"] = hashlib.sha1(queries_path.read_bytes()).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"offline-{report_date.isoformat()}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    md_path.write_text(
        render_report(results, version, report_date, queries_path.as_posix())
    )
    return md_path, json_path
