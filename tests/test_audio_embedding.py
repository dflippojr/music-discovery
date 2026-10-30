"""Audio-embedding job and ranker, on synthetic clips and a fake encoder."""

import io
import json
import sys
import types
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest
from test_evaluation import GENRES, make_frame

from musicdiscovery import audio_embedding, cli
from musicdiscovery.audio_embedding import (
    DEFAULT_VECTORS,
    SAMPLE_RATE,
    AudioEmbeddingError,
    AudioEmbeddingRanker,
    SentenceTransformerEncoder,
    VectorStore,
    decode_clip,
    describe_vector,
    embed_archive,
    member_name,
    select_tracks,
)
from musicdiscovery.catalog import Catalog
from musicdiscovery.cli import main
from musicdiscovery.evaluation import evaluate, generate_queries, make_ranker
from musicdiscovery.ingest import sha1_of
from musicdiscovery.preference import Preference
from musicdiscovery.ranking import Ranker

DIMENSION = 8


@pytest.fixture(autouse=True)
def in_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


@pytest.fixture(scope="module")
def catalog():
    return Catalog(make_frame(per_genre=20))


def genre_vector(genre: str, rng: np.random.Generator) -> np.ndarray:
    """A clip vector near its genre's axis, so same-genre clips sound alike."""
    vector = rng.normal(scale=0.3, size=DIMENSION)
    vector[GENRES.index(genre)] += 3.0
    return vector


def store_for(catalog: Catalog, skip: tuple[int, ...] = ()) -> VectorStore:
    """Vectors for every catalog track but `skip`, clustered by top genre."""
    rng = np.random.default_rng(0)
    ids, vectors = [], []
    for track_id, genre in zip(
        catalog.track_ids, catalog.frame["genre_top"], strict=True
    ):
        if int(track_id) not in skip:
            ids.append(int(track_id))
            vectors.append(genre_vector(genre, rng))
    unit = np.array(vectors) / np.linalg.norm(vectors, axis=1, keepdims=True)
    return VectorStore(np.array(ids), unit, {"model": "fake"})


def genre_of(catalog, track_id):
    return catalog.frame.set_index("track_id")["genre_top"][track_id]


class FakeEncoder:
    """Reads the clip vector back out of the first samples of the clip."""

    name = "fake-encoder"

    def __init__(self):
        self.batches = []

    def encode_audio(self, clips):
        self.batches.append(len(clips))
        return np.array([clip[:DIMENSION] for clip in clips])

    def encode_text(self, texts):
        return np.array([np.eye(DIMENSION)[GENRES.index(t)] for t in texts])


def fake_decode(data: bytes) -> np.ndarray:
    if data == b"broken":
        raise ValueError("not audio")
    return np.frombuffer(data, dtype=np.float32)


def clip_bytes(vector: np.ndarray, seconds: float = 2.0) -> bytes:
    clip = np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)
    clip[:DIMENSION] = vector
    return clip.tobytes()


def write_archive(path: Path, catalog: Catalog, missing=(), broken=(), short=()):
    rng = np.random.default_rng(1)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fma_small/README.txt", "synthetic")
        for track_id, genre in zip(
            catalog.track_ids, catalog.frame["genre_top"], strict=True
        ):
            track_id = int(track_id)
            if track_id in missing:
                continue
            if track_id in broken:
                data = b"broken"
            else:
                seconds = 0.5 if track_id in short else 2.0
                data = clip_bytes(genre_vector(genre, rng), seconds)
            zf.writestr(member_name(track_id), data)
    return path


# --- the embedding job --------------------------------------------------------


def test_member_name_matches_the_fma_layout():
    assert member_name(2) == "fma_small/000/000002.mp3"
    assert member_name(155066) == "fma_small/155/155066.mp3"


def test_select_tracks_all_or_a_seeded_sorted_sample(catalog):
    every = select_tracks(catalog, None, 0)
    assert every == sorted(int(t) for t in catalog.track_ids)
    assert select_tracks(catalog, 10_000, 0) == every
    sample = select_tracks(catalog, 7, 3)
    assert len(sample) == 7 and sample == sorted(sample)
    assert sample == select_tracks(catalog, 7, 3)
    assert set(sample) <= set(every)
    with pytest.raises(AudioEmbeddingError, match="at least 1"):
        select_tracks(catalog, 0, 0)


