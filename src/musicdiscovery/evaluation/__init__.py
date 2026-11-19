"""Offline evaluation: stored queries, metrics and the report.

The package keeps one job per module:

- `queries`: generating the synthetic listener queries and their file;
- `profiles_io`: the listener profile file of the held-out preference benchmark;
- `metrics`: the metrics, the held-out tag split and the bootstrap interval;
- `benchmark`: the two benchmark loops, `evaluate` and `evaluate_profiles`;
- `report`: the catalog version, the Markdown report and `run`.

The rankers it scores, the reference rankers included, are in
`musicdiscovery.rankers`. Every public name of the old `evaluation` module is
re-exported here, so `from musicdiscovery.evaluation import ...` keeps working.
"""

from musicdiscovery.evaluation.benchmark import evaluate, evaluate_profiles
from musicdiscovery.evaluation.metrics import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    HOLDOUT_FRACTION,
    HOLDOUT_SEED,
    METRIC_NOTES,
    METRICS,
    MIN_TAGS_FOR_HOLDOUT,
    PROFILE_METRICS,
    Context,
    HeldOut,
    nanmean,
    query_metrics,
)
from musicdiscovery.evaluation.profiles_io import (
    DEFAULT_PROFILES,
    load_profiles,
    write_profiles,
)
from musicdiscovery.evaluation.queries import (
    DEFAULT_QUERIES,
    QUERIES_PER_SPLIT,
    QUERY_SEED,
    K,
    Query,
    generate_queries,
    load_queries,
    write_queries,
)
from musicdiscovery.evaluation.report import (
    BENCHMARKS,
    DATASET_VERSION,
    DEFAULT_REPORT_DIR,
    LIMITS,
    MIN_SCORED_QUERIES,
    PROFILE_LIMITS,
    catalog_version,
    render_profiles_section,
    render_report,
    run,
)
from musicdiscovery.rankers import (
    RANKERS,
    EvaluationError,
    PopularRanker,
    RandomRanker,
    make_ranker,
)

__all__ = [
    "BENCHMARKS",
    "BOOTSTRAP_RESAMPLES",
    "BOOTSTRAP_SEED",
    "DATASET_VERSION",
    "DEFAULT_PROFILES",
    "DEFAULT_QUERIES",
    "DEFAULT_REPORT_DIR",
    "HOLDOUT_FRACTION",
    "HOLDOUT_SEED",
    "K",
    "LIMITS",
    "METRICS",
    "METRIC_NOTES",
    "MIN_SCORED_QUERIES",
    "MIN_TAGS_FOR_HOLDOUT",
    "PROFILE_LIMITS",
    "PROFILE_METRICS",
    "QUERIES_PER_SPLIT",
    "QUERY_SEED",
    "RANKERS",
    "Context",
    "EvaluationError",
    "HeldOut",
    "PopularRanker",
    "Query",
    "RandomRanker",
    "catalog_version",
    "evaluate",
    "evaluate_profiles",
    "generate_queries",
    "load_profiles",
    "load_queries",
    "make_ranker",
    "nanmean",
    "query_metrics",
    "render_profiles_section",
    "render_report",
    "run",
    "write_profiles",
    "write_queries",
]
