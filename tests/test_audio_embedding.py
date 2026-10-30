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
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
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
BAD_SAMPLE = 99.0


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


def cancelling_store(catalog: Catalog) -> VectorStore:
    """`store_for`, with track 101's vector the negative of track 100's."""
    store = store_for(catalog)
    ids = list(store.track_ids)
    store.vectors[ids.index(101)] = -store.vectors[ids.index(100)]
    return store


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


def test_embed_archive_batch_with_only_failures(tmp_path, catalog):
    # Tracks 100 and 101 sort first, so the first batch of two holds only them.
    archive = write_archive(tmp_path / "a.zip", catalog, broken=(100,), short=(101,))
    out = tmp_path / "v.npz"
    encoder = FakeEncoder()
    checkpoints = []

    def log(line):
        checkpoints.append((line, VectorStore.load(out).failures))

    result = embed_archive(
        catalog,
        archive,
        out,
        encoder,
        batch_size=2,
        checkpoint_every=2,
        expected_sha1=None,
        decode=fake_decode,
        log=log,
    )
    assert (result.embedded, result.failed) == (58, 2)
    assert 0 not in encoder.batches
    assert len(checkpoints) == 30
    first_line, first_failures = checkpoints[0]
    assert first_line == "embedded 0 of 60, 2 failed"
    assert first_failures == {
        "100": "cannot decode: ValueError",
        "101": "clip shorter than 1 s",
    }
    again = embed_archive(
        catalog, archive, out, FakeEncoder(), expected_sha1=None, decode=fake_decode
    )
    assert (again.embedded, again.failed, again.skipped) == (0, 0, 60)


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


def test_encoder_reports_a_model_that_will_not_load(monkeypatch):
    def refuse(*_, **__):
        raise RuntimeError("corrupt\nweights")

    monkeypatch.setitem(
        sys.modules, "torch", types.SimpleNamespace(float32=0, set_num_threads=int)
    )
    monkeypatch.setitem(
        sys.modules,
        "sentence_transformers",
        types.SimpleNamespace(SentenceTransformer=refuse),
    )
    with pytest.raises(AudioEmbeddingError, match="RuntimeError: corrupt weights"):
        SentenceTransformerEncoder()


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


def test_cancelling_seed_vectors_return_nothing(catalog):
    ranker = AudioEmbeddingRanker(store=cancelling_store(catalog))
    assert ranker.rank(Preference((100, 101), k=5), catalog) == []
    assert ranker.seeds_cancel_out(Preference((100, 101), k=5), catalog)
    assert not ranker.seeds_cancel_out(Preference((100,), k=5), catalog)


def test_cancelling_seeds_with_a_description_rank_on_the_description(catalog):
    description = describe_vector("Jazz", FakeEncoder())
    ranker = AudioEmbeddingRanker(
        store=cancelling_store(catalog), description=description
    )
    results = ranker.rank(Preference((100, 101), k=5), catalog)
    assert len(results) == 5
    for result in results:
        terms = {t.name: t.value for t in result.terms}
        assert terms["embedding"] == 0.0 and terms["description"] > 0.0
        assert "sounds like your description: Jazz" in result.explanations
        assert genre_of(catalog, result.track_id) == "Jazz"
    lowest = sorted(int(t) for t in catalog.track_ids if t not in (100, 101))[:5]
    assert [r.track_id for r in results] != lowest


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


def test_recommend_says_when_seed_vectors_cancel(catalog, capsys):
    write_catalog(catalog)
    cancelling_store(catalog).save(DEFAULT_VECTORS)
    args = ["recommend", "--seed", "100", "--seed", "101"]
    assert main([*args, "--ranker", "audio-embedding"]) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "recommend: the seeds' audio vectors (100, 101) cancel out; nothing to rank on"
    ]


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


# --- a failure while embedding (#204) -----------------------------------------


class RejectingEncoder(FakeEncoder):
    """Raises on any batch that holds a clip whose first sample is `BAD_SAMPLE`."""

    def encode_audio(self, clips):
        if any(clip[0] == BAD_SAMPLE for clip in clips):
            raise RuntimeError("the model rejects this clip")
        return super().encode_audio(clips)


class DyingEncoder(FakeEncoder):
    """Encodes `clips_left` clips, then raises `error` on every call."""

    def __init__(self, clips_left, error=RuntimeError):
        super().__init__()
        self.clips_left, self.error = clips_left, error

    def encode_audio(self, clips):
        if len(clips) > self.clips_left:
            raise self.error("encoder\nfailed")
        self.clips_left -= len(clips)
        return super().encode_audio(clips)


