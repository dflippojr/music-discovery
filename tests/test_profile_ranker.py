"""Profile ranker and its training, on a synthetic catalog with planted structure."""

import json
from pathlib import Path

import numpy as np
import pytest
from test_evaluation import make_frame

from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main
from musicdiscovery.evaluation import write_profiles
from musicdiscovery.hybrid import HybridConfig
from musicdiscovery.preference import Preference, PreferenceError
from musicdiscovery.profile_ranker import (
    FEATURES,
    TEXTS,
    ProfileDiverseRanker,
    ProfileModel,
    ProfileRanker,
    ProfileRankerError,
    text_documents,
    weighted_centre,
    words,
)
from musicdiscovery.profile_training import fit_logistic, train, training_set
from musicdiscovery.profile_training import main as train_main
from musicdiscovery.profiles import Interaction, Profile, generate_profiles
from musicdiscovery.ranking import Ranker


def make_titled_frame(per_genre=100):
    """The evaluation fixture, with titles and albums that share words by genre."""
    frame = make_frame(per_genre=per_genre)
    frame["title"] = [
        f"{g} song {t}"
        for g, t in zip(frame["genre_top"], frame["track_id"], strict=True)
    ]
    frame["album"] = [f"{g.lower()} album" for g in frame["genre_top"]]
    return frame


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_titled_frame())


@pytest.fixture(scope="module")
def profiles(catalog):
    return generate_profiles(catalog, per_split=10)


MODEL = ProfileModel({"audio": 1.0, "metadata": 1.0, "text": 1.0, "popularity": 0.1})


def test_interface_shipped_model_and_explanations(catalog):
    ranker: Ranker = ProfileRanker()
    assert set(ranker.model.weights) == set(FEATURES)
    results = ranker.rank(Preference((100, 101), k=10), catalog)
    assert len(results) == 10
    for result in results:
        assert 1 <= len(result.explanations) <= 3
        assert {t.name for t in result.terms} == set(FEATURES)
        assert set(result.explanations) <= set(TEXTS.values())
        assert result.score == pytest.approx(sum(t.value for t in result.terms))
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_same_genre_history_finds_same_genre_tracks(catalog):
    folk = catalog.frame.loc[catalog.frame["genre_top"] == "Folk", "track_id"]
    seeds = tuple(int(t) for t in folk[:5])
    results = ProfileRanker(MODEL).rank(Preference(seeds, k=10), catalog)
    genres = catalog.frame.set_index("track_id")["genre_top"]
    assert all(genres[r.track_id] == "Folk" for r in results)


def test_quality_terms_are_added(catalog):
    plain = ProfileRanker(MODEL).rank(Preference((100,), k=5), catalog)
    liked = ProfileRanker(MODEL).rank(
        Preference.from_strings([100], likes=["genre:Rock"], k=5), catalog
    )
    assert "genre:Rock" in {t.name for t in liked[0].terms}
    assert "genre:Rock" not in {t.name for t in plain[0].terms}


def test_weighted_centre_follows_strengths():
    vectors = np.array([[1.0, 0.0], [0.0, 1.0]])
    assert weighted_centre(vectors, [1, 1]) == pytest.approx([0.5, 0.5])
    assert weighted_centre(vectors, [3, 1]) == pytest.approx([0.75, 0.25])
    with pytest.raises(ProfileRankerError, match="at least one"):
        weighted_centre(np.empty((0, 2)), [])
    with pytest.raises(ProfileRankerError, match="weights for"):
        weighted_centre(vectors, [1.0])
    with pytest.raises(ProfileRankerError, match="positive"):
        weighted_centre(vectors, [1.0, 0.0])


def test_strengths_move_the_ranking(catalog):
    genres = catalog.frame.set_index("track_id")["genre_top"]
    folk = int(genres[genres == "Folk"].index[0])
    rock = int(genres[genres == "Rock"].index[0])
    ranker = ProfileRanker(MODEL)

    def top_genre(strengths):
        preference = Preference((folk, rock), k=1, strengths=strengths)
        return genres[ranker.rank(preference, catalog)[0].track_id]

    assert top_genre((0.95, 0.05)) == "Folk"
    assert top_genre((0.05, 0.95)) == "Rock"