def test_embed_archive_records_failures_and_resumes(tmp_path, catalog):
    archive = write_archive(
        tmp_path / "fma_small.zip", catalog, missing=(100,), broken=(101,), short=(102,)
    )
    out = tmp_path / "vectors" / "v.npz"
    encoder = FakeEncoder()
    lines = []
    result = embed_archive(
        catalog,
        archive,
        out,
        encoder,
        batch_size=4,
        checkpoint_every=10,
        expected_sha1=sha1_of(archive),
        decode=fake_decode,
        log=lines.append,
    )
    assert result.total == len(catalog) == 60
    assert result.failed == 3 and result.embedded == 57 and result.skipped == 0
    assert max(encoder.batches) <= 4 and lines
    store = VectorStore.load(out)
    assert len(store.track_ids) == 57
    assert np.allclose(np.linalg.norm(store.vectors, axis=1), 1.0)
    assert store.failures == {
        "100": "missing from archive",
        "101": "cannot decode: ValueError",
        "102": "clip shorter than 1 s",
    }
    assert store.meta["model"] == "fake-encoder"
    assert store.meta["archive_sha1"] == sha1_of(archive)
    assert not list(out.parent.glob("*.part"))

    again = embed_archive(
        catalog, archive, out, FakeEncoder(), expected_sha1=None, decode=fake_decode
    )
    assert (again.embedded, again.failed, again.skipped) == (0, 0, 60)
    assert np.array_equal(VectorStore.load(out).vectors, store.vectors)


def test_embed_archive_a_bounded_sample(tmp_path, catalog):
    archive = write_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    result = embed_archive(
        catalog,
        archive,
        out,
        FakeEncoder(),
        limit=5,
        expected_sha1=None,
        decode=fake_decode,
    )
    assert (result.embedded, result.total) == (5, 5)
    assert list(VectorStore.load(out).track_ids) == select_tracks(catalog, 5, 0)


def test_embed_archive_refuses_wrong_or_missing_inputs(tmp_path, catalog):
    archive = write_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    with pytest.raises(AudioEmbeddingError, match="no audio archive"):
        embed_archive(catalog, tmp_path / "none.zip", out, FakeEncoder())
    with pytest.raises(AudioEmbeddingError, match="outside the working directory"):
        embed_archive(catalog, archive, tmp_path.parent / "v.npz", FakeEncoder())
    with pytest.raises(AudioEmbeddingError, match="outside the working directory"):
        VectorStore.load(Path("..") / "v.npz")
    with pytest.raises(AudioEmbeddingError, match="expected"):
        embed_archive(catalog, archive, out, FakeEncoder(), expected_sha1="0" * 40)
    embed_archive(
        catalog,
        archive,
        out,
        FakeEncoder(),
        limit=2,
        expected_sha1=None,
        decode=fake_decode,
    )
    other = FakeEncoder()
    other.name = "another-model"
    with pytest.raises(AudioEmbeddingError, match="move it away"):
        embed_archive(catalog, archive, out, other, expected_sha1=None)


def test_embed_archive_refuses_a_non_positive_batch_size(tmp_path, catalog):
    archive = write_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    for batch_size in (0, -1):
        with pytest.raises(AudioEmbeddingError, match="batch_size must be at least 1"):
            embed_archive(
                catalog,
                archive,
                out,
                FakeEncoder(),
                batch_size=batch_size,
                expected_sha1=None,
                decode=fake_decode,
            )
    assert not out.exists()


def test_decode_clip_resamples_to_16k_mono():
    pytest.importorskip("av")
    rate, seconds = 44_100, 1.5
    t = np.arange(int(rate * seconds)) / rate
    tone = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(np.repeat(tone, 2).tobytes())
    clip = decode_clip(buffer.getvalue())
    assert clip.dtype == np.float32 and clip.ndim == 1
    assert abs(len(clip) - seconds * SAMPLE_RATE) < 0.05 * SAMPLE_RATE
    # Stereo is downmixed to mono, so the peak is near, not at, 0.3.
    assert 0.2 < np.abs(clip).max() < 0.5


# --- the vector cache ---------------------------------------------------------


