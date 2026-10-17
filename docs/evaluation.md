# Offline evaluation

`musicdiscovery evaluate` scores rankers on a fixed set of synthetic queries and
writes a Markdown report and a JSON file with the same numbers.

```sh
musicdiscovery evaluate --generate-queries          # once per catalog; writes eval/queries.json
musicdiscovery evaluate --ranker baseline --ranker hybrid --ranker random --ranker popular
```

Output goes to `reports/offline-<date>.md` and `.json` (`--out-dir`, `--date`).
With no `--ranker`, the five rankers that need no vector cache run. Generated reports are committed only in a
pull request that is about results.

`--benchmark` picks what is scored: `queries` (the default, described first
below), `profiles` (the held-out preference benchmark at the end of this page)
or `all`, which adds the "Held-out preferences" section to the query report.
Without `--benchmark profiles` or `all`, reports are the same as before it
existed.

```sh
musicdiscovery evaluate --generate-profiles         # once per catalog; writes eval/profiles.json
musicdiscovery evaluate --benchmark all
```

## Queries

`eval/queries.json` is generated once with a fixed seed and committed, so
reports stay comparable over time. It holds 100 validation and 100 test queries.
Each query is a seed track plus one to three liked qualities sampled from a
feature axis (`more:`/`less:`), the seed's genre and one of the seed's tags. The file records
the catalog size; evaluation refuses a catalog of a different size. Use the
validation split to choose settings and the test split to report.

## Rankers

| Name | What it returns |
| --- | --- |
| `baseline` | the content-based ranker in `docs/ranking.md` |
| `hybrid` | the hybrid ranker in `docs/ranking.md` |
| `profile` | the learned profile ranker in `docs/ranking.md` |
| `profile-diverse` | the profile ranker's scores re-ranked with the hybrid's MMR, in `docs/ranking.md` |
| `audio-embedding` | the EmbeddingGemma 2 clip-vector ranker in `docs/ranking.md`; needs the vector cache from `musicdiscovery embed-audio`, so it runs only when named with `--ranker` |
| `random` | uniformly random tracks (seeded by the query) |
| `popular` | the most listened-to tracks in the seed's top genre |

Any object with `rank(preference, catalog)` (the `Ranker` protocol) can be added
to `RANKERS` in `musicdiscovery/evaluation.py`.

## Metrics (k = 10)

| Metric | Definition |
| --- | --- |
| Same-genre rate and nDCG (earlier reports: "genre precision" and "genre nDCG") | a result counts if its `genre_top` equals the seed's; binary gains, ideal list from the catalog's relevant tracks. `popular` filters on this label and `hybrid` scores on it, so they see what is being scored |
| Tag overlap | mean Jaccard of result tags with the seed's tags; queries whose seed has no tags are skipped. `hybrid` scores on tags, so it sees what is being scored |
| Held-out tag overlap | mean Jaccard of the results' held-out tags with the seed's held-out tags; rankers never see held-out tags (see below) |
| Preference adherence | share of results whose axis value moves in the requested direction relative to the seed; queries with no axis like are skipped |
| Artist diversity | distinct artists per list divided by list length |
| Popularity | mean listen-count percentile of results (catalog mean is about 0.5) |
| Coverage | share of the catalog recommended on any query in the split; a point value |

Per-query values are averaged. Intervals are percentile bootstrap 95% intervals
over queries (1000 resamples, fixed seed), so the same inputs give identical
numbers. The report ends with a fixed "Limits" section and records the catalog
version from `docs/catalog.md` and the ingest manifest.

## Held-out tags

The same-genre rate and tag overlap reward a ranker for using the label they
are scored against, so they say little about rankers that can see it. The
held-out tag metric scores on tags the rankers cannot see.

- **Split.** Each track's tags are split into a visible and a held-out half by a
  seeded shuffle of its sorted tags (`HOLDOUT_SEED`, recorded in the report),
  keyed on the track id so row order does not matter. The held-out half is 50%
  of the tags (`HOLDOUT_FRACTION`, rounded, at least one tag on each side).
  Tracks with fewer than two tags have nothing to hide.
- **What rankers see.** Every ranker, including `random` and `popular`, is run
  on a catalog view whose `tags` column holds only the visible tags, and the
  `HeldOut` class builds it. The existing metrics still use the full catalog, so
  earlier reports stay comparable.
