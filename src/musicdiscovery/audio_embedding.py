"""Audio-embedding ranker: EmbeddingGemma 2 vectors of the FMA small clips.

The vectors come from an offline job (`musicdiscovery embed-audio`) that reads
the clips straight from the checksum-verified `fma_small.zip` under `data/`,
embeds each 30 s clip on the CPU and caches the vectors under `data/`. Neither
audio nor vectors are committed. Ranking needs only numpy and the cache; the
model is loaded only to embed clips or a text description.
"""

import io
import json
import os
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from musicdiscovery.catalog import Catalog
from musicdiscovery.ingest import sha1_of
from musicdiscovery.preference import Preference
from musicdiscovery.profile_ranker import weighted_centre
from musicdiscovery.ranking import (
    BaselineRanker,
    Recommendation,
    _allowed,
    quality_terms,
)

MODEL_ID = "google/embeddinggemma-2"
SAMPLE_RATE = 16_000  # the model card asks for mono 16 kHz audio
MIN_SECONDS = 1.0
DEFAULT_ARCHIVE = Path("data") / "audio" / "fma_small.zip"
DEFAULT_VECTORS = Path("data") / "embeddings" / "embeddinggemma-2.npz"
# From the mdeff/fma README; embed-audio refuses any other archive.
FMA_SMALL_SHA1 = "ade154f733639d52e35e32f5593efe5be76c6d70"
SIMILARITY_WEIGHT = 1.0
DESCRIPTION_WEIGHT = 1.0
# The model card's retrieval query prompt; clips are embedded with no prompt.
QUERY_PROMPT = "SearchQuery"
SIMILARITY_TEXT = "similar sound to the seeds (audio embedding)"


class AudioEmbeddingError(Exception):
    """Raised when clips cannot be embedded or the vector cache cannot be used."""


class Encoder(Protocol):
    """Maps 16 kHz mono clips and text into one unit-length vector space."""

    name: str

    def encode_audio(self, clips: Sequence[np.ndarray]) -> np.ndarray: ...

    def encode_text(self, texts: Sequence[str]) -> np.ndarray: ...


class SentenceTransformerEncoder:
    """EmbeddingGemma 2 through sentence-transformers, on the CPU in float32.

    The model card advises against float16. The vision encoder is not loaded.
    Needs the `audio` extra (`pip install -e ".[audio]"`) and downloads the
    weights from Hugging Face on first use.
    """

    def __init__(self, model_id: str = MODEL_ID, threads: int = 4):
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise AudioEmbeddingError(
                f'embedding needs the audio extra: pip install -e ".[audio]" ({error})'
            ) from error
        torch.set_num_threads(threads)
        self.name = model_id
        self._model = SentenceTransformer(
            model_id,
            device="cpu",
            model_kwargs={"torch_dtype": torch.float32},
            config_kwargs={"vision_config": None},
        )

    def encode_audio(self, clips: Sequence[np.ndarray]) -> np.ndarray:
        inputs = [
            {"audio": {"array": clip, "sampling_rate": SAMPLE_RATE}} for clip in clips
        ]
        return self._model.encode(
            inputs, batch_size=len(inputs), normalize_embeddings=True
        )

    def encode_text(self, texts: Sequence[str]) -> np.ndarray:
        return self._model.encode(
            list(texts), prompt_name=QUERY_PROMPT, normalize_embeddings=True
        )