def decode_with_bad_tracks(bad):
    """`fake_decode`, marking the clips of the tracks in `bad` for RejectingEncoder.

    `write_bad_archive` stores each track id in the clip's second sample.
    """

    def decode(data):
        clip = fake_decode(data).copy()
        if int(clip[1]) in bad:
            clip[0] = BAD_SAMPLE
        return clip

    return decode


def write_bad_archive(path: Path, catalog: Catalog) -> Path:
    rng = np.random.default_rng(1)
    with zipfile.ZipFile(path, "w") as zf:
        for track_id in catalog.track_ids:
            vector = rng.normal(size=DIMENSION)
            vector[1] = int(track_id)
            zf.writestr(member_name(int(track_id)), clip_bytes(vector, seconds=1.0))
    return path


def test_encoder_failure_keeps_finished_clips(tmp_path, monkeypatch):
    class Encoder:
        name = "stub"

        def __init__(self, fail_after):
            self.fail_after, self.seen = fail_after, 0

        def encode_audio(self, clips):
            self.seen += len(clips)
            if self.seen > self.fail_after:
                raise RuntimeError("encoder failed on a clip")
            return np.ones((len(clips), 4))

    monkeypatch.chdir(tmp_path)
    catalog = Catalog(make_frame())
    with zipfile.ZipFile("a.zip", "w") as archive:
        for track in catalog.track_ids:
            archive.writestr(member_name(int(track)), b"x")
    with pytest.raises(AudioEmbeddingError):
        embed_archive(
            catalog,
            Path("a.zip"),
            Path("v.npz"),
            Encoder(16),
            batch_size=4,
            expected_sha1=None,
            decode=lambda _: np.zeros(20_000, dtype=np.float32),
        )
    assert len(VectorStore.load(Path("v.npz")).track_ids) == 16


@pytest.mark.parametrize("batch_size", [1, 2, 4])
def test_clips_that_always_fail_are_recorded_and_the_run_completes(
    tmp_path, catalog, batch_size
):
    # 100 and 101 sit side by side, so a whole batch of two holds only bad clips.
    bad = {100, 101, 130}
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    decode = decode_with_bad_tracks(bad)
    result = embed_archive(
        catalog,
        archive,
        out,
        RejectingEncoder(),
        batch_size=batch_size,
        expected_sha1=None,
        decode=decode,
    )
    assert (result.embedded, result.failed, result.total) == (57, 3, 60)
    store = VectorStore.load(out)
    assert store.failures == {
        str(track): "cannot encode: RuntimeError" for track in bad
    }
    assert not bad & {int(t) for t in store.track_ids}
    again = embed_archive(
        catalog, archive, out, RejectingEncoder(), expected_sha1=None, decode=decode
    )
    assert (again.embedded, again.failed, again.skipped) == (0, 0, 60)


@pytest.mark.parametrize(
    "output",
    [
        pytest.param(lambda clips: np.full((len(clips), 4), np.nan), id="non-finite"),
        pytest.param(lambda clips: np.ones((len(clips) + 1, 4)), id="too-many-rows"),
        pytest.param(lambda clips: np.ones(len(clips)), id="one-dimension"),
    ],
)
def test_unusable_encoder_output_never_reaches_the_cache(tmp_path, catalog, output):
    class BadOutput(FakeEncoder):
        def encode_audio(self, clips):
            if any(clip[0] == BAD_SAMPLE for clip in clips):
                return output(clips)
            return super().encode_audio(clips)

    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    result = embed_archive(
        catalog,
        archive,
        out,
        BadOutput(),
        batch_size=4,
        expected_sha1=None,
        decode=decode_with_bad_tracks({102}),
    )
    assert (result.embedded, result.failed) == (59, 1)
    assert VectorStore.load(out).failures == {"102": "cannot encode: ValueError"}


def test_a_vector_of_another_size_never_reaches_the_cache(tmp_path, catalog):
    class OtherSize(RejectingEncoder):
        def encode_audio(self, clips):
            if any(clip[0] == BAD_SAMPLE for clip in clips):
                return np.ones((len(clips), DIMENSION + 1))
            return super().encode_audio(clips)

    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    result = embed_archive(
        catalog,
        archive,
        out,
        OtherSize(),
        batch_size=1,
        expected_sha1=None,
        decode=decode_with_bad_tracks({102}),
    )
    assert (result.embedded, result.failed) == (59, 1)
    assert VectorStore.load(out).failures == {"102": "cannot encode: ValueError"}


