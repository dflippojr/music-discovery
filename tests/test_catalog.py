"""Values on the catalog that the rankers and reports all share."""

import numpy as np
import pandas as pd

from musicdiscovery.catalog import Catalog


def test_popularity_is_the_listen_percentile_with_ties_and_gaps():
    frame = pd.DataFrame(
        {
            "track_id": [3, 1, 2, 4, 5, 6],
            "artist": ["a", "b", "c", "d", "e", "f"],
            "genre_top": ["Folk", "Rock", None, "Jazz", "Folk", "Rock"],
            "tags": [["x"], [], ["y"], ["x"], [], ["z"]],
            "listens": [10, 5, np.nan, 5, 20, 10],
            "mfcc_mean_00": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        }
    )
    catalog = Catalog(frame)

    # The expression the rankers and reports used before Catalog.popularity.
    old = catalog.frame["listens"].rank(pct=True).to_numpy(dtype=np.float64)
    np.testing.assert_array_equal(catalog.popularity, old)
    # Tied listens share their average rank; a missing count stays nan.
    np.testing.assert_allclose(catalog.popularity, [0.3, np.nan, 0.7, 0.3, 1.0, 0.7])