def test_vector_store_validates_and_round_trips(tmp_path):
    with pytest.raises(AudioEmbeddingError, match="shape"):
        VectorStore(np.array([1, 2]), np.zeros((3, 4)))
    with pytest.raises(AudioEmbeddingError, match="duplicate"):
        VectorStore(np.array([1, 1]), np.zeros((2, 4)))
    with pytest.raises(AudioEmbeddingError, match="non-finite"):
        VectorStore(np.array([1]), np.full((1, 4), np.nan))
    store = VectorStore(np.array([3, 4]), np.eye(2, 4), {"model": "m"})
    path = tmp_path / "deep" / "v.npz"
    store.save(path)
    loaded = VectorStore.load(path)
    assert list(loaded.track_ids) == [3, 4]
    assert np.array_equal(loaded.vectors, store.vectors)
    assert loaded.meta == {"model": "m"} and loaded.failures == {}


def test_vector_store_load_errors(tmp_path):
    with pytest.raises(AudioEmbeddingError, match="run `musicdiscovery embed-audio`"):
        VectorStore.load(tmp_path / "missing.npz")
    bad = tmp_path / "bad.npz"
    bad.write_bytes(b"not a cache")
    with pytest.raises(AudioEmbeddingError, match="cannot read"):
        VectorStore.load(bad)
    bad.write_bytes(b"")  # numpy raises EOFError on an empty file
    with pytest.raises(AudioEmbeddingError, match="cannot read"):
        VectorStore.load(bad)


def test_vector_store_rejects_a_plain_npy_and_a_non_object_meta(tmp_path):
    npy = tmp_path / "v.npz"
    np.save(npy, np.eye(2, 6, dtype=np.float32))  # writes v.npz.npy
    (tmp_path / "v.npz.npy").replace(npy)
    with pytest.raises(AudioEmbeddingError, match="cannot read"):
        VectorStore.load(npy)
    bad_meta = tmp_path / "m.npz"
    np.savez(
        bad_meta, track_ids=np.array([1]), vectors=np.eye(1, 4), meta=np.array('["x"]')
    )
    with pytest.raises(AudioEmbeddingError, match="meta"):
        VectorStore.load(bad_meta)


# --- the encoder --------------------------------------------------------------


def test_encoder_without_the_audio_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    with pytest.raises(AudioEmbeddingError, match="audio extra"):
        SentenceTransformerEncoder()


def test_encoder_refuses_non_positive_threads(monkeypatch):
    torch = types.SimpleNamespace(set_num_threads=pytest.fail)
    monkeypatch.setitem(sys.modules, "torch", torch)
    for threads in (0, -1):
        with pytest.raises(AudioEmbeddingError, match="threads must be at least 1"):
            SentenceTransformerEncoder(threads=threads)


def test_encoder_settings_and_calls(monkeypatch):
    calls = {}

    class Model:
        def __init__(self, model_id, **kwargs):
            calls["init"] = (model_id, kwargs)

        def encode(self, inputs, **kwargs):
            calls.setdefault("encode", []).append((inputs, kwargs))
            return np.ones((len(inputs), 3))

    torch = types.SimpleNamespace(
        float32="float32", set_num_threads=lambda n: calls.update(threads=n)
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=Model),
    )
    encoder = SentenceTransformerEncoder(threads=2)
    assert encoder.name == "google/embeddinggemma-2" and calls["threads"] == 2
    _, kwargs = calls["init"]
    assert kwargs["device"] == "cpu"
    assert kwargs["model_kwargs"] == {"torch_dtype": "float32"}
    assert kwargs["config_kwargs"] == {"vision_config": None}

    clip = np.zeros(SAMPLE_RATE, dtype=np.float32)
    assert encoder.encode_audio([clip, clip]).shape == (2, 3)
    inputs, kwargs = calls["encode"][0]
    assert inputs[0]["audio"]["sampling_rate"] == SAMPLE_RATE
    assert kwargs["normalize_embeddings"] is True
    encoder.encode_text(["calm piano"])
    inputs, kwargs = calls["encode"][1]
    assert inputs == ["calm piano"] and kwargs["prompt_name"] == "SearchQuery"


# --- the ranker ---------------------------------------------------------------


def test_ranker_finds_same_genre_clips(catalog):
    ranker: Ranker = AudioEmbeddingRanker(store=store_for(catalog))
    results = ranker.rank(Preference((100,), k=10), catalog)
    assert len(results) == 10 and 100 not in {r.track_id for r in results}
    assert {genre_of(catalog, r.track_id) for r in results} == {genre_of(catalog, 100)}
    for result in results:
        assert result.terms[0].name == "embedding"
        assert result.explanations[0] == audio_embedding.SIMILARITY_TEXT
    again = ranker.rank(Preference((100,), k=10), catalog)
    assert [r.track_id for r in again] == [r.track_id for r in results]


