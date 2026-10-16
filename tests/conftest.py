"""Hypothesis profiles: `ci` (the default) is derandomized and bounded; `deep`
searches far more cases. Pick one with HYPOTHESIS_PROFILE=deep."""

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=60,
    derandomize=True,
    database=None,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "deep",
    max_examples=2000,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
