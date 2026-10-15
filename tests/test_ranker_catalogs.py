"""Every ranker copes with catalogs that lack or have gaps in their metadata."""

import numpy as np
import pytest
from test_evaluation import make_frame

from musicdiscovery.catalog import Catalog
from musicdiscovery.hybrid import HybridConfig, HybridRanker
from musicdiscovery.preference import Preference
from musicdiscovery.profile_ranker import ProfileRanker
from musicdiscovery.ranking import BaselineRanker

RANKERS = {
    "baseline": BaselineRanker,
    "hybrid": HybridRanker,
    "profile": ProfileRanker,
    "mmr": lambda: HybridRanker(HybridConfig(mmr_lambda=0.8, genre_redundancy=0.5)),
}


def frames():
    full = make_frame()
    gaps = full.copy()
    gaps["title"] = gaps["title"].astype(object)
    gaps.loc[gaps["track_id"] % 3 == 0, "title"] = np.nan
    gaps.loc[gaps["track_id"] % 3 == 1, "title"] = None
    gaps.loc[gaps["track_id"] % 7 == 0, "genre_top"] = None
    return {"full": full, "no_title": full.drop(columns="title"), "gaps": gaps}


@pytest.mark.parametrize("catalog_name", list(frames()))
@pytest.mark.parametrize("ranker_name", list(RANKERS))
def test_returns_k_unseeded_tracks_with_titles(catalog_name, ranker_name):
    catalog = Catalog(frames()[catalog_name])
    ranker = RANKERS[ranker_name]()
    for seeds, likes, k in [
        ([100], [], 6),
        ([161], ["more:energy"], 4),
        ([230, 255], ["tag:loud", "less:energy"], 3),
    ]:
        results = ranker.rank(Preference.from_strings(seeds, likes, k=k), catalog)
        assert len(results) == k
        assert not {r.track_id for r in results} & set(seeds)
        assert all(r.title.strip() and r.title not in {"nan", "None"} for r in results)
