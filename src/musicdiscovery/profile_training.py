"""Train the profile ranker's scoring head on the validation profiles."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from musicdiscovery.catalog import Catalog, CatalogError
from musicdiscovery.evaluation import (
    DEFAULT_PROFILES,
    EvaluationError,
    confined,
    load_profiles,
)
from musicdiscovery.ingest import CATALOG_NAME
from musicdiscovery.profile_ranker import (
    FEATURES,
    MODEL_NAME,
    ProfileModel,
    ProfileRanker,
    ProfileRankerError,
)
from musicdiscovery.profiles import WEAK_SIGNAL_THRESHOLD, Profile

TRAINING_SEED = 20261013
NEGATIVES_PER_PROFILE = 50
L2_PENALTY = 1.0
NEWTON_STEPS = 50
_TOLERANCE = 1e-10
MODEL_PATH = Path("src") / "musicdiscovery" / MODEL_NAME


def training_set(
    catalog: Catalog,
    profiles: Sequence[Profile],
    negatives: int = NEGATIVES_PER_PROFILE,
    seed: int = TRAINING_SEED,
    threshold: float = WEAK_SIGNAL_THRESHOLD,
) -> tuple[np.ndarray, np.ndarray]:
    """Features and labels: held-out positives against sampled catalog tracks.

    Uses validation profiles only, in id order. Negatives are drawn uniformly
    without replacement from tracks outside the profile's whole history and
    held-out set, with one generator seeded once, so the set is reproducible.
    """
    prepared = ProfileRanker(ProfileModel(dict.fromkeys(FEATURES, 0.0))).prepare(
        catalog
    )
    rng = np.random.default_rng(seed)
    rows: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    for profile in sorted(profiles, key=lambda p: p.id):
        if profile.split != "validation":
            continue
        preference = profile.preference(1, threshold)
        seed_rows = [catalog.position[t] for t in preference.seeds]
        features = prepared.features(seed_rows, preference.seed_weights())
        positive = [catalog.position[t] for t in sorted(profile.positives(threshold))]
        seen = {catalog.position[i.track] for i in (*profile.history, *profile.heldout)}
        pool = np.setdiff1d(np.arange(len(catalog)), sorted(seen))
        negative = rng.choice(pool, size=min(negatives, len(pool)), replace=False)
        rows += [features[positive], features[np.sort(negative)]]
        labels += [np.ones(len(positive)), np.zeros(len(negative))]
    if not rows:
        raise ProfileRankerError("no validation profiles to train on")
    return np.vstack(rows), np.concatenate(labels)


def fit_logistic(
    x: np.ndarray, y: np.ndarray, l2: float = L2_PENALTY, steps: int = NEWTON_STEPS
) -> tuple[np.ndarray, float]:
    """L2-penalised logistic regression by Newton's method; returns weights, bias.

    The penalty is `l2 / 2 * |weights|^2` on the summed log loss and leaves the
    bias out. Newton's method has no randomness, so the same data gives the
    same weights.
    """
    if len(set(y.tolist())) < 2:
        raise ProfileRankerError("training needs both positives and negatives")
    design = np.column_stack([x, np.ones(len(x))])
    penalty = np.full(design.shape[1], l2)
    penalty[-1] = 0.0
    theta = np.zeros(design.shape[1])
    for _ in range(steps):
        p = 1.0 / (1.0 + np.exp(-(design @ theta)))
        gradient = design.T @ (p - y) + penalty * theta
        hessian = (design * (p * (1 - p))[:, None]).T @ design + np.diag(penalty)
        step = np.linalg.solve(hessian + 1e-9 * np.eye(len(theta)), gradient)
        theta -= step
        if np.abs(step).max() < _TOLERANCE:
            break
    return theta[:-1], float(theta[-1])


def train(
    catalog: Catalog,
    profiles: Sequence[Profile],
    negatives: int = NEGATIVES_PER_PROFILE,
    seed: int = TRAINING_SEED,
    l2: float = L2_PENALTY,
) -> ProfileModel:
    x, y = training_set(catalog, profiles, negatives, seed)
    weights, bias = fit_logistic(x, y, l2)
    return ProfileModel(
        weights={name: float(w) for name, w in zip(FEATURES, weights, strict=True)},
        bias=bias,
        training={
            "split": "validation",
            "seed": seed,
            "negatives_per_profile": negatives,
            "l2_penalty": l2,
            "positives": int(y.sum()),
            "negatives": int(len(y) - y.sum()),
            "catalog_tracks": len(catalog),
        },
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--write", action="store_true", help=f"write {MODEL_PATH}")
    args = parser.parse_args(argv)
    try:
        catalog = Catalog.load(args.data_dir / "catalog" / CATALOG_NAME)
        profiles = load_profiles(args.profiles, catalog)
        model = train(catalog, profiles)
        if args.write:
            confined(MODEL_PATH).write_text(model.dump(), encoding="utf-8")
    except (CatalogError, EvaluationError, ProfileRankerError, OSError) as error:
        print(f"train: {error}", file=sys.stderr)
        return 1
    print(model.dump(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
