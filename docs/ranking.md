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

# Hybrid ranker

`--ranker hybrid` (and `hybrid` in the evaluator) adds metadata similarity, a
popularity prior and a diversity re-rank to the baseline. It implements the same
`Ranker` interface and needs no training per query.

```sh
musicdiscovery recommend --ranker hybrid --seed 2 --like genre:Folk -k 10
```

## Scoring

1. Relevance is the sum of the baseline terms (`audio` is the baseline's
   `similarity`, plus the liked and disliked axis, genre and tag terms) and two
   more terms.
2. `metadata`: TF-IDF cosine similarity between each track and the mean seed,
   over its genre ids and tags (binary term frequency, smoothed idf), times
   `metadata_weight`. Text: `similar genres and tags`.
3. `popularity`: the track's listen-count percentile times `popularity_weight`.
   Text: `popular with listeners`.
4. MMR re-rank of the top `pool_size` tracks. Relevance is min-max scaled to
   0..1 over the pool; each step picks the track with the largest
   `(1 - lambda) * relevance - lambda * redundancy`, where redundancy is
   `(1 - g)` if the artist is already picked plus `g` times the share of picks
   so far with the same top genre (`g` is `genre_redundancy`).

A track that MMR placed ahead of a more relevant one gets a `variety` term worth
`lambda` times the redundancy it avoided, with the text `added for variety: a
different artist or genre from the picks so far`. Explanations follow the
baseline rule (top three positive terms), so `variety` shows up only when it is
among them. Scores are the sum of all terms, `variety` included.

## Config and tuning

The weights and lambda are in `src/musicdiscovery/hybrid_config.json`.
`python scripts/tune_hybrid.py --write` reproduces them from the validation
queries and the ingested catalog: it scores every combination in a fixed grid
(`GRID` in `src/musicdiscovery/tuning.py`), keeps those whose validation genre
precision is within the width of the baseline's validation 95% interval of the
baseline's mean, and picks the one with the highest genre nDCG plus artist
diversity (ties go to the first in grid order). The full grid takes about three
minutes.

Current values: `metadata_weight` 2.0, `popularity_weight` 0.1, `mmr_lambda`
0.6, `genre_redundancy` 0.0, `pool_size` 100. Three of these sit at the edge of
the grid, so a wider grid might do better; the search was not extended.

## What the numbers show

Offline report `reports/offline-2026-10-05.md`, test queries, k = 10, baseline
against hybrid (means; 95% bootstrap intervals are in the report):

| Metric | Baseline | Hybrid |
| --- | --- | --- |
| Genre precision | 0.657 | 0.983 |
| Artist diversity | 0.847 | 0.997 |
| Tag overlap | 0.139 | 0.177 |
| Preference adherence | 0.743 | 0.618 |
| Popularity (catalog mean 0.50) | 0.502 | 0.530 |
| Catalog coverage | 0.115 | 0.111 |

- **Artist diversity rose and genre precision did not fall**, so the
  acceptance condition holds. The artist-diversity intervals do not overlap.
- **The genre precision jump is mostly the metric agreeing with the input.**
  Genre precision counts a result as relevant when it shares the seed's top
  genre, and the hybrid scores tracks on genre ids, which are the labels that
  top genre comes from. A large gain says the hybrid copies the label well. It
  does not say listeners would like the results more. The `popular` reference
  scores 1.0 for the same reason.
- **Preference adherence fell** (0.743 to 0.618, intervals do not overlap).
  The heavy metadata weight outweighs the small axis terms. The tuner did not
  measure adherence, so this cost was not weighed.
- **Coverage did not improve.** The hybrid draws on the same share of the
  catalog (about 11%) as the baseline; the extra variety is across artists, not
  across more of the catalog.
- **Tag overlap** rose slightly, with intervals that overlap.
- The hybrid pulls a little toward popular tracks (0.53 against 0.50).
- **A measure the rankers cannot see** was added after this report: held-out
  tag overlap, in `reports/offline-2026-10-06.md` (see `docs/evaluation.md`).
  Few queries have a seed with two or more tags, so its intervals are wide.
- Everything above is proxy metrics on synthetic queries; see the Limits
  section of the report. Whether the hybrid is better for listeners is for the
  listener study (issue #8).

## Speed

On the 7,995-track catalog the first call (TF-IDF, popularity, PCA) takes about
0.16 s and later calls about 3 ms, so it fits the 200 ms target and the browser
port in issue #7.