def decode_clip(data: bytes) -> np.ndarray:
    """Decode a compressed clip to float32 mono samples at `SAMPLE_RATE`."""
    import av

    chunks = []
    with av.open(io.BytesIO(data)) as container:
        resampler = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(frame)]
        chunks += [f.to_ndarray().reshape(-1) for f in resampler.resample(None)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def member_name(track_id: int) -> str:
    """The clip's path in the archive, for example `fma_small/000/000002.mp3`."""
    name = f"{track_id:06d}"
    return f"fma_small/{name[:3]}/{name}.mp3"


def confined(path: Path) -> Path:
    """Resolve a user-supplied path and keep it inside the working directory."""
    # Imported here: the evaluator imports this module for its ranker table.
    from musicdiscovery.evaluation import EvaluationError
    from musicdiscovery.evaluation import confined as inside

    try:
        return inside(path)
    except EvaluationError as error:
        raise AudioEmbeddingError(str(error)) from error


def _unit(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


@dataclass
class VectorStore:
    """Cached clip vectors: one unit-length row per embedded track id.

    `meta` records the model, the archive checksum and every track that could
    not be embedded, with the reason.
    """

    track_ids: np.ndarray
    vectors: np.ndarray
    meta: dict = field(default_factory=dict)

    def __post_init__(self):
        self.track_ids = np.asarray(self.track_ids, dtype=np.int64).reshape(-1)
        self.vectors = np.asarray(self.vectors, dtype=np.float32)
        if self.vectors.ndim != 2 or len(self.vectors) != len(self.track_ids):
            raise AudioEmbeddingError(
                f"got {len(self.track_ids)} track ids and vectors of shape "
                f"{self.vectors.shape}"
            )
        if len(np.unique(self.track_ids)) != len(self.track_ids):
            raise AudioEmbeddingError("vector cache has duplicate track ids")
        if not np.all(np.isfinite(self.vectors)):
            raise AudioEmbeddingError("vector cache has non-finite values")

    @property
    def failures(self) -> dict[str, str]:
        return self.meta.setdefault("failures", {})

    def save(self, path: Path) -> None:
        """Write the cache atomically: a crash never leaves a half-written file."""
        path = confined(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_name(path.name + ".part")
        with part.open("wb") as handle:
            np.savez(
                handle,
                track_ids=self.track_ids,
                vectors=self.vectors,
                meta=np.array(json.dumps(self.meta, sort_keys=True)),
            )
        os.replace(part, path)

    @classmethod
    def load(cls, path: Path) -> "VectorStore":
        path = confined(path)
        if not path.exists():
            raise AudioEmbeddingError(
                f"no audio vectors at {path}; run `musicdiscovery embed-audio`"
            )
        try:
            with np.load(path, allow_pickle=False) as data:
                return cls(
                    data["track_ids"], data["vectors"], json.loads(str(data["meta"]))
                )
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            raise AudioEmbeddingError(
                f"cannot read audio vectors at {path}: {error}"
            ) from error


@dataclass(frozen=True)
class EmbedResult:
    embedded: int
    failed: int
    skipped: int
    total: int


def select_tracks(catalog: Catalog, limit: int | None, seed: int) -> list[int]:
    """Every catalog track id, or a seeded sample of `limit` of them, in id order."""
    ids = [int(t) for t in catalog.track_ids]
    if limit is None or limit >= len(ids):
        return ids
    if limit < 1:
        raise AudioEmbeddingError(f"limit must be at least 1, got {limit}")
    rng = np.random.default_rng(seed)
    return sorted(int(t) for t in rng.choice(ids, size=limit, replace=False))


def embed_archive(
    catalog: Catalog,
    archive: Path,
    out: Path,
    encoder: Encoder,
    *,
    limit: int | None = None,
    sample_seed: int = 0,
    batch_size: int = 4,
    checkpoint_every: int = 200,
    expected_sha1: str | None = FMA_SMALL_SHA1,
    decode: Callable[[bytes], np.ndarray] = decode_clip,
    log: Callable[[str], None] = lambda _: None,
) -> EmbedResult:
    """Embed catalog clips from the archive into the cache at `out`.

    A second run resumes: tracks already embedded or recorded as failed are
    skipped. The cache is saved every `checkpoint_every` tracks and at the end.
    A missing, undecodable or shorter-than-a-second clip is recorded as a
    failure and the run goes on. Both paths must be inside the working
    directory.
    """
    archive, out = confined(archive), confined(out)
    if not archive.exists():
        raise AudioEmbeddingError(f"no audio archive at {archive}")
    archive_sha1 = sha1_of(archive)
    if expected_sha1 is not None and archive_sha1 != expected_sha1:
        raise AudioEmbeddingError(
            f"{archive} has SHA1 {archive_sha1}, expected {expected_sha1}"
        )
    meta = {
        "model": encoder.name,
        "sample_rate": SAMPLE_RATE,
        "archive_sha1": archive_sha1,
    }
    if out.exists():
        store = VectorStore.load(out)
        cached = {k: store.meta.get(k) for k in meta}
        if cached != meta:
            raise AudioEmbeddingError(
                f"{out} was made with {cached}, not {meta}; move it away to start over"
            )
    else:
        store = VectorStore(np.zeros(0), np.zeros((0, 0)), {**meta, "failures": {}})

    wanted = select_tracks(catalog, limit, sample_seed)
    done = {int(t) for t in store.track_ids} | {int(t) for t in store.failures}
    todo = [t for t in wanted if t not in done]
    ids, vectors = list(store.track_ids), list(store.vectors)
    failures = store.failures
    embedded = 0

    def save() -> None:
        stacked = np.stack(vectors) if vectors else np.zeros((0, 0))
        VectorStore(np.array(ids), stacked, {**meta, "failures": failures}).save(out)

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        since_save = 0
        for start in range(0, len(todo), batch_size):
            batch_ids, clips = [], []
            for track_id in todo[start : start + batch_size]:
                name = member_name(track_id)
                if name not in names:
                    failures[str(track_id)] = "missing from archive"
                    continue
                try:
                    clip = decode(zf.read(name))
                except Exception as error:  # noqa: BLE001 - any decoder error
                    failures[str(track_id)] = f"cannot decode: {type(error).__name__}"
                    continue
                if len(clip) < MIN_SECONDS * SAMPLE_RATE:
                    failures[str(track_id)] = f"clip shorter than {MIN_SECONDS:g} s"
                    continue
                batch_ids.append(track_id)
                clips.append(np.asarray(clip, dtype=np.float32))
            if clips:
                encoded = _unit(
                    np.asarray(encoder.encode_audio(clips), dtype=np.float32)
                )
                ids += batch_ids
                vectors += list(encoded)
                embedded += len(batch_ids)
            since_save += len(todo[start : start + batch_size])
            if since_save >= checkpoint_every:
                save()
                since_save = 0
                log(f"embedded {len(ids)} of {len(wanted)}, {len(failures)} failed")
    save()
    return EmbedResult(
        embedded=embedded,
        failed=sum(1 for t in todo if str(t) in failures),
        skipped=len(wanted) - len(todo),
        total=len(wanted),
    )


class _Aligned:
    """The cache's vectors in catalog row order; `has` marks rows with a vector."""

    def __init__(self, store: VectorStore, catalog: Catalog):
        rows = [catalog.position.get(int(t)) for t in store.track_ids]
        keep = np.array([r is not None for r in rows], dtype=bool)
        self.has = np.zeros(len(catalog), dtype=bool)
        dimension = store.vectors.shape[1] if store.vectors.size else 0
        self.unit = np.zeros((len(catalog), dimension), dtype=np.float32)
        if keep.any():
            positions = np.array([r for r in rows if r is not None], dtype=int)
            self.has[positions] = True
            self.unit[positions] = _unit(store.vectors[keep])


class AudioEmbeddingRanker:
    """Cosine similarity to the seeds' clip vectors, plus the baseline quality terms.

    The profile is the strength-weighted mean of the vectors of the seeds that
    have one. Only tracks with a vector can be returned, so scoring stays inside
    the embedded catalog. An optional description vector (from
    `describe_vector`) adds a text-to-audio term. When no seed has a vector and
    there is no description, nothing in the query says what to sound like, so
    the ranker returns an empty list instead of ordering tracks by id.
    """

    def __init__(
        self,
        store: VectorStore | None = None,
        path: Path = DEFAULT_VECTORS,
        description: tuple[str, np.ndarray] | None = None,
        description_weight: float = DESCRIPTION_WEIGHT,
    ):
        self._store = store
        self.path = path
        self.description = description
        self.description_weight = description_weight
        self._baseline = BaselineRanker()
        self._catalog: Catalog | None = None
        self._aligned: _Aligned | None = None

    @property
    def store(self) -> VectorStore:
        if self._store is None:
            self._store = VectorStore.load(self.path)
        return self._store

    def aligned(self, catalog: Catalog) -> _Aligned:
        if self._catalog is not catalog or self._aligned is None:
            self._aligned = _Aligned(self.store, catalog)
            self._catalog = catalog
        return self._aligned

    def seeds_without_vector(
        self, preference: Preference, catalog: Catalog
    ) -> tuple[int, ...]:
        """The seeds that have no cached clip vector."""
        has = self.aligned(catalog).has
        return tuple(s for s in preference.seeds if not has[catalog.position[s]])

    def rank(self, preference: Preference, catalog: Catalog) -> list[Recommendation]:
        preference.validate(catalog)
        aligned = self.aligned(catalog)
        if not aligned.has.any():
            raise AudioEmbeddingError("no catalog track has an audio vector")
        seed_rows = [catalog.position[s] for s in preference.seeds]
        pairs = [
            (row, w)
            for row, w in zip(seed_rows, preference.seed_weights(), strict=True)
            if aligned.has[row]
        ]
        if not pairs and self.description is None:
            return []
        similarity = np.zeros(len(catalog))
        if pairs:
            rows, weights = zip(*pairs, strict=True)
            centre = weighted_centre(aligned.unit[list(rows)], weights)
            norm = np.linalg.norm(centre)
            if norm:
                similarity = aligned.unit @ (centre / norm)
        terms = [("embedding", SIMILARITY_TEXT, SIMILARITY_WEIGHT * similarity)]
        if self.description is not None:
            text, vector = self.description
            vector = np.asarray(vector, dtype=np.float32).reshape(-1)
            if vector.shape[0] != aligned.unit.shape[1]:
                raise AudioEmbeddingError(
                    f"description vector has {vector.shape[0]} dimensions, "
                    f"the cache {aligned.unit.shape[1]}"
                )
            vector = vector / (np.linalg.norm(vector) or 1.0)
            terms.append(
                (
                    "description",
                    f"sounds like your description: {text}",
                    self.description_weight * (aligned.unit @ vector),
                )
            )
        terms += quality_terms(preference, catalog, self._baseline._fitted(catalog))
        scores = np.sum([values for _, _, values in terms], axis=0)

        candidates = np.flatnonzero(_allowed(preference, catalog) & aligned.has)
        # Highest score first; ties go to the lower track id.
        order = np.lexsort((catalog.track_ids[candidates], -scores[candidates]))
        chosen = candidates[order[: preference.k]]
        return [BaselineRanker.build(catalog, int(row), terms) for row in chosen]


def describe_vector(text: str, encoder: Encoder) -> tuple[str, np.ndarray]:
    """Embed a description of the qualities a listener likes, for the ranker."""
    if not text.strip():
        raise AudioEmbeddingError("the description is empty")
    return text, np.asarray(encoder.encode_text([text]), dtype=np.float32)[0]
