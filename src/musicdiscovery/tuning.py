"""Tune the hybrid ranker's weights and MMR lambda on the validation queries."""

import argparse
import itertools
import sys
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np

from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.evaluation import (
    BOOTSTRAP_RESAMPLES,
    DEFAULT_QUERIES,
    Context,
    EvaluationError,
    K,
    confined,
    evaluate,
    load_queries,
    query_metrics,
)
from musicdiscovery.hybrid import (
    CONFIG_NAME,
    HybridConfig,
    HybridConfigError,
    HybridRanker,
)
from musicdiscovery.ingest import CATALOG_NAME

GRID: dict[str, tuple[float, ...]] = {
    "metadata_weight": (0.0, 0.25, 0.5, 1.0, 2.0),
    "popularity_weight": (0.0, 0.05, 0.1),
    "mmr_lambda": (0.0, 0.1, 0.2, 0.3, 0.45, 0.6),
    "genre_redundancy": (0.0, 0.25, 0.5),
}
CONFIG_PATH = Path("src") / "musicdiscovery" / CONFIG_NAME


def _mean(values: Sequence[float]) -> float:
    valid = [v for v in values if not np.isnan(v)]
    return float(np.mean(valid)) if valid else float("nan")


def validation_means(ranker, catalog: Catalog, queries, ctx: Context) -> dict:
    """Mean of each metric over the validation queries."""
    per_metric: dict[str, list[float]] = {}
    for query in queries:
        values, _ = query_metrics(
            ctx, query, ranker.rank(query.preference(K), catalog), K
        )
        for name, value in values.items():
            per_metric.setdefault(name, []).append(value)
    return {name: _mean(values) for name, values in per_metric.items()}


def tune(
    catalog: Catalog,
    queries,
    grid: Mapping[str, Sequence[float]] = GRID,
    pool_size: int = HybridConfig().pool_size,
) -> tuple[HybridConfig, list[dict]]:
    """Pick the config that diversifies most while genre precision holds.

    Constraint: validation genre precision may fall below the baseline's by at
    most the width of the baseline's validation 95% interval. Objective among
    configs that satisfy it: genre nDCG plus artist diversity. Ties go to the
    first config in grid order, so the answer is reproducible.
    """
    validation = [q for q in queries if q.split == "validation"]
    ctx = Context(catalog)
    baseline = evaluate(catalog, queries, ["baseline"], resamples=BOOTSTRAP_RESAMPLES)
    precision = baseline["rankers"]["baseline"]["validation"]["genre_precision"]
    floor = precision["mean"] - (precision["hi"] - precision["lo"])

    rows = []
    names = list(grid)
    for values in itertools.product(*grid.values()):
        config = replace(
            HybridConfig(pool_size=pool_size), **dict(zip(names, values, strict=True))
        )
        means = validation_means(HybridRanker(config), catalog, validation, ctx)
        rows.append({"config": config, "means": means, "floor": floor})
    feasible = [r for r in rows if r["means"]["genre_precision"] >= floor]
    if not feasible:
        raise EvaluationError("no config keeps genre precision within the interval")
    best = max(
        feasible,
        key=lambda r: r["means"]["genre_ndcg"] + r["means"]["artist_diversity"],
    )
    return best["config"], rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--queries", type=Path, default=DEFAULT_QUERIES)
    parser.add_argument("--write", action="store_true", help=f"write {CONFIG_PATH}")
    args = parser.parse_args(argv)
    try:
        catalog = Catalog.load(args.data_dir / "catalog" / CATALOG_NAME)
        queries = load_queries(args.queries, catalog)
        config, rows = tune(catalog, queries)
        if args.write:
            confined(CONFIG_PATH).write_text(config.dump(), encoding="utf-8")
    except (CatalogError, EvaluationError, HybridConfigError, OSError) as error:
        print(f"tune: {error}", file=sys.stderr)
        return 1
    floor = rows[0]["floor"]
    print(f"genre precision floor on validation: {floor:.4f}")
    for row in rows:
        means, cfg = row["means"], row["config"]
        mark = "*" if cfg == config else " "
        print(
            f"{mark} meta={cfg.metadata_weight} pop={cfg.popularity_weight} "
            f"lambda={cfg.mmr_lambda} genre={cfg.genre_redundancy} "
            f"precision={means['genre_precision']:.4f} "
            f"ndcg={means['genre_ndcg']:.4f} "
            f"artists={means['artist_diversity']:.4f}"
        )
    print(config.dump(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