@pytest.mark.parametrize("site", ["encoder", "decoder", "log"])
def test_ctrl_c_anywhere_leaves_the_finished_clips(tmp_path, catalog, site):
    archive = write_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    decoded = []

    def decode(data):
        decoded.append(data)
        if site == "decoder" and len(decoded) == 7:
            raise KeyboardInterrupt
        return fake_decode(data)

    def log(_):
        if site == "log":
            raise KeyboardInterrupt

    encoder = DyingEncoder(6, KeyboardInterrupt) if site == "encoder" else FakeEncoder()
    with pytest.raises(KeyboardInterrupt):
        embed_archive(
            catalog,
            archive,
            out,
            encoder,
            batch_size=2,
            checkpoint_every=4,
            expected_sha1=None,
            decode=decode,
            log=log,
        )
    # Two batches reach the checkpoint, whose log line is the "log" site's Ctrl+C.
    assert len(VectorStore.load(out).track_ids) == (4 if site == "log" else 6)


def test_embed_audio_reports_an_encoder_failure_in_one_line(
    catalog, capsys, monkeypatch
):
    write_catalog(catalog)
    write_archive(Path("data") / "audio.zip", catalog)
    encoder = DyingEncoder(8)

    def fake_embed(*args, **kwargs):
        return embed_archive(*args, **kwargs, expected_sha1=None, decode=fake_decode)

    monkeypatch.setattr(cli, "SentenceTransformerEncoder", lambda threads: encoder)
    monkeypatch.setattr(cli, "embed_archive", fake_embed)
    command = ["embed-audio", "--archive", "data/audio.zip", "--vectors", "data/v.npz"]
    assert main(command) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    (line,) = captured.err.splitlines()
    assert line.startswith("embed-audio: ")
    assert "RuntimeError" in line and "8 clips" in line
    assert len(VectorStore.load(Path("data/v.npz")).track_ids) == 8

    encoder.clips_left = 1_000
    assert main(command) == 0
    assert (
        "embedded 52 of 60 tracks, 0 failed, 8 already done" in capsys.readouterr().out
    )


def test_embed_audio_reports_ctrl_c_in_one_line(catalog, capsys, monkeypatch):
    write_catalog(catalog)
    write_archive(Path("data") / "audio.zip", catalog)

    def fake_embed(*args, **kwargs):
        return embed_archive(*args, **kwargs, expected_sha1=None, decode=fake_decode)

    monkeypatch.setattr(
        cli,
        "SentenceTransformerEncoder",
        lambda threads: DyingEncoder(8, KeyboardInterrupt),
    )
    monkeypatch.setattr(cli, "embed_archive", fake_embed)
    command = ["embed-audio", "--archive", "data/audio.zip", "--vectors", "data/v.npz"]
    assert main(command) == 130
    captured = capsys.readouterr()
    assert captured.err.splitlines() == [
        "embed-audio: interrupted; run it again to resume"
    ]
    assert len(VectorStore.load(Path("data/v.npz")).track_ids) == 8


class LengthLimitEncoder(FakeEncoder):
    """Rejects any clip longer than `limit` samples."""

    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def encode_audio(self, clips):
        if any(len(clip) > self.limit for clip in clips):
            raise RuntimeError("clip is too long")
        return super().encode_audio(clips)


def decode_with_long_tracks(long_tracks):
    """Two-second clips, three seconds for the tracks in `long_tracks`."""

    def decode(data):
        clip = fake_decode(data)
        seconds = 3 if int(clip[1]) in long_tracks else 2
        return np.resize(clip, seconds * SAMPLE_RATE)

    return decode


@pytest.mark.parametrize("long_tracks", [{100}, {100, 101}, {130}])
def test_a_clip_over_the_encoders_length_limit_does_not_block_the_run(
    tmp_path, catalog, long_tracks
):
    # Track 100 is the first clip, so nothing has encoded yet when it fails.
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    result = embed_archive(
        catalog,
        archive,
        out,
        LengthLimitEncoder(2 * SAMPLE_RATE),
        batch_size=2,
        expected_sha1=None,
        decode=decode_with_long_tracks(long_tracks),
    )
    assert result.embedded == 60 - len(long_tracks)
    assert VectorStore.load(out).failures == {
        str(track): "cannot encode: RuntimeError" for track in long_tracks
    }


