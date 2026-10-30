# Sound bridge pilot

Issue #227 asks whether a known track's AcousticBrainz features can pick a
sensible nearest track in the FMA catalog (#226). The owner listens to 30 blind
pairs. **Pass: the bridge wins at least 21 of 30.** The bar was fixed on
2026-10-30 before any data existed. On a fail, the idea is dropped with no
fallback to a genre stand-in.

Nothing here touches the rankers, the catalog or the demo, and no audio is
downloaded. `scripts/bridge_pilot.py` writes only under the ignored `data/`.

## Tracks

`eval/bridge_pilot.json` holds the 30 tracks, their alternates, the seed, the
bar and how they were chosen. It was committed before any fetch or match ran.
Each track's `genre` is the catalog top genre it would have, set by hand.

## Fetching records

`bridge_pilot.py fetch` searches MusicBrainz for each song and keeps recordings
with the same title and artist, dropping any whose comment marks a live, remix,
cover or similar version. It takes the first with an AcousticBrainz low-level
record that has every feature, whose own tags name the same song and artist, and
that is within 15 s of its recording's length and 35% of the median length of the
song's recordings. The tag and median checks were added after a first fetch, before any match ran, because that
fetch found files attached to the wrong recording (for example a Devo cover under
the Rolling Stones) and live cuts.
`resolved.json` says which tracks needed a recording other than the first. A
track with no usable record is replaced by the next alternate of its genre,
before any match runs.

MusicBrainz's search hits vary between runs, so `resolved.json` from the one
fetch that fed the match is the record, not a fresh run.

Requests send a contact User-Agent, leave 1.5 s between calls, and wait out any
`X-RateLimit` or 429 answer.

## Matching

`bridge_pilot.py prepare` compares 38 values that mean about the same thing in
both extractors:

| Catalog column (librosa) | AcousticBrainz field (Essentia) | Values |
| --- | --- | --- |
| `mfcc_mean_01`–`13` | `mfcc.mean` | 13 |
| `mfcc_std_01`–`13` | square root of the `mfcc.cov` diagonal | 13 |
| `spectral_centroid_*_01` | `spectral_centroid` | mean, median, std |
| `spectral_rolloff_*_01` | `spectral_rolloff` | mean, median, std |
| `rmse_*_01` | `spectral_rms` | mean, median, std |
| `zcr_*_01` | `zerocrossingrate` | mean, median, std |

Chroma, tonnetz and spectral contrast are left out because the two extractors
define their bands and reference pitch differently.

**Standardizing.** The extractors' values can't be compared raw, so each side
gets a z-score per column against itself: the catalog side over all catalog rows,
the known side over the 30 known tracks. This is crude. It assumes the two groups
have similar spreads, and it removes whatever the 30 famous tracks share, such
as being loud. The pick is the catalog row with the smallest Euclidean distance.
Every column has equal weight, and nothing was tuned.

## Pairs and blinding

Each pair holds the bridge's pick and a random catalog track from the **known
track's own genre**. That control stands for the genre stand-in the bridge has to
beat. The same seed, from `eval/bridge_pilot.json`, drives the control, the A/B
slot and the listening order.

No track appears twice, because a link seen in two pairs would give away the
bridge's. When a pick is already taken, the track falls to its next nearest,
in list order. `unblind` reports how many picks that moved.

The page shows the song and two links, A and B, with no role anywhere in it. The
key is `data/bridge-pilot/sealed/key.json`. `prepare` prints only counts and the
key's SHA-256, and refuses to run twice.

One leak remains. The archive's page shows a track's genre, so a pick from
another genre could be told from the control by someone who reads the page. The
page asks the listener to judge by ear and ignore those labels, and `unblind`
reports how many picks were off-genre.

## Listening

The listening page is a private page on claude.ai, published from
`data/bridge-pilot/listen.html`. Each pick saves as it is made, so the check
works over several sittings. "Can't compare" records a pair as unplayable.

## Scoring

Save the picks as JSON (`{"1": "A", "2": "unplayable", ...}`), then run
`bridge_pilot.py unblind PICKS.json`. An unplayable pair can't win, so the bar
always counts wins out of 30. The result gives wins, the two-sided exact sign
test on all pairs and on played pairs, pass or fail, and how many picks were
moved or off-genre.

A small sample from one listener can stop a bad idea but can't prove a good one.
