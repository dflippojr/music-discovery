# Catalog

The catalog is built by `musicdiscovery ingest` from the Free Music Archive
dataset ([`mdeff/fma`](https://github.com/mdeff/fma)). Only metadata and
precomputed features are used. No audio is downloaded, processed or hosted.

## Source

| Item | Value |
| --- | --- |
| Archive | `fma_metadata.zip` (342 MiB), <https://os.unil.cloud.switch.ch/fma/fma_metadata.zip> |
| Dataset version | FMA dataset v1 (archive files dated 2017-04-01) |
| Archive SHA1 | `f0df49ffe5f2a6008d7dc83c6915b31835dfe733` (from the `mdeff/fma` README; ingest refuses any other file) |
| Subset | `small`: 8,000 tracks, 8 balanced top genres (`set.subset == "small"` in `tracks.csv`) |

The download uses a 30 s connect/read timeout and up to 3 attempts with
exponential backoff for timeouts, connection drops and HTTP 5xx; 4xx fails at
once. A failed download leaves no `.part` file and no archive behind.

Files read from the archive, with their SHA1 as listed in its `checksums` file:

| File | Used for | SHA1 |
| --- | --- | --- |
| `tracks.csv` | title, artist, album, top genre, genre ids, tags, duration, listens, subset | `e65cfe18cbba4e37921b58ea5293c2510329de75` |
| `raw_tracks.csv` | license URL and source page URL per track | `2d78c45e4d8415215d4c44eced083ac173feff4f` |
| `features.csv` | 518 librosa features per track | `b7f2339016af1c31e0293cef18383906992b160c` |

`genres.csv` is not read: `tracks.csv` already carries genre ids and the top
genre name. `echonest.csv` is not used: it covers only 1,289 of the 7,995
catalog tracks (16%), too few for a ranker.

## Output

`data/catalog/catalog.parquet`, plus `data/catalog/catalog.json` (source
checksum, ingest version and counts, used to make a second run a no-op). Both
are git-ignored. Columns: `track_id`, `title`, `artist`, `album`, `genre_top`,
`genre_ids`, `tags`, `duration` (seconds), `listens`, `license_url`,
`source_url`, then 518 feature columns named `<feature>_<statistic>_<nn>`
(for example `mfcc_mean_01`) as `float32`.

## Filters

1. Keep only tracks in the `small` subset.
2. Drop tracks with no license URL. Tracks with a missing or unknown license
   are removed, so every row carries a recorded license.
3. Drop tracks with no complete feature row.

The run prints how many rows each filter dropped. On the published archive:
7,995 of 8,000 tracks are kept, 5 dropped for a missing license, 0 for missing
features.

`source_url` values come from the 2017 dump and are best-effort: the FMA site
changed hands after 2019, and ingest does not check that they resolve.

## Licenses

| Part | License |
| --- | --- |
| Metadata and features | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| FMA code | MIT |
| Audio (not used here) | Each artist's chosen license, recorded per track in `license_url` |

`license_url` is kept for display only. Many tracks are NC or ND licensed, so
the demo links each track to its source page and hosts no audio.

## Attribution

> Track metadata and audio features come from the Free Music Archive dataset
> (FMA) by Defferrard, Benzi, Vandergheynst and Bresson, "FMA: A Dataset For
> Music Analysis", ISMIR 2017, <https://github.com/mdeff/fma>, licensed under
> CC BY 4.0. Cleaned and filtered for this project; see `docs/catalog.md`.