def test_a_resume_gets_past_a_clip_over_the_length_limit(tmp_path, catalog):
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    first = select_tracks(catalog, 5, 0)
    long_track = next(int(t) for t in catalog.track_ids if int(t) not in first)
    decode = decode_with_long_tracks({long_track})
    encoder = LengthLimitEncoder(2 * SAMPLE_RATE)
    embed_archive(
        catalog, archive, out, encoder, limit=5, expected_sha1=None, decode=decode
    )
    result = embed_archive(
        catalog, archive, out, encoder, expected_sha1=None, decode=decode
    )
    assert (result.embedded, result.failed) == (54, 1)
    assert VectorStore.load(out).failures == {
        str(long_track): "cannot encode: RuntimeError"
    }


def test_running_out_of_memory_for_full_clips_blames_no_clip(tmp_path, catalog):
    # Under memory pressure a short tone still encodes but no full clip does, on
    # this run or on a resume. Neither may mark good clips as failed.
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    decode = decode_with_long_tracks(set())

    class Pressured(LengthLimitEncoder):
        def encode_audio(self, clips):
            self.limit = 2 * SAMPLE_RATE if len(self.batches) < 2 else SAMPLE_RATE
            return super().encode_audio(clips)

    pressured = Pressured(0)
    with pytest.raises(AudioEmbeddingError, match="test tone"):
        embed_archive(
            catalog, archive, out, pressured, expected_sha1=None, decode=decode
        )
    assert VectorStore.load(out).failures == {}
    assert len(VectorStore.load(out).track_ids) == 8
    with pytest.raises(AudioEmbeddingError, match="test tone"):
        embed_archive(
            catalog,
            archive,
            out,
            LengthLimitEncoder(SAMPLE_RATE),
            expected_sha1=None,
            decode=decode,
        )
    assert VectorStore.load(out).failures == {}


def test_memory_pressure_on_an_older_cache_blames_no_clip(tmp_path, catalog):
    # A cache from before `longest_clip`: its clips' lengths are unknown, so a
    # one-second tone proves nothing about the full clips it holds.
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    decode = decode_with_long_tracks(set())
    embed_archive(
        catalog, archive, out, FakeEncoder(), limit=5, expected_sha1=None, decode=decode
    )
    store = VectorStore.load(out)
    del store.meta["longest_clip"]
    store.save(out)
    with pytest.raises(AudioEmbeddingError, match="no clip encodes"):
        embed_archive(
            catalog,
            archive,
            out,
            LengthLimitEncoder(SAMPLE_RATE),
            expected_sha1=None,
            decode=decode,
        )
    assert VectorStore.load(out).failures == {}


def test_an_older_cache_gets_past_a_clip_over_the_length_limit(tmp_path, catalog):
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    first = select_tracks(catalog, 5, 0)
    long_track = next(int(t) for t in catalog.track_ids if int(t) not in first)
    decode = decode_with_long_tracks({long_track})
    encoder = LengthLimitEncoder(2 * SAMPLE_RATE)
    embed_archive(
        catalog, archive, out, encoder, limit=5, expected_sha1=None, decode=decode
    )
    store = VectorStore.load(out)
    del store.meta["longest_clip"]
    store.save(out)
    result = embed_archive(
        catalog, archive, out, encoder, expected_sha1=None, decode=decode
    )
    assert (result.embedded, result.failed) == (54, 1)
    store = VectorStore.load(out)
    assert store.failures == {str(long_track): "cannot encode: RuntimeError"}
    assert store.meta["longest_clip"] == 2 * SAMPLE_RATE


def test_a_selection_of_only_failing_clips_completes_and_stays_retryable(
    tmp_path, catalog
):
    # With no vector in the cache, nothing shows the clips rather than the encoder
    # are at fault. The run records them and a later run tries them again.
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    decode = decode_with_long_tracks(set())
    lines = []

    def run(encoder):
        return embed_archive(
            catalog,
            archive,
            out,
            encoder,
            limit=3,
            expected_sha1=None,
            decode=decode,
            log=lines.append,
        )

    result = run(LengthLimitEncoder(SAMPLE_RATE))
    assert (result.embedded, result.failed, result.total) == (0, 3, 3)
    assert lines == ["no clip encoded; a later run tries them again"]
    store = VectorStore.load(out)
    assert set(store.failures.values()) == {"cannot encode: RuntimeError"}

    result = run(FakeEncoder())
    assert (result.embedded, result.failed, result.skipped) == (3, 0, 0)
    assert VectorStore.load(out).failures == {}


