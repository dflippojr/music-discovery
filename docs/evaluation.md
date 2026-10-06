# Offline evaluation

`musicdiscovery evaluate` scores rankers on a fixed set of synthetic queries and
writes a Markdown report and a JSON file with the same numbers.

```sh
musicdiscovery evaluate --generate-queries          # once per catalog; writes eval/queries.json
musicdiscovery evaluate --ranker baseline --ranker random --ranker popular
```

Output goes to `reports/offline-<date>.md` and `.json` (`--out-dir`, `--date`).
With no `--ranker`, all three run. Generated reports are committed only in a
pull request that is about results.

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
| `random` | uniformly random tracks (seeded by the query) |
| `popular` | the most listened-to tracks in the seed's top genre |

Any object with `rank(preference, catalog)` (the `Ranker` protocol) can be added
to `RANKERS` in `musicdiscovery/evaluation.py`.

## Metrics (k = 10)

| Metric | Definition |
| --- | --- |
| Genre precision and nDCG | a result is relevant if its `genre_top` equals the seed's; binary gains, ideal list from the catalog's relevant tracks |
| Tag overlap | mean Jaccard of result tags with the seed's tags; queries whose seed has no tags are skipped |
| Preference adherence | share of results whose axis value moves in the requested direction relative to the seed; queries with no axis like are skipped |
| Artist diversity | distinct artists per list divided by list length |
| Popularity | mean listen-count percentile of results (catalog mean is about 0.5) |
| Coverage | share of the catalog recommended on any query in the split; a point value |

Per-query values are averaged. Intervals are percentile bootstrap 95% intervals
over queries (1000 resamples, fixed seed), so the same inputs give identical
numbers. The report ends with a fixed "Limits" section and records the catalog
version from `docs/catalog.md` and the ingest manifest.
