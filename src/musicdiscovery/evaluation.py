"""Offline evaluation: reference rankers, stored queries, metrics and the report."""

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from musicdiscovery.catalog import Catalog
from musicdiscovery.hybrid import HybridRanker
from musicdiscovery.preference import AXES, Preference, Quality
from musicdiscovery.ranking import BaselineRanker, Ranker, Recommendation, _allowed

K = 10
QUERY_SEED = 20261005
BOOTSTRAP_SEED = 7
HOLDOUT_FRACTION = 0.5
HOLDOUT_SEED = 20261006
BOOTSTRAP_RESAMPLES = 1000
QUERIES_PER_SPLIT = 100
SPLITS = ("validation", "test")
DATASET_VERSION = "FMA dataset v1 (archive files dated 2017-04-01), small subset"
DEFAULT_QUERIES = Path("eval") / "queries.json"
DEFAULT_REPORT_DIR = Path("reports")


class EvaluationError(Exception):
    """Raised when queries or rankers cannot be used."""


def confined(path: Path) -> Path:
    """Resolve a user-supplied path and keep it inside the working directory."""
    resolved = (Path.cwd() / path).resolve()
    if not resolved.is_relative_to(Path.cwd().resolve()):
        raise EvaluationError(f"{path} is outside the working directory")
    return resolved


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
    "hybrid": HybridRanker,
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
    path = confined(path)
    payload = {
        "query_seed": QUERY_SEED,
        "k": K,
        "catalog_tracks": len(catalog),
        "queries": [asdict(q) for q in queries],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")


def load_queries(path: Path, catalog: Catalog) -> list[Query]:
    path = confined(path)
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
    "genre_precision": "Same-genre rate@10 (genre precision)",
    "genre_ndcg": "Same-genre nDCG@10 (genre nDCG)",
    "tag_overlap": "Tag overlap@10 (mean Jaccard with the seed)",
    "heldout_tags": "Held-out tag overlap@10 (mean Jaccard of hidden tags)",
    "adherence": "Preference adherence@10",
    "artist_diversity": "Artist diversity (distinct artists / list length)",
    "popularity": "Popularity (mean listen percentile of results)",
    "coverage": "Catalog coverage",
}


# Which rankers can see the label each metric is scored against.
METRIC_NOTES: dict[str, str] = {
    "genre_precision": (
        "Scored on `genre_top`: `popular` filters on it and `hybrid` scores on it "
        "through genre ids and tags, so they see the label; `baseline` (audio "
        "features only) and `random` do not."
    ),
    "genre_ndcg": (
        "Scored on `genre_top`; the same rankers see the label as for the "
        "same-genre rate."
    ),
    "tag_overlap": (
        "Scored on full tag sets: `hybrid` scores on tags, and a liked `tag:` "
        "quality can name one of the seed's tags, so these rankers see the label."
    ),
    "heldout_tags": (
        "Rankers see only a visible half of each track's tags; the score uses "
        "the hidden half, which no ranker sees. Queries whose seed has fewer "
        "than two tags are skipped."
    ),
}

MIN_TAGS_FOR_HOLDOUT = 2


