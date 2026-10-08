"""Seed and seed-artist exclusion is shared by all four rankers."""

import pytest
from test_evaluation import make_frame

from musicdiscovery.catalog import Catalog
from musicdiscovery.evaluation import PopularRanker, RandomRanker
from musicdiscovery.hybrid import HybridRanker
from musicdiscovery.preference import Preference
from musicdiscovery.ranking import BaselineRanker, _allowed

RANKERS = [BaselineRanker, HybridRanker, RandomRanker, PopularRanker]
SEEDS = (100, 101, 160)


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_frame())


def artist_of(catalog, track_id):
    return catalog.frame.set_index("track_id")["artist"][track_id]


@pytest.mark.parametrize("ranker", RANKERS)
def test_multiple_seeds_absent(catalog, ranker):
    results = ranker().rank(Preference(SEEDS, k=500), catalog)
    assert not {r.track_id for r in results} & set(SEEDS)


@pytest.mark.parametrize("ranker", RANKERS)
def test_artist_exclusion_toggle(catalog, ranker):
    seed_artists = {artist_of(catalog, s) for s in SEEDS}
    kept = ranker().rank(Preference(SEEDS, k=500), catalog)
    assert any(r.artist in seed_artists for r in kept)
    dropped = ranker().rank(Preference(SEEDS, k=500, exclude_seed_artist=True), catalog)
    assert not any(r.artist in seed_artists for r in dropped)


@pytest.mark.parametrize("ranker", RANKERS)
def test_all_seed_artist_catalog_is_empty(ranker):
    frame = make_frame().assign(artist="solo")
    results = ranker().rank(
        Preference((100,), k=10, exclude_seed_artist=True), Catalog(frame)
    )
    assert results == []


def test_mask_is_fresh_boolean(catalog):
    pref = Preference(SEEDS, exclude_seed_artist=True)
    first = _allowed(pref, catalog)
    assert first.dtype == bool
    first[:] = False
    second = _allowed(pref, catalog)
    assert second is not first and second.any()


def test_popular_keeps_first_seed_genre_restriction(catalog):
    results = PopularRanker().rank(Preference((100, 160), k=500), catalog)
    genres = catalog.frame.set_index("track_id")["genre_top"]
    assert {genres[r.track_id] for r in results} == {genres[100]}
    again = PopularRanker().rank(Preference((100, 160), k=500), catalog)
    assert [r.track_id for r in again] == [r.track_id for r in results]