def test_ranker_weights_seeds_by_strength(catalog):
    frame = catalog.frame
    folk = int(frame[frame["genre_top"] == "Folk"]["track_id"].iloc[0])
    rock = int(frame[frame["genre_top"] == "Rock"]["track_id"].iloc[0])
    ranker = AudioEmbeddingRanker(store=store_for(catalog))
    for strong, weak, genre in ((folk, rock, "Folk"), (rock, folk, "Rock")):
        preference = Preference((strong, weak), k=5, strengths=(1.0, 0.05))
        results = ranker.rank(preference, catalog)
        assert {genre_of(catalog, r.track_id) for r in results} == {genre}


def test_ranker_stays_inside_the_embedded_catalog(catalog):
    skip = tuple(int(t) for t in catalog.track_ids[1::2])
    ranker = AudioEmbeddingRanker(store=store_for(catalog, skip=skip))
    results = ranker.rank(Preference((100,), k=500), catalog)
    assert len(results) == len(catalog) - len(skip) - 1
    assert not {r.track_id for r in results} & set(skip)


def test_no_seed_vector_returns_nothing(catalog):
    ranker = AudioEmbeddingRanker(store=store_for(catalog, skip=(100,)))
    assert ranker.rank(Preference((100,), k=5), catalog) == []
    liked = Preference.from_strings([100], ["genre:Jazz"], k=5)
    assert ranker.rank(liked, catalog) == []
    assert ranker.seeds_without_vector(liked, catalog) == (100,)


def test_only_seeds_with_vectors_contribute(catalog):
    ranker = AudioEmbeddingRanker(store=store_for(catalog, skip=(100,)))
    other = int(catalog.track_ids[catalog.track_ids != 100][0])
    both = ranker.rank(Preference((100, other), k=5), catalog)
    alone = ranker.rank(Preference((other,), k=5), catalog)
    assert both and [r.track_id for r in both] == [r.track_id for r in alone]
    assert all(r.terms[0].value != 0.0 for r in both)


def test_description_still_ranks_when_no_seed_has_a_vector(catalog):
    description = describe_vector("Jazz", FakeEncoder())
    ranker = AudioEmbeddingRanker(
        store=store_for(catalog, skip=(100,)), description=description
    )
    results = ranker.rank(Preference((100,), k=5), catalog)
    assert len(results) == 5
    assert results[0].explanations[0] == "sounds like your description: Jazz"


def test_ranker_errors_without_vectors(catalog, tmp_path):
    empty = VectorStore(np.array([1]), np.ones((1, DIMENSION)))
    with pytest.raises(AudioEmbeddingError, match="no catalog track"):
        AudioEmbeddingRanker(store=empty).rank(Preference((100,)), catalog)
    with pytest.raises(AudioEmbeddingError, match="no audio vectors"):
        AudioEmbeddingRanker(path=tmp_path / "x.npz").rank(Preference((100,)), catalog)


def test_description_adds_a_text_to_audio_term(catalog):
    description = describe_vector("Jazz", FakeEncoder())
    ranker = AudioEmbeddingRanker(
        store=store_for(catalog), description=description, description_weight=5.0
    )
    folk_seed = int(
        catalog.frame[catalog.frame["genre_top"] == "Folk"]["track_id"].iloc[0]
    )
    results = ranker.rank(Preference((folk_seed,), k=5), catalog)
    assert {genre_of(catalog, r.track_id) for r in results} == {"Jazz"}
    assert results[0].explanations[0] == "sounds like your description: Jazz"
    with pytest.raises(AudioEmbeddingError, match="empty"):
        describe_vector("  ", FakeEncoder())
    wrong = AudioEmbeddingRanker(
        store=store_for(catalog), description=("x", np.ones(3))
    )
    with pytest.raises(AudioEmbeddingError, match="dimensions"):
        wrong.rank(Preference((100,)), catalog)


def test_evaluator_scores_the_audio_embedding_ranker(catalog):
    store_for(catalog).save(DEFAULT_VECTORS)
    assert isinstance(make_ranker("audio-embedding"), AudioEmbeddingRanker)
    queries = generate_queries(catalog, per_split=5)
    results = evaluate(catalog, queries, ["audio-embedding", "random"], resamples=10)
    audio = results["rankers"]["audio-embedding"]["test"]
    rand = results["rankers"]["random"]["test"]
    assert audio["genre_precision"]["mean"] > rand["genre_precision"]["mean"]