def test_retries_run_outside_the_failed_calls_exception_handler(tmp_path, catalog):
    # Inside the handler, the traceback keeps the failed call's memory allocated.
    class Recording(LengthLimitEncoder):
        handling = []

        def encode_audio(self, clips):
            self.handling.append(sys.exc_info()[0])
            return super().encode_audio(clips)

    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    encoder = Recording(2 * SAMPLE_RATE)
    embed_archive(
        catalog,
        archive,
        tmp_path / "v.npz",
        encoder,
        expected_sha1=None,
        decode=decode_with_long_tracks({100, 130}),
    )
    assert len(encoder.handling) > 60 / 4
    assert set(encoder.handling) == {None}


def test_a_clip_that_fails_before_any_encodes_gets_its_second_chance(tmp_path, catalog):
    # The first clip's failure was passing, so it is embedded, not recorded.
    class FirstCallsFail(FakeEncoder):
        def encode_audio(self, clips):
            if len(clips[0]) > SAMPLE_RATE and len(self.batches) < 2:
                self.batches.append(0)
                raise MemoryError
            return super().encode_audio(clips)

    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    result = embed_archive(
        catalog,
        archive,
        out,
        FirstCallsFail(),
        batch_size=1,
        expected_sha1=None,
        decode=decode_with_long_tracks(set()),
    )
    assert (result.embedded, result.failed) == (60, 0)
    assert VectorStore.load(out).failures == {}


@pytest.mark.parametrize("clips_left", [1_000, 6], ids=["finished", "encoder-failed"])
def test_ctrl_c_during_the_exit_save_still_saves(
    tmp_path, catalog, monkeypatch, clips_left
):
    archive = write_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    real_save = VectorStore.save
    interrupts = [KeyboardInterrupt]

    def save(self, path):
        if interrupts:
            raise interrupts.pop()
        real_save(self, path)

    monkeypatch.setattr(VectorStore, "save", save)
    with pytest.raises(KeyboardInterrupt):
        embed_archive(
            catalog,
            archive,
            out,
            DyingEncoder(clips_left),
            batch_size=2,
            expected_sha1=None,
            decode=fake_decode,
        )
    assert len(VectorStore.load(out).track_ids) == min(clips_left, 60)


# Class-level check for the escaped defect #204: no failure in the job's loop may
# lose finished work, mark a good clip as failed, or stop a later run finishing.
@given(
    clips_left=st.integers(0, 11),
    batch_size=st.integers(1, 5),
    checkpoint_every=st.sampled_from([1, 5, 200]),
    error=st.sampled_from([RuntimeError, MemoryError, KeyboardInterrupt]),
    bad=st.sets(st.sampled_from([100, 101, 102, 105, 111]), max_size=3),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_any_encoder_failure_keeps_the_work_and_a_later_run_finishes(
    tmp_path, clips_left, batch_size, checkpoint_every, error, bad
):
    catalog = Catalog(make_frame(per_genre=5))
    total = len(catalog)
    archive = write_bad_archive(tmp_path / "a.zip", catalog)
    out = tmp_path / "v.npz"
    out.unlink(missing_ok=True)
    decode = decode_with_bad_tracks(bad)

    class Rejecting(DyingEncoder, RejectingEncoder):
        returned = ()

        def encode_audio(self, clips):
            encoded = super().encode_audio(clips)
            self.returned += tuple(int(clip[1]) for clip in clips)
            return encoded

    def run(encoder):
        return embed_archive(
            catalog,
            archive,
            out,
            encoder,
            batch_size=batch_size,
            checkpoint_every=checkpoint_every,
            expected_sha1=None,
            decode=decode,
        )

    healthy = RejectingEncoder()
    reference = run(healthy)
    assert (reference.embedded, reference.failed) == (total - len(bad), len(bad))
    expected = VectorStore.load(out)
    out.unlink()

    dying = Rejecting(clips_left, error)
    with pytest.raises((AudioEmbeddingError, KeyboardInterrupt)):
        run(dying)
    stopped = VectorStore.load(out)
    # Every clip the encoder returned is in the cache. The encoder stopped working,
    # so the only clips it may blame are the ones that really are bad.
    in_catalog = {int(t) for t in catalog.track_ids}
    assert set(stopped.track_ids.tolist()) == set(dying.returned) & in_catalog
    assert set(stopped.failures) <= {str(t) for t in bad}

    run(RejectingEncoder())
    finished = VectorStore.load(out)
    assert finished.failures == expected.failures
    finished_rows = dict(
        zip(finished.track_ids.tolist(), finished.vectors, strict=True)
    )
    for track, vector in zip(
        expected.track_ids.tolist(), expected.vectors, strict=True
    ):
        assert np.array_equal(finished_rows[track], vector)
    assert len(finished_rows) == len(expected.track_ids)