- **Relevance.** Jaccard overlap of a result's held-out tags with the seed's
  held-out tags, averaged over the ten results. Queries whose seed has fewer than
  two tags are skipped and counted in the report.
- **Queries.** `eval/queries.json` is unchanged. If a query likes a tag that is
  held out for its seed, that like is dropped for this metric, so the ranker is
  never told a hidden tag. Genre and axis likes stay.
- **Same intervals and splits** as the other metrics.

Limits: tags are sparse. In the FMA small subset most seeds have fewer than two
tags, so only a small share of queries count, and the intervals are wide. The
report says so when fewer than 30 queries per split are scored, and does not
switch metrics. Tags are also noisy and correlated with genre, so a ranker that
finds same-genre tracks gains through the visible tags too. The metric reduces
the label leak; it does not measure listener preference, which is for the
listener study. If the hybrid does not beat the baseline here, the report
shows it as measured.

The tests plant a tag structure in a synthetic catalog and check that no ranker
is given a held-out tag, and that a ranker shown the full tags would score
higher.

## Held-out preferences

`--benchmark profiles` asks whether a ranker, given part of a listener's
history, finds the tracks that were held out of it. Real listening data is out
of scope, so the listeners are synthetic.

- **Profiles.** `eval/profiles.json` is generated once with a fixed seed
  (`PROFILE_SEED`) by `--generate-profiles` and committed. Like `queries.json`
  it records the catalog size: evaluation refuses a catalog of a different size,
  and generation refuses to overwrite a profile file made for a catalog of another size. It holds 100
  validation and 100 test profiles.
- **Hidden taste.** Each profile has one top genre, two or three tags drawn from
  that genre's 20 most common tags, and, for about half of the profiles, a
  direction on a feature axis (`more:`/`less:`). Rankers never see the taste.
- **Interactions.** Each profile has 20 distinct catalog tracks with a strength,
  a synthetic play fraction from 0 to 1. Sixteen are taste draws: tracks are
  sampled with weight `exp(3·genre match + 3·tags matched + 1·axis z)` (z
  clipped to ±3), and their play fraction comes from Beta(5, 2) (mostly
  strong). Four are noise: uniform tracks with a play fraction from Beta(2, 5)
  (mostly weak). The weights were set on the validation split so that a ranker
  can recover the taste at all; with weaker weights every ranker, `random`
  included, scored near zero.
- **Split.** 30% of each profile's interactions are held out at random. The
  rest are the history. The split is redrawn until each side has a strong play.
- **Weak-signal filtering.** Plays below `WEAK_SIGNAL_THRESHOLD` (0.3, recorded
  in the report) are dropped from the history before ranking and never count
  as held-out positives. The idea comes from Netflix GenRec, which drops very
  short plays; nothing else of it is used.
- **Ranker input.** The filtered history becomes a `Preference` whose seeds are
  the history tracks, with no liked qualities and k = 10. Rankers are unchanged
  and score the full catalog. Seeds are excluded from results, and a ranker that
  returns a history track is an error. A profile track missing from the catalog
  is an error too, not a skip.
- **Metrics (k = 10).** MRR@10 is the reciprocal rank of the first held-out
  positive in the list, 0 if none is in the top ten. nDCG@10 uses binary gains
  over the held-out positives; the ideal list puts min(10, positives) of them
  first. Both are averaged over profiles with the same bootstrap intervals and
  splits as the query metrics.
- **Report.** The "Held-out preferences" section gives the profile count, seed,
  threshold, how many weak plays were dropped, both metrics per ranker and a
  limits paragraph. The JSON report holds the same numbers under
  `held_out_preferences`, with the profile file's SHA-256.

Limits: synthetic tastes reward rankers that share the generator's features.
The taste is built from `genre_top`, tags and a librosa axis, so `hybrid`, which
scores on genre ids and tags, and `popular`, which filters on the top genre, see
what the generator used; `baseline` sees only audio features. A high score means
a ranker recovers the generator's taste, not that a listener would enjoy the
results. Interactions are not listens, a weak play is not excluded from
results, and the intervals do not cover the generator's settings. The listener
study is still the test of preference.

## Shape-matched test catalogs

Tests never read the real catalog, but they run on synthetic catalogs shaped
like it. `scripts/profile_catalog.py` reads a local ingest and writes
`tests/fixtures/catalog_shape.json`, which holds aggregate statistics only:

- the share of each missing field (title, artist, album, top genre, genre ids,
  tags, duration, listens, license and source URLs);
- the genre, genre-id-count and tag-count distributions, and the number of
  distinct tags per track;
- the share of non-ASCII and right-to-left titles and artist names, the share
  per script (CJK, Cyrillic, Arabic, Hebrew, Greek, emoji, accented Latin,
  other), length quantiles, the duplicate rate and the tracks-per-artist quantiles;
- duration and listens quantiles;
- the share of tracks without features or without an audio vector, and the
  feature column schema (statistics and coefficient counts per feature).

It holds no track id, title, artist, album, tag or other row-level value; the
only strings are field names, the top-genre labels, the feature schema and the
archive checksum. A test checks this. `tests/catalog_factory.py`
`from_shape(profile, n, seed)` generates a catalog with those distributions and
made-up titles, artists and tags, and `vectors_for` a matching vector cache.
Every edge case the code claims to handle (a missing title, album or top genre,
no genre ids, no tags, a track without an audio vector, each title script)
occurs at least once, even when the ingest measured none: on the 2017 `small`
catalog no track lacks a top genre, but #89 crashed on a catalog that had one.

`tests/test_catalog_shape.py` runs `recommend` (every ranker, including
`audio-embedding` from a seed without a vector, and text output on a cp1252
console), `evaluate` and `export-demo` on a 400-track shape-matched catalog.
`scripts/build_demo_fixture.py` exports one to `<out_dir>/shape` and adds its
exclusion queries to the JavaScript parity test.

### Refreshing the profile

After an ingest change or a new ingest, and after `embed-audio` if the vector
cache changed:

```sh
musicdiscovery ingest
python scripts/profile_catalog.py   # --vectors PATH if the cache is not in its default place
git diff tests/fixtures/catalog_shape.json
```

Read the diff before committing it: it must contain only shares, quantiles and
the schema. Without a vector cache, `audio.without_vector` is `null` and the
generator leaves one track without a vector. The committed profile was taken
from the 2017 `small` ingest with no vector cache; on the full cache, 6 of 7,995
clips have no vector ([ranking.md](ranking.md#audio-embedding-ranker)).

## Feedback logs from the demo

The demo page keeps a feedback log in the visitor's browser (see
[demo.md](demo.md#feedback-log)): one JSON line per thumbs action, with the
ranker, seeds, qualities, the shown list (track ids and ranks), the rated track,
its rating and the score terms behind the explanations it showed. A visitor can
download it and send it on. It is never uploaded. Summarise a downloaded log with:

```sh
musicdiscovery feedback-report music-discovery-feedback.jsonl
```

As with the other commands' paths, the log must be inside the working directory.
Each line is checked against `src/musicdiscovery/feedback-log.schema.json`. A
line that is not JSON, does not match the schema, or names a rated track that is
not at its rank in the shown list is reported on stderr with its line number and
skipped. The command fails only when the file cannot be read or no line is valid.
Within one shown list (same ranker, seeds, qualities, exclusion and track order)
the latest action on a track counts, and a `rating` of 0 means that rating was
taken back. The report then prints:

- **Thumbs-up rate per ranker**: liked results over rated results, with a 95%
  Wilson interval.
- **MRR of the first thumbs-up** per ranker: for each shown list with at least
  one rating, 1 / the rank of its highest-placed liked track, or 0 when none was
  liked, averaged over lists.
- **Explanation terms on liked and disliked results**: for each term kind
  (`audio`, `metadata`, `popularity`, `variety`, `genre`, `tag`, `axis`), how
  many liked results and how many disliked results showed it among their
  explanations.

Limits: these numbers describe whoever used the page, not listeners in general.
Visitors choose which tracks to rate, so unrated results are missing rather than
negative, and the rates lean towards tracks people felt strongly about. Only
lists with at least one rating count towards MRR, and a visitor can see the top
of the list first, so MRR favours tracks near the top regardless of ranker.
Visitors know which ranker they picked, unlike the blind listener study, and
nothing ties events to a person, so one visitor can dominate a log and events
are not independent. Merging logs from several browsers cannot detect
duplicates. Term counts show co-occurrence only: a term on liked results did not
necessarily cause the like, and the hybrid shows `metadata` and `popularity`
terms the baseline never has. Use the logs to find questions for the offline
metrics and the [listener study](listener-study.md), not to rank the rankers.