def test_preference_strengths_are_checked():
    assert Preference((1, 2)).seed_weights() == (1.0, 1.0)
    assert Preference((1, 2), strengths=(0.5, 1.0)).seed_weights() == (0.5, 1.0)
    with pytest.raises(PreferenceError, match="strengths for"):
        Preference((1, 2), strengths=(1.0,))
    with pytest.raises(PreferenceError, match="positive"):
        Preference((1,), strengths=(0.0,))
    with pytest.raises(PreferenceError, match="at least one seed"):
        Preference(())


def test_profile_preference_carries_play_strengths():
    profile = Profile(
        0,
        "validation",
        "Folk",
        (),
        None,
        (Interaction(1, 0.9), Interaction(2, 0.1), Interaction(3, 0.5)),
        (Interaction(4, 0.8),),
    )
    preference = profile.preference(10)
    assert preference.seeds == (1, 3)
    assert preference.strengths == (0.9, 0.5)


def test_text_features(catalog):
    assert words("The Long-Road, 2 (Live)") == {"the", "long", "road", "live"}
    assert words(None) == set()
    documents = text_documents(catalog)
    row = catalog.position[100]
    record = catalog.frame.iloc[row]
    assert words(record["title"]) <= documents[row]
    assert words(record["artist"]) <= documents[row]
    assert "album" in documents[row]
    for tag in record["tags"]:
        assert words(tag) <= documents[row]

    prepared = ProfileRanker(MODEL).prepare(catalog)
    text = prepared.features([row], [1.0])[:, FEATURES.index("text")]
    assert text[row] == pytest.approx(1.0)
    same = (catalog.frame["genre_top"] == record["genre_top"]).to_numpy()
    assert text[same].mean() > text[~same].mean()


def test_text_features_without_titles_or_albums():
    catalog = Catalog(make_frame(per_genre=10).drop(columns=["title"]))
    assert len(text_documents(catalog)) == len(catalog)
    assert len(ProfileRanker(MODEL).rank(Preference((100,), k=5), catalog)) == 5


