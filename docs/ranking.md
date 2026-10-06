# Baseline ranker

`musicdiscovery recommend` ranks catalog tracks from seed tracks and optional
likes and dislikes. It is content-based: it uses only the precomputed librosa
features and the genre and tag metadata from `docs/catalog.md`. No audio is
processed and nothing touches the network.

```sh
musicdiscovery recommend --seed 2 --like genre:Folk --like more:energy \
    --dislike tag:noise -k 10 --exclude-seed-artist --json
```

## Preference input

`Preference` holds one or more seed track ids, liked and disliked qualities,
`k` (default 10) and `exclude_seed_artist`. A quality is one of:

| Form | Meaning |
| --- | --- |
| `genre:<name>` | a `genre_top` value in the catalog (for example `Folk`) |
| `tag:<name>` | a tag in the catalog |
| `more:<axis>` / `less:<axis>` | a direction on a named feature axis |

Unknown seed ids, genres, tags and axes are rejected with an error that names
the offending value. Disliking `more:energy` is the same as liking
`less:energy`.

## Axis table

| Axis | Catalog column | `more:` reads as | `less:` reads as |
| --- | --- | --- | --- |
| `energy` | `rmse_mean_01` (mean RMS energy) | more energetic | calmer |
| `brightness` | `spectral_centroid_mean_01` | brighter | darker |
| `noisiness` | `zcr_mean_01` (mean zero-crossing rate) | noisier | cleaner |

The table is `AXES` in `src/musicdiscovery/preference.py`. The catalog has no
tempo feature (that comes from Echo Nest data, which ingest does not use), so
there is no "slower" axis.

## Scoring

1. Standardise all feature columns on the catalog (z-score, clipped to ±5).
2. Project onto the top 20 principal components (PCA, fit on the catalog).
3. `similarity`: cosine similarity of each track to the mean seed vector.
4. Add fixed-weight terms per quality:

| Term | Value |
| --- | --- |
| liked axis | `0.05 × z`, with `z` the track's standardised column value clipped to ±3 (so at most ±0.15), sign set by the direction |
| liked genre / tag | `+0.15` / `+0.10` when the track matches |
| disliked genre / tag | `−0.15` / `−0.10` when the track matches |

Score is the sum of the terms. Seed tracks are never returned; with
`--exclude-seed-artist` neither is any track by a seed artist. Ties go to the
lower track id. The ranker has no randomness, so equal input gives equal output.

## Explanations

Each recommendation carries its score terms. Its explanations are the texts of
the top three positive terms, largest first, for example `similar sound to the
seed`, `same genre: Folk`, `more energetic, as asked`. If no term is positive,
the largest term is used, so every result has at least one explanation.

## Interface

Rankers implement `Ranker.rank(preference, catalog) -> list[Recommendation]`
(`src/musicdiscovery/ranking.py`), so other rankers can replace the baseline.

## Speed

On a 7,998-track, 518-feature synthetic catalog, a laptop (Windows, Python
3.12) fits the standardisation and PCA in about 0.15 s on the first call, then
ranks 10 results in about 3-9 ms. The 200 ms target is documented, not a CI gate.
