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
If the seeds' features sit at the catalog mean (for example, every track has the
same features) and nothing is liked or disliked, no term tells tracks apart, so
the ranker returns an empty list instead of the lowest track ids.

## Explanations

Each recommendation carries its score terms. Its explanations are the texts of
the top three positive terms, largest first, for example `similar sound to the
seed`, `same genre: Folk`, `more energetic, as asked`. If no term is positive
(a track that sounds unlike the seed and matches nothing asked for, which only
a small catalog runs down to), the result has no explanation rather than an
untrue one. The
demo page can also show every term of a result with its signed contribution
([demo.md](demo.md#score-detail)).

## Interface

Rankers implement `Ranker.rank(preference, catalog) -> list[Recommendation]`
(`src/musicdiscovery/ranking.py`), so other rankers can replace the baseline.

## Speed

On a 7,998-track, 518-feature synthetic catalog, a laptop (Windows, Python
3.12) fits the standardisation and PCA in about 0.15 s on the first call. Later
calls rank 10 results in about 1.2 ms (median over 200 generated queries on a
7,995-track synthetic catalog shaped like the real one, measured for issue #79).
The 200 ms target is documented, not a CI gate.

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
diversity (ties go to the first in grid order). The full grid takes about two
minutes (122 s on a 7,995-track synthetic catalog, measured for issue #79).

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
0.16 s. Later calls take about 1.9 ms (median over 200 generated queries on a
7,995-track synthetic catalog shaped like the real one, measured for issue #79),
so it fits the 200 ms target and the browser port in issue #7.

# Profile ranker

`--ranker profile` (and `profile` in the evaluator) scores a listener's whole
history rather than one seed. It is a small learned linear model over a few
features, runs on the CPU with numpy only, and does no training at query time.

```sh
musicdiscovery recommend --ranker profile --seed 2 --seed 5 --seed 10 -k 10
musicdiscovery evaluate --benchmark all --ranker baseline --ranker hybrid --ranker profile
musicdiscovery recommend --ranker profile-diverse --seed 2 --seed 5 --seed 10 -k 10
```

## Profile

Each seed may carry a strength (`Preference.strengths`, for example a play
fraction). The held-out preference benchmark passes each history track's play
fraction after weak plays are dropped (see `docs/evaluation.md`); `recommend`
gives every seed strength 1. The profile is the strength-weighted mean of the
history tracks' vectors, separately for each feature below. A preference
always has at least one seed, and building a profile from no tracks is an error.

## Features

| Feature | What it is |
| --- | --- |
| `audio` | cosine similarity to the profile in the baseline's 20-component PCA space |
| `metadata` | the hybrid's TF-IDF cosine over genre ids and tags |
| `text` | TF-IDF cosine over the words (two or more letters or digits, lower-cased) of the title, artist, album, top genre name and tags |
| `popularity` | listen-count percentile |

Both TF-IDF features use the same `TfIdf` class in `hybrid.py` (binary term
frequency, smoothed idf). In the evaluator's held-out tag view, the tags are the
visible ones only. Liked and disliked axes, genres and tags add the baseline's
fixed-weight terms on top.

## Scoring head

A track's score is the sum of each feature times its learned weight, plus the
quality terms. Seed tracks are never returned and `--exclude-seed-artist` works
as in the baseline; ties go to the lower track id. Explanations follow the
baseline rule (top three positive terms): `similar sound to your listening`,
`similar genres and tags`, `similar titles, artists and tags`, `popular with
listeners`. `profile` has no diversity re-rank; `profile-diverse` adds one.

## Diversity re-rank (`profile-diverse`)

`--ranker profile-diverse` (and `profile-diverse` in the evaluator) scores
exactly as `profile`, then re-ranks the top `pool_size` allowed tracks with the
hybrid's MMR (`mmr` in `hybrid.py`, described under the hybrid ranker). It
reads `mmr_lambda` (0.6), `genre_redundancy` (0.0) and `pool_size` (100) from
`hybrid_config.json` and has no config or tuning of its own. With
`genre_redundancy` 0.0 the penalty is for repeating an artist already picked
for the list; artists from the history are not penalised. A pick that jumped a
more relevant track gets the hybrid's `variety` term and explanation.

The weights come from an L2-penalised logistic regression (penalty 1.0, no
penalty on the bias), fit by Newton's method in numpy on the validation
profiles only: each profile's held-out positives against 50 catalog tracks
drawn uniformly from outside its history and held-out set, with seed 20261013
(507 positives, 5,000 negatives). `python scripts/train_profile_ranker.py
--write` reproduces `src/musicdiscovery/profile_ranker.json` from the ingested
catalog and `eval/profiles.json` in about two seconds; Newton's method has no
randomness, so two runs give the same file.

Current weights: `audio` 0.63, `metadata` 3.41, `text` 6.38, `popularity` 0.39.
The features are on similar 0-1 scales, so text similarity matters most.

## What the numbers show

Offline report `reports/offline-2026-10-13-profile-diverse.md` (the baseline,
hybrid and profile numbers are the same as in `reports/offline-2026-10-12.md`),
test split, k = 10, means with 95% bootstrap intervals:

| Metric | Baseline | Hybrid | Profile | Profile-diverse |
| --- | --- | --- | --- | --- |
| Held-out preference MRR@10 | 0.015 [0.002, 0.033] | 0.281 [0.197, 0.368] | 0.395 [0.310, 0.476] | 0.346 [0.259, 0.432] |
| Held-out preference nDCG@10 | 0.008 [0.001, 0.019] | 0.112 [0.078, 0.147] | 0.263 [0.207, 0.319] | 0.149 [0.112, 0.185] |
| Results by a history artist (profiles) | 0.079 [0.056, 0.104] | 0.142 [0.124, 0.164] | 0.807 [0.745, 0.866] | 0.369 [0.336, 0.406] |
| Held-out tag overlap (queries) | 0.088 [0.018, 0.176] | 0.089 [0.050, 0.131] | 0.284 [0.165, 0.429] | 0.098 [0.063, 0.139] |
| Same-genre rate (queries) | 0.657 | 0.983 | 0.979 | 0.977 |
| Artist diversity (queries) | 0.847 | 0.997 | 0.380 [0.328, 0.436] | 0.955 [0.910, 0.991] |
| Preference adherence (queries) | 0.743 | 0.618 | 0.566 [0.500, 0.638] | 0.605 [0.541, 0.667] |

"Results by a history artist" is not printed by `evaluate`. It is the share of
each test profile's top-10 results whose artist is among the artists of the
profile's filtered history tracks (the seeds the ranker gets), averaged over
the 100 test profiles, with a bootstrap interval over profiles (seed 7, 1000
resamples). Every list has ten results, so the mean equals the share of all
1,000 results. The same count over the held-out positives gives 0.31.

What the numbers can show:

- **On the held-out preference benchmark, profile ranks the generator's
  held-out tracks higher than hybrid.** The nDCG intervals do not overlap; the
  MRR intervals overlap a little. Validation numbers move the same way, but
  validation is the training split, so only test counts.
- **Held-out tag overlap rose** with intervals that do not overlap hybrid's, on
  few queries (12 test seeds have two or more tags).

What they cannot show:

- **Profile is trained on the same synthetic generator it is tested on.** Its
  weights are fit on the validation profiles, and the test profiles come from
  the same generator with the same genre, tag and axis tastes. A better test
  score shows it learned that generator, not that listeners would like it.
- **Much of the gain is same-artist tracks.** On the test profiles, 81% of
  profile's results are by an artist already in the history, against 14% for
  hybrid and 31% of the held-out positives. Artist names and album titles are
  text features, and tracks by one artist share tags, so the generator's
  held-out tracks are often the same artist's other tracks. Artist diversity on
  the queries falls to 0.38. That is the opposite of discovery, and a diversity
  re-rank (as in hybrid) would trade some of the score back.
- Held-out tag overlap is helped the same way: tracks by one artist tend to
  share their hidden tags too.
- **Preference adherence fell** below hybrid's, with overlapping intervals. The
  quality terms keep the baseline's small fixed weights, which the learned
  feature weights outweigh; they were not learned, because the profiles have
  no liked qualities.
- Same-genre rate is a label the ranker sees, as for hybrid.
- Whether any of this helps listeners is for the listener study (issue #8).

With the diversity re-rank (`profile-diverse`), what the numbers can show:

- **The re-rank removes most of the same-artist lists.** Results by a history
  artist fall from 0.81 to 0.37, close to the 0.31 of the held-out positives,
  and artist diversity on the queries rises from 0.38 to 0.955. What remains is
  one track per history artist: MMR penalises an artist only once it is in the
  list, so each history artist can still appear once (1 of the 100 test lists
  repeats an artist).
- **Profile-diverse's nDCG is above hybrid's** (0.149 against 0.112), but the
  intervals overlap, so this does not show it ranks better; its MRR (0.346)
  is between hybrid's and profile's, with intervals that overlap both.
- **Most of profile's nDCG lead came from same-artist tracks.** nDCG falls from
  0.263 to 0.149 when the same-artist repeats go, with intervals that do not
  overlap. Held-out tag overlap falls back to hybrid's level (0.098 against
  0.089), which supports the earlier note that tracks by one artist share their
  hidden tags.

What they cannot show:

- Everything above about profile still applies: the weights were trained on
  this generator, and its held-out tracks are often the history artists' other
  tracks, which profile-diverse still returns once per artist.
- The MMR settings are the hybrid's, tuned for the hybrid's scores. They were
  not tuned for profile scores, so a different lambda could trade more or less
  of the score back. This arm has no tuning by design (issue #80).
- Whether a lead this small over hybrid matters to listeners is for the
  listener study (issue #8).

## Speed

On the 7,995-track catalog (Windows laptop, Python 3.12), the first call fits
the PCA and builds both TF-IDF tables in about 0.25 s; later calls rank 10
results from a profile of 12 history tracks in about 0.9 ms, and from one seed
in about 1.6 ms (medians on a 7,995-track synthetic catalog shaped like the real
one, measured for issue #79), well under the 200 ms target. Training takes about
2 s. The `profile-diverse` re-rank adds about 0.3 ms per call (medians over
200 calls on the ingested catalog: 1.2 ms for profile and 1.5 ms for
profile-diverse from 12 history tracks).

# Audio-embedding ranker

`--ranker audio-embedding` (and `audio-embedding` in the evaluator) ranks by how
alike the clips sound to EmbeddingGemma 2, Google's multimodal embedding model,
which maps text and audio into one 768-dimension space. It needs the vector
cache from an offline job and runs on the CPU only.

```sh
pip install -e ".[audio]"
musicdiscovery embed-audio --threads 4 --batch-size 4
musicdiscovery recommend --ranker audio-embedding --seed 2 -k 10
musicdiscovery recommend --ranker audio-embedding --seed 2 --describe "warm acoustic guitar"
```

## Model

- **Licence.** `google/embeddinggemma-2` is Apache 2.0 (model card).
- **Audio quality claim.** The card reports a MAEB mean of 49.39 at 768
  dimensions (49.21 at 512, 46.92 at 128). That is the card's own number; this
  project did not rerun MAEB.
- **Runtime.** The card says not to use float16, so the model runs in float32
  through sentence-transformers, with the vision encoder not loaded. The
  `unsloth/embeddinggemma-2-GGUF` repo ships `mmproj` encoder files, but neither
  card documents audio embeddings through llama.cpp, so it is not used. Clips
  are embedded with no prompt; `--describe` text uses the card's `SearchQuery`
  prompt.

## Offline job

`musicdiscovery embed-audio` reads each clip straight from the checksum-verified
`data/audio/fma_small.zip`, decodes it to 16 kHz mono and caches unit vectors in
`data/embeddings/embeddinggemma-2.npz`. It saves every 200 tracks, so a stopped
run resumes where it left off, and it records failed tracks with the reason.
`--limit` embeds a seeded sample instead. Neither audio nor vectors are
committed.

On the full catalog, 7,989 of 7,995 clips were embedded. The six that failed
(track ids 98565, 98567, 98569, 99134, 108925 and 133297) do not decode; they
are known bad files in FMA `small`. On a Windows tower with four CPU threads and
batches of four, the run took about 6 minutes per 200 clips, about 4 hours in
all.

## Scoring

The profile is the strength-weighted mean of the seeds' clip vectors, as in
the profile ranker. A track's score is its cosine similarity to that profile,
plus a cosine to the `--describe` vector when given, plus the baseline's
fixed-weight quality terms. Only tracks with a vector can be returned, so
scoring stays inside the embedded catalog. Seeds without a vector (such as the
six that failed to decode) are left out of the profile. If no seed has a vector
and there is no `--describe` text, the ranker returns an empty list and
`recommend` prints a note on stderr, because nothing says what to sound like;
the evaluator scores that query as undefined (NaN), so it drops out of the
means. Seed tracks are never returned,
`--exclude-seed-artist` works as in the baseline, and ties go to the lower
track id.

## What the numbers show

Offline report `reports/offline-2026-10-13.md`, test split, k = 10, means with
95% bootstrap intervals:

| Metric | Baseline | Hybrid | Profile | Audio-embedding |
| --- | --- | --- | --- | --- |
| Held-out preference MRR@10 | 0.015 [0.002, 0.033] | 0.281 [0.197, 0.368] | 0.395 [0.310, 0.476] | 0.015 [0.000, 0.040] |
| Held-out preference nDCG@10 | 0.008 [0.001, 0.019] | 0.112 [0.078, 0.147] | 0.263 [0.207, 0.319] | 0.006 [0.000, 0.015] |
| Held-out tag overlap (queries) | 0.088 [0.018, 0.176] | 0.089 [0.050, 0.131] | 0.284 [0.165, 0.429] | 0.051 [0.006, 0.108] |
| Same-genre rate (queries) | 0.657 [0.587, 0.720] | 0.983 | 0.979 | 0.674 [0.598, 0.745] |
| Artist diversity (queries) | 0.847 | 0.997 | 0.380 | 0.795 [0.752, 0.834] |
| Preference adherence (queries) | 0.743 [0.688, 0.795] | 0.618 | 0.566 | 0.977 [0.952, 0.998] |
| Coverage (queries) | 0.115 | 0.111 | 0.117 | 0.082 |

What the numbers can show:

- **Audio-embedding does not win offline.** On the held-out preference
  benchmark it scores like baseline and near `random`, far below hybrid and
  profile. On the query metrics it ties baseline on the same-genre rate, a label
  neither ranker sees, and its held-out tag overlap is no better than baseline's.
- **Preference adherence is high because the axis terms dominate.** Clip
  vectors are close together: for 300 sampled clips against the catalog, the
  cosine averages 0.79 with a standard deviation of 0.07. A liked axis adds up to 0.15
  (0.05 per standard deviation, clipped at 3), so it outweighs most differences
  in similarity, and the ranker mostly sorts sound-alikes by the asked-for axis.
  The baseline's PCA cosines spread from -1 to 1, so similarity matters more
  there.

What they cannot show:

- **The benchmarks favour the label columns.** The synthetic listener tastes are
  built from genres, tags and a librosa axis, and the query likes come from the
  same columns. Hybrid and profile score on genre ids and tags; audio-embedding
  sees only the clips. A ranker that hears what a listener hears is not
  rewarded here unless it lines up with those labels.
- **Text-to-audio (`--describe`) is not evaluated.** The benchmarks have no
  descriptions, so nothing offline measures it.
- Weights were not tuned: the similarity weight is 1 and the quality terms are
  the baseline's. Rescaling the similarity would lower adherence and might move
  the other metrics; that is a tuning question for the validation split.
- Whether it helps listeners is for the listener study (issue #8). It does not
  win offline, so it is not proposed as a pilot arm.

## Speed

On the 7,995-track catalog, the first call loads the cache and fits the
baseline's PCA in about 0.11 s; later calls with 12 seeds take about 11 ms. The
model is loaded only to embed clips or a `--describe` text, not to rank.
