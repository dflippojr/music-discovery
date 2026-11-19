"""The two benchmark loops: stored queries and held-out listener preferences."""

from collections.abc import Callable, Sequence

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation.metrics import (
    BOOTSTRAP_RESAMPLES,
    BOOTSTRAP_SEED,
    HOLDOUT_FRACTION,
    HOLDOUT_SEED,
    METRICS,
    PROFILE_METRICS,
    Context,
    HeldOut,
    bootstrap_interval,
    query_metrics,
    rounded,
)
from musicdiscovery.evaluation.queries import K, Query
from musicdiscovery.profiles import (
    PROFILE_SEED,
    SPLITS,
    WEAK_SIGNAL_THRESHOLD,
    Profile,
    ProfileError,
    ndcg,
    reciprocal_rank,
)
from musicdiscovery.rankers import EvaluationError, make_ranker
from musicdiscovery.ranking import Recommendation


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
                    triple = bootstrap_interval(per_query[metric][index], draws)
                scores[metric] = {
                    "mean": rounded(triple[0]),
                    "lo": rounded(triple[1]),
                    "hi": rounded(triple[2]),
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
        "catalog_mean_popularity": rounded(float(ctx.percentile.mean())),
        "rankers": table,
    }


def _profile_scores(
    profile: Profile, results: Sequence[Recommendation], k: int, threshold: float
) -> tuple[float, float]:
    """MRR and nDCG of one ranked list against the profile's held-out positives."""
    ranked = [r.track_id for r in results]
    seen = set(profile.seeds(threshold)) & set(ranked)
    if seen:
        raise EvaluationError(
            f"a ranker returned history tracks {sorted(seen)} for profile {profile.id}"
        )
    positives = profile.positives(threshold)
    return reciprocal_rank(ranked, positives, k), ndcg(ranked, positives, k)


def evaluate_profiles(
    catalog: Catalog,
    profiles: Sequence[Profile],
    ranker_names: Sequence[str],
    k: int = K,
    resamples: int = BOOTSTRAP_RESAMPLES,
    threshold: float = WEAK_SIGNAL_THRESHOLD,
) -> dict:
    """Rank from each profile's filtered history; score against held-out positives."""
    for profile in profiles:
        if not profile.positives(threshold):
            raise EvaluationError(
                f"profile {profile.id} has no held-out track above {threshold}"
            )
    table: dict[str, dict] = {}
    for name in ranker_names:
        ranker = make_ranker(name)
        per_profile = {m: np.full(len(profiles), np.nan) for m in PROFILE_METRICS}
        for i, profile in enumerate(profiles):
            try:
                preference = profile.preference(k, threshold)
            except ProfileError as error:
                raise EvaluationError(str(error)) from error
            results = ranker.rank(preference, catalog)
            mrr, gain = _profile_scores(profile, results, k, threshold)
            per_profile["mrr"][i] = mrr
            per_profile["ndcg"][i] = gain
        table[name] = {}
        for split in SPLITS:
            index = np.flatnonzero([p.split == split for p in profiles])
            rng = np.random.default_rng(BOOTSTRAP_SEED)
            draws = rng.integers(len(index), size=(resamples, len(index)))
            table[name][split] = {}
            for metric, values in per_profile.items():
                mean, lo, hi = bootstrap_interval(values[index], draws)
                table[name][split][metric] = {
                    "mean": rounded(mean),
                    "lo": rounded(lo),
                    "hi": rounded(hi),
                }

    def total(split: str, count: Callable[[Profile], int]) -> int:
        return sum(count(p) for p in profiles if p.split == split)

    return {
        "k": k,
        "bootstrap_resamples": resamples,
        "profile_seed": PROFILE_SEED,
        "weak_signal_threshold": threshold,
        "profiles": {s: total(s, lambda _: 1) for s in SPLITS},
        "history_tracks": {
            s: total(s, lambda p: len(p.seeds(threshold))) for s in SPLITS
        },
        "weak_history_dropped": {
            s: total(s, lambda p: len(p.history) - len(p.seeds(threshold)))
            for s in SPLITS
        },
        "positives": {
            s: total(s, lambda p: len(p.positives(threshold))) for s in SPLITS
        },
        "weak_heldout_dropped": {
            s: total(s, lambda p: len(p.heldout) - len(p.positives(threshold)))
            for s in SPLITS
        },
        "rankers": table,
    }
