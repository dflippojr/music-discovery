"""Ranker output pinned to lists captured before the rankers read fields by column.

`ranker_golden.json` holds the full recommendation lists that `baseline`,
`hybrid` (shipped config and a strong-MMR config) and `profile` returned for
fixed queries on three synthetic catalogs: the fixture catalog, the same with no
`title` column, and one with missing title (NaN and None) and genre values.

Ids, titles, artists, term names, term texts and explanations must match
exactly. Floats are compared to 1e-12: the metadata TF-IDF builds its
vocabulary from sets, so their last bit already varies with `PYTHONHASHSEED`.
"""

import json
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pytest
from test_evaluation import make_frame

from musicdiscovery.catalog import Catalog
from musicdiscovery.hybrid import HybridConfig, HybridRanker
from musicdiscovery.preference import Preference
from musicdiscovery.profile_ranker import ProfileRanker
from musicdiscovery.ranking import BaselineRanker

GOLDEN = json.loads((Path(__file__).parent / "ranker_golden.json").read_text())

QUERIES = [
    ([100], [], 6),
    ([161], ["more:energy"], 4),
    ([230, 255], ["tag:loud", "less:energy"], 3),
]

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


@pytest.fixture(scope="module")
def catalogs():
    return {name: Catalog(frame) for name, frame in frames().items()}


def approx_floats(value):
    if isinstance(value, float):
        return pytest.approx(value, rel=1e-12, abs=1e-12)
    if isinstance(value, dict):
        return {k: approx_floats(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [approx_floats(v) for v in value]
    return value


def as_lists(value):
    if isinstance(value, dict):
        return {k: as_lists(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [as_lists(v) for v in value]
    return value


@pytest.mark.parametrize("catalog_name", ["full", "no_title", "gaps"])
@pytest.mark.parametrize("ranker_name", list(RANKERS))
def test_matches_captured_lists(catalogs, catalog_name, ranker_name):
    ranker = RANKERS[ranker_name]()
    catalog = catalogs[catalog_name]
    for i, (seeds, likes, k) in enumerate(QUERIES):
        results = ranker.rank(Preference.from_strings(seeds, likes, k=k), catalog)
        expected = GOLDEN[f"{catalog_name}/{ranker_name}/{i}"]
        assert as_lists([asdict(r) for r in results]) == approx_floats(expected)


def test_golden_covers_missing_titles_and_variety():
    recs = [r for results in GOLDEN.values() for r in results]
    titles = {r["title"] for r in recs}
    assert {"", "nan", "None"} <= titles
    assert any(t["name"] == "variety" for r in recs for t in r["terms"])
    assert len(GOLDEN) == len(RANKERS) * len(QUERIES) * 3