# --- the command line ---------------------------------------------------------


def write_catalog(catalog: Catalog) -> None:
    target = Path("data") / "catalog"
    target.mkdir(parents=True)
    catalog.frame.to_parquet(target / "catalog.parquet")


def test_recommend_with_the_audio_embedding_ranker(catalog, capsys, monkeypatch):
    write_catalog(catalog)
    store_for(catalog).save(DEFAULT_VECTORS)
    args = ["recommend", "--seed", "100", "--ranker", "audio-embedding", "-k", "3"]
    assert main([*args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 3

    monkeypatch.setattr(
        cli, "SentenceTransformerEncoder", lambda threads: FakeEncoder()
    )
    assert main([*args, "--describe", "Rock", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["terms"][1]["name"] == "description"

    assert main(["recommend", "--seed", "100", "--describe", "Rock"]) == 1
    assert "--describe needs" in capsys.readouterr().err
    assert main([*args, "--vectors", "data/none.npz"]) == 1
    assert "no audio vectors" in capsys.readouterr().err
    np.save(Path("data") / "plain.npy", np.eye(2, 6, dtype=np.float32))
    (Path("data") / "plain.npy").replace(Path("data") / "plain.npz")
    assert main([*args, "--vectors", "data/plain.npz"]) == 1
    assert "cannot read audio vectors" in capsys.readouterr().err


def test_recommend_without_a_seed_vector_says_so(catalog, capsys):
    write_catalog(catalog)
    store_for(catalog, skip=(100,)).save(DEFAULT_VECTORS)
    args = ["recommend", "--seed", "100", "--ranker", "audio-embedding"]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no seed has an audio vector (100)" in captured.err
    assert main([*args, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_embed_audio_command(catalog, capsys, monkeypatch):
    write_catalog(catalog)
    write_archive(Path("data") / "audio.zip", catalog, missing=(100,))

    def fake_embed(*args, **kwargs):
        return embed_archive(*args, **kwargs, expected_sha1=None, decode=fake_decode)

    monkeypatch.setattr(
        cli, "SentenceTransformerEncoder", lambda threads: FakeEncoder()
    )
    monkeypatch.setattr(cli, "embed_archive", fake_embed)
    command = ["embed-audio", "--archive", "data/audio.zip", "--vectors", "data/v.npz"]
    assert main([*command, "--batch-size", "2", "--threads", "1"]) == 0
    out = capsys.readouterr().out
    assert "embedded 59 of 60 tracks, 1 failed, 0 already done" in out
    assert main([*command, "--archive", "data/none.zip"]) == 1
    assert "no audio archive" in capsys.readouterr().err
    np.savez(
        Path("data") / "list-meta.npz",
        track_ids=np.array([1]),
        vectors=np.eye(1, 4),
        meta=np.array('["x"]'),
    )
    assert main([*command[:-1], "data/list-meta.npz"]) == 1
    assert "meta is not a JSON object" in capsys.readouterr().err


def test_embed_audio_rejects_non_positive_batch_size_and_threads(
    catalog, capsys, monkeypatch
):
    write_catalog(catalog)
    write_archive(Path("data") / "audio.zip", catalog)

    def fake_embed(*args, **kwargs):
        return embed_archive(*args, **kwargs, expected_sha1=None, decode=fake_decode)

    monkeypatch.setattr(
        cli, "SentenceTransformerEncoder", lambda threads: FakeEncoder()
    )
    monkeypatch.setattr(cli, "embed_archive", fake_embed)
    command = ["embed-audio", "--archive", "data/audio.zip", "--vectors", "data/v.npz"]
    recommend = ["recommend", "--seed", "100", "--ranker", "audio-embedding"]
    cases = [
        [*command, "--batch-size"],
        [*command, "--threads"],
        [*recommend, "--describe", "Rock", "--threads"],
    ]
    for args in cases:
        for value in ("0", "-1", "two"):
            with pytest.raises(SystemExit) as info:
                main([*args, value])
            assert info.value.code == 2
            err = capsys.readouterr().err
            assert f"argument {args[-1]}: '{value}'" in err
            if value != "two":
                assert "must be at least 1" in err
    assert not Path("data/v.npz").exists()
