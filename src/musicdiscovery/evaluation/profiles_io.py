"""Read and write the listener profile file of the held-out preference benchmark.

The profiles themselves are generated in `musicdiscovery.profiles`.
"""

import json
from collections.abc import Sequence
from pathlib import Path

from musicdiscovery import paths
from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation.queries import K, refuse_other_catalog
from musicdiscovery.profiles import PROFILE_SEED, SPLITS, Interaction, Profile
from musicdiscovery.rankers import EvaluationError

DEFAULT_PROFILES = Path("eval") / "profiles.json"


def write_profiles(path: Path, profiles: Sequence[Profile], catalog: Catalog) -> None:
    """Write profiles; refuse to replace a file made for a different catalog size."""
    path = paths.confined(path, EvaluationError)
    refuse_other_catalog(path, catalog, "profiles")
    payload = {
        "profile_seed": PROFILE_SEED,
        "k": K,
        "catalog_tracks": len(catalog),
        "profiles": [
            {
                "id": p.id,
                "split": p.split,
                "taste": {"genre": p.genre, "tags": list(p.tags), "axis": p.axis},
                "history": [[i.track, i.strength] for i in p.history],
                "heldout": [[i.track, i.strength] for i in p.heldout],
            }
            for p in profiles
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")


def _profile(record: dict) -> Profile:
    taste = record["taste"]
    return Profile(
        id=int(record["id"]),
        split=str(record["split"]),
        genre=str(taste["genre"]),
        tags=tuple(str(t) for t in taste["tags"]),
        axis=None if taste["axis"] is None else str(taste["axis"]),
        history=tuple(Interaction(int(t), float(s)) for t, s in record["history"]),
        heldout=tuple(Interaction(int(t), float(s)) for t, s in record["heldout"]),
    )


def load_profiles(path: Path, catalog: Catalog) -> list[Profile]:
    """Read profiles and check that every interaction is a catalog track."""
    path = paths.confined(path, EvaluationError)
    if not path.exists():
        raise EvaluationError(
            f"no profiles at {path}; create them with `musicdiscovery evaluate "
            "--generate-profiles`"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        profiles = [_profile(record) for record in payload["profiles"]]
        expected = payload["catalog_tracks"]
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        raise EvaluationError(f"cannot read profiles at {path}: {error!r}") from error
    if expected != len(catalog):
        raise EvaluationError(
            f"profiles were made for a catalog of {expected} tracks, "
            f"this one has {len(catalog)}"
        )
    for split in SPLITS:
        if not any(p.split == split for p in profiles):
            raise EvaluationError(f"profiles have no {split} split")
    if any(p.split not in SPLITS for p in profiles):
        raise EvaluationError(f"profile splits must be one of {SPLITS}")
    for profile in profiles:
        missing = [
            i.track
            for i in (*profile.history, *profile.heldout)
            if i.track not in catalog.position
        ]
        if missing:
            raise EvaluationError(
                f"profile {profile.id} names tracks missing from the catalog: {missing}"
            )
    return profiles