def test_model_load_dump_and_errors(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(MODEL.dump())
    assert ProfileModel.load(path) == MODEL
    assert json.loads(MODEL.dump())["features"] == list(FEATURES)
    with pytest.raises(ProfileRankerError, match="exactly"):
        ProfileModel({"audio": 1.0})
    with pytest.raises(ProfileRankerError, match="finite"):
        ProfileModel(dict.fromkeys(FEATURES, float("nan")))
    path.write_text("not json")
    with pytest.raises(ProfileRankerError, match="cannot read"):
        ProfileModel.load(path)
    with pytest.raises(ProfileRankerError, match="cannot read"):
        ProfileModel.load(tmp_path / "missing.json")


def test_fit_logistic_separates_and_needs_both_classes():
    rng = np.random.default_rng(0)
    x = np.vstack([rng.normal(1, 1, (50, 2)), rng.normal(-1, 1, (50, 2))])
    y = np.r_[np.ones(50), np.zeros(50)]
    weights, _ = fit_logistic(x, y)
    assert (weights > 0).all()
    with pytest.raises(ProfileRankerError, match="both"):
        fit_logistic(x, np.ones(100))


def test_training_is_reproducible_and_uses_validation_only(catalog, profiles):
    first = train(catalog, profiles, negatives=20)
    second = train(catalog, profiles, negatives=20)
    assert first.dump() == second.dump()
    assert first.training["positives"] == sum(
        len(p.positives()) for p in profiles if p.split == "validation"
    )
    test_only = [p for p in profiles if p.split == "test"]
    with pytest.raises(ProfileRankerError, match="no validation"):
        training_set(catalog, test_only)
    # Same-genre text and metadata should carry weight on planted genres.
    assert first.weights["metadata"] + first.weights["text"] > 0


@pytest.fixture
def workdir(tmp_path, monkeypatch, catalog, profiles):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data" / "catalog"
    data.mkdir(parents=True)
    catalog.frame.to_parquet(data / "catalog.parquet", index=False)
    write_profiles(Path("eval/profiles.json"), profiles, catalog)
    return tmp_path


def test_training_script_writes_the_same_weights_twice(workdir, capsys):
    import musicdiscovery.profile_training as training

    (workdir / "src" / "musicdiscovery").mkdir(parents=True)
    assert train_main(["--write"]) == 0
    first = (workdir / training.MODEL_PATH).read_text()
    assert train_main(["--write"]) == 0
    assert (workdir / training.MODEL_PATH).read_text() == first
    assert set(json.loads(first)["weights"]) == set(FEATURES)
    assert '"weights"' in capsys.readouterr().out
    assert train_main(["--data-dir", "nowhere"]) == 1
    assert "train:" in capsys.readouterr().err


def test_cli_reports_a_broken_model(workdir, monkeypatch, capsys):
    import musicdiscovery.profile_ranker as module

    monkeypatch.setattr(module, "MODEL_NAME", "missing.json")
    argv = ["recommend", "--ranker", "profile", "--seed", "100"]
    assert main(argv) == 1
    assert "cannot read profile model" in capsys.readouterr().err


@pytest.fixture(scope="module")
def star_catalog():
    """The fixture with one artist on 40 Folk tracks, so the profile features
    of a history by that artist favour more of the same artist."""
    frame = make_titled_frame()
    folk = frame.index[frame["genre_top"] == "Folk"]
    frame.loc[folk[:40], "artist"] = "Star Band"
    star = tuple(int(t) for t in frame.loc[folk[:5], "track_id"])
    other = int(frame.loc[folk[45], "track_id"])
    return Catalog(frame), (*star, other)


def test_profile_diverse_spreads_artists(star_catalog):
    catalog, history = star_catalog
    preference = Preference(history, k=10)
    plain = ProfileRanker(MODEL).rank(preference, catalog)
    diverse = ProfileDiverseRanker(MODEL).rank(preference, catalog)
    assert len(diverse) == 10
    assert sum(r.artist == "Star Band" for r in plain) >= 5
    assert len({r.artist for r in diverse}) > len({r.artist for r in plain})
    for results in (plain, diverse):
        assert not {r.track_id for r in results} & set(history)
    # A pick that jumped a more relevant track says so; the first never does.
    names = [{t.name for t in r.terms} for r in diverse]
    assert "variety" not in names[0]
    assert any("variety" in n for n in names[1:])
    assert all(r.explanations for r in diverse)

    excluded = Preference(history, k=10, exclude_seed_artist=True)
    seed_artists = set(catalog.frame.set_index("track_id").loc[list(history), "artist"])
    for ranker in (ProfileRanker(MODEL), ProfileDiverseRanker(MODEL)):
        results = ranker.rank(excluded, catalog)
        assert len(results) == 10
        assert not {r.artist for r in results} & seed_artists


def test_profile_diverse_without_lambda_matches_profile(star_catalog):
    catalog, history = star_catalog
    preference = Preference(history, k=10)
    config = HybridConfig(mmr_lambda=0.0, genre_redundancy=0.0)
    plain = ProfileRanker(MODEL).rank(preference, catalog)
    same = ProfileDiverseRanker(MODEL, config).rank(preference, catalog)
    assert [r.track_id for r in same] == [r.track_id for r in plain]
    assert [r.score for r in same] == pytest.approx([r.score for r in plain])


def test_profile_diverse_uses_the_hybrid_mmr_settings(catalog):
    ranker = ProfileDiverseRanker(MODEL)
    assert ranker.config == HybridConfig.load()
    small = ProfileDiverseRanker(MODEL, HybridConfig(pool_size=3))
    # The pool never holds fewer than k tracks.
    assert len(small.rank(Preference((100,), k=10), catalog)) == 10