class HeldOut:
    """A seeded split of every track's tags into visible and held-out halves.

    Each track's split depends only on the seed and its track id, so it does not
    change with row order. Tracks with fewer than two tags keep everything
    visible and have no held-out tags.
    """

    def __init__(
        self,
        catalog: Catalog,
        fraction: float = HOLDOUT_FRACTION,
        seed: int = HOLDOUT_SEED,
    ):
        if not 0 < fraction < 1:
            raise EvaluationError(
                f"hold-out fraction must be in (0, 1), got {fraction}"
            )
        self.fraction = fraction
        self.seed = seed
        self.catalog = catalog
        self.visible: list[frozenset] = []
        self.held: list[frozenset] = []
        for track_id, tags in zip(
            catalog.track_ids, catalog.frame["tags"], strict=True
        ):
            ordered = sorted(tags)
            if len(ordered) < MIN_TAGS_FOR_HOLDOUT:
                self.visible.append(frozenset(ordered))
                self.held.append(frozenset())
                continue
            rng = np.random.default_rng([seed, int(track_id)])
            order = rng.permutation(len(ordered))
            hidden = min(max(round(len(ordered) * fraction), 1), len(ordered) - 1)
            self.held.append(frozenset(ordered[i] for i in order[:hidden]))
            self.visible.append(frozenset(ordered[i] for i in order[hidden:]))

    def view(self) -> Catalog:
        """The catalog as rankers see it: visible tags only."""
        frame = self.catalog.frame.copy()
        frame["tags"] = pd.Series(
            [sorted(tags) for tags in self.visible], index=frame.index, dtype=object
        )
        return Catalog(frame)

    def preference(self, query: "Query", k: int) -> Preference:
        """The query's preference without a liked tag the ranker may not see."""
        row = self.catalog.position[query.seed]
        likes = [
            text
            for text in query.likes
            if not text.lower().startswith("tag:")
            or text.partition(":")[2].strip() in self.visible[row]
        ]
        return Preference.from_strings([query.seed], likes, k=k)

    def overlap(self, seed_row: int, rows: np.ndarray) -> float:
        """Mean Jaccard of held-out tags with the seed's; nan if undefined."""
        seed_held = self.held[seed_row]
        if not seed_held or not len(rows):
            return float("nan")
        return float(np.mean([_jaccard(seed_held, self.held[r]) for r in rows]))


class Context:
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
    ctx: Context, query: Query, results: Sequence[Recommendation], k: int = K
) -> tuple[dict[str, float], np.ndarray]:
    """Per-query metric values (nan where undefined) and the recommended rows."""
    catalog = ctx.catalog
    rows = np.array([catalog.position[r.track_id] for r in results], dtype=int)
    seed_row = catalog.position[query.seed]
    nan = float("nan")
    out = dict.fromkeys(METRICS, nan)
    out.pop("coverage")
    out.pop("heldout_tags")  # needs the hold-out; scored in `evaluate`
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
    holdout_fraction: float = HOLDOUT_FRACTION,
    holdout_seed: int = HOLDOUT_SEED,
) -> dict:
    """Score each ranker on every query; return the numbers as a JSON-ready dict."""
    ctx = Context(catalog)
    holdout = HeldOut(catalog, holdout_fraction, holdout_seed)
    view = holdout.view()
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
        # Held-out tags: the ranker gets the visible-tags view and nothing else.
        blind = make_ranker(name)
        for i, query in enumerate(queries):
            results = blind.rank(holdout.preference(query, k), view)
            rows = np.array([view.position[r.track_id] for r in results], dtype=int)
            per_query["heldout_tags"][i] = holdout.overlap(
                catalog.position[query.seed], rows
            )
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
        "heldout": {
            "fraction": holdout_fraction,
            "seed": holdout_seed,
            "queries_skipped": {
                split: sum(
                    q.split == split and not holdout.held[catalog.position[q.seed]]
                    for q in queries
                )
                for split in SPLITS
            },
        },
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
  same-genre when it shares the seed's top genre. That rewards obvious picks, says
  nothing about whether a listener enjoys a result, and cannot show that anything
  was discovered. A ranker that returns the same sound-alikes every time scores well.
  The `popular` reference ranker returns only the seed's genre, so it scores 1.0 on
  the same-genre rate by construction, and `hybrid` scores on genre ids and tags.
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
        f"Held-out tags: {results['heldout']['fraction']:.0%} of each track's tags "
        f"are hidden from the rankers, split with seed {results['heldout']['seed']}. "
        "Queries skipped because the seed has fewer than two tags: "
        f"{results['heldout']['queries_skipped']['validation']} validation, "
        f"{results['heldout']['queries_skipped']['test']} test.",
        "",
        *_sparsity_note(results),
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
    queries_path = confined(queries_path)
    out_dir = confined(out_dir)
    queries = load_queries(queries_path, catalog)
    results = evaluate(catalog, queries, list(dict.fromkeys(ranker_names)))
    version = catalog_version(manifest_path, catalog)
    results["catalog"] = version
    results["queries_sha256"] = hashlib.sha256(queries_path.read_bytes()).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"offline-{report_date.isoformat()}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    md_path.write_text(
        render_report(
            results,
            version,
            report_date,
            queries_path.relative_to(Path.cwd().resolve()).as_posix(),
        )
    )
    return md_path, json_path
