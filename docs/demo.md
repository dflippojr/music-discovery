# Static demo page

A small page that lets a visitor pick a seed track, like or dislike qualities,
and read explained recommendations from the hybrid ranker (the default) or the
baseline. The optional "Exclude the seed artist" checkbox is unchecked by default;
checking it reranks with tracks by other artists, keeping your seed and qualities.
Its state stays selected across seed and ranker changes. If no candidates remain,
the page shows a status message and you can change the controls to restore results.
It is fully static: ranking runs in the browser from precomputed
data, with no server, accounts or analytics, and no audio. Each result links to
its Free Music Archive page and license.

## Build the bundle

```sh
musicdiscovery ingest                       # once; needs the catalog under data/
musicdiscovery export-demo build/demo       # writes the bundle and reports its size
python -m http.server -d build/demo 8000    # open http://localhost:8000/
```

| File | Purpose |
| --- | --- |
| `index.html` | standalone test page; mounts the demo in `#music-discovery-demo` |
| `demo.js`, `ranker.js`, `ui.js` | the UI, the JavaScript port of both rankers and shared UI pieces (ES modules) |
| `feedback-log.js` | the demo's local feedback log (see below) |
| `study.html`, `study.js`, `study-lib.js` | the blind listener study page (see below) |
| `demo.css` | styles, scoped to `.md-demo`; light and dark by `prefers-color-scheme` |
| `catalog.json` | compact catalog and precomputed data (see below); the first download |
| `sources.json` | FMA source page URL per track; fetched after the page is ready |
| `parity.json` | expected top 10 from Python on fixed queries; for tests, not vendored |
| `SOURCE.json` | the commit the bundle was built from (see below) |

`export-demo` prints each file's raw and gzip size and the bundle total, and
exits non-zero if the compressed bundle (every file listed above except
`parity.json`) reaches 2 MiB (the target for the small catalog). It also reports the first-load size (everything
but `sources.json`) and the deferred size separately. The generated bundle is never committed.

`SOURCE.json` records where the bundle came from:

```json
{
  "repo": "dflippojr/music-discovery",
  "sha": "<full git HEAD>",
  "built": "2026-10-16T09:30:00Z",
  "paths": ["src/musicdiscovery", "scripts/vendor-into-site"]
}
```

`paths` are the repository paths the bundle is built from, so a site can tell
whether `main` has changed them since. If tracked files have uncommitted changes,
`sha` ends in `-dirty`. `export-demo --release` refuses to export from such a work
tree, and `export-demo` fails outside a git checkout of this repository.

## What is in `catalog.json`

Per track: id, title, artist, top genre and license URL. Then
the data each ranker needs, as base64 little-endian integers:

- the 20-dimension PCA projection the baseline uses, as 16-bit integers with one
  scale, plus the three clipped axis z-scores (energy, brightness, noisiness);
- the hybrid's TF-IDF genre and tag matrix in sparse form, the popularity
  percentile, and the weights from `hybrid_config.json`.

Source page links are in `sources.json` (`{"version", "sources"}`, in catalog order),
which the page fetches after it is ready: results show their license link at once and
gain the source link when the file arrives. Both pages announce loading and failure.
Each catalog and source attempt has a 30-second application deadline covering
the request and reading/parsing its JSON body. Expiry aborts the request; the
deadline timer is cleared on success, failure or timeout. A catalog timeout shows
the page’s catalog alert and clears its busy state, with no automatic retry.
Source loading stays deferred and does not block the page.
If the file is unavailable (including HTTP, network, invalid JSON or timeout failures),
"Retry source links" fetches the same configured sources URL again. There are no
automatic retries or per-track requests; the button is disabled during a retry.
Recommendations, license links, ratings and study navigation remain usable.
Late completion from a timed-out attempt is ignored.
Successful recovery adds links once to the currently displayed results, announces
recovery and removes the retry button. Seeds, qualities, ratings, list order and
the study session id stay intact. You can retry again after another failure;
there is no need to refresh and lose your in-progress session.
Vendor both files together.

Quantisation error is far below the score gaps that decide the top 10, and the
parity test guards that.

## Parity and browser tests

`ranker.js` is a line-for-line port of `ranking.py` and `hybrid.py`. Both rankers
must return the same top 10 as Python on 24 fixed queries (likes and dislikes):

```sh
python scripts/build_demo_fixture.py .demo-fixture   # synthetic catalog, no dataset
npm ci && npx playwright install chromium
npm run test:parity    # Node: JavaScript vs Python top 10
npm run test:study     # Node: study list order, session id, export shape and the feedback log
npm run test:browser   # Chromium: CSP, console errors, toggle, axe-core, 320 px, ratings, score detail, log
```

The fixture build also exports a 400-track catalog shaped like the real one
(missing genres, sparse tags, non-Latin titles; see
[evaluation.md](evaluation.md#shape-matched-test-catalogs)) to
`.demo-fixture/shape`, and the parity test checks its exclusion queries too.

The browser test serves the bundle with `Content-Security-Policy: default-src
'self'; script-src 'self'; style-src 'self'`, fails on any console error, runs
axe-core in light and dark mode, and checks there is no horizontal scroll at 320 px.
The page uses no inline scripts, styles, handlers or third-party files.

## Score detail

Under each result's explanations, a closed "How this was scored" disclosure (a
native `details` element, so it opens with Enter or Space and announces its
state) lists every score term the ranker returned for that track, largest first:
a plain-language name, the signed contribution to two decimals and a bar. Bars
start at a zero line in the middle, grow right for positive and left for negative
terms, and are scaled to the largest term in that result; the bar is decorative
(`aria-hidden`) because the number carries the same information. The line above
the terms gives the total score. Term names read as follows:

| Term | Shown as |
| --- | --- |
| `similarity` (baseline), `audio` (hybrid) | Sounds like the seed |
| `metadata` | Shares genres and tags with the seed |
| `popularity` | Popular with listeners |
| `variety` | Adds variety to the list |
| `genre:<name>`, `tag:<name>` | Genre you picked: <name>, Tag you picked: <name> |
| `axis:<axis>+` or `-` | Sound you picked: the axis word, for example calmer |

A disliked genre or tag shows as a negative term. The study page does not show
explanations or this detail.

## Ratings

Thumbs up or down are kept in the page only. "Export my ratings" downloads JSON.
The normal demo exports `format: "music-discovery-ratings"`, `version: 2`, and
`ratings` records containing `track_id`, `rating` (1 or -1), `ranker`,
`seed_track_id`, `likes`, `dislikes`, and boolean `exclude_seed_artist`.
The added boolean records the exclusion setting when that rating was made,
including `false` by default. Ratings for the same query and track under different
exclusion settings are kept separately. No importer currently exists in this repo.
The blind listener study's controls, export schema and protocol are unchanged.

## Feedback log

Every thumbs action on the demo page, including taking a rating back, also
appends an event to a feedback log in the browser's `localStorage` (key
`music-discovery-feedback-log`). Unlike the in-page ratings, the log survives a
reload. It holds at most 2,000 events and drops the oldest first. If the browser
refuses storage (blocked site data, a full quota, some private windows), the page
keeps the log in memory for that visit and says so; it never fails the rating.
Each event holds:

- `version` (1) and `time` (UTC, to the second);
- `ranker`, `seed_track_ids`, `likes`, `dislikes` and `exclude_seed_artist`;
- `shown`, the whole list as shown: `track_id` and `rank` for each result;
- `track_id` and `rank` of the rated track, and `rating`: 1, -1, or 0 when an
  earlier rating of that track in that list was taken back;
- `explanation_terms`, the score terms behind the explanations the track showed,
  for example `["audio", "genre:Folk"]`.

The "Feedback log" section shows the number of entries, with "Download feedback
log" (JSON Lines, `music-discovery-feedback.jsonl`, one event per line) and
"Clear feedback log", which asks for confirmation first. Both are disabled while
the log is empty. The log is never sent anywhere. `src/musicdiscovery/feedback-log.schema.json`
describes one line, and `musicdiscovery feedback-report <log.jsonl>` summarises
a downloaded log; [evaluation.md](evaluation.md#feedback-logs-from-the-demo)
explains its output and limits. The study page has no feedback log.

## Links to results

The demo keeps its settings in the URL fragment, so a link opens the same list and
nothing about it reaches a server:

```text
#seed=<track id>&like=<quality>&dislike=<quality>&ranker=<hybrid|baseline>&exclude=1
```

`like` and `dislike` repeat, are URL-encoded and use the preference form from
[ranking.md](ranking.md#preference-input): `genre:Folk`, `tag:punk`, `more:energy`
or `less:energy`. An axis can be liked as `less:energy` or disliked as
`more:energy`, and a link restores exactly the chip and state it names. Omitted
keys mean the defaults: no qualities, the hybrid ranker and exclusion off
(`exclude=0` is accepted too). After every change that leaves a seed chosen, the
page replaces the fragment with its current state using `history.replaceState`,
so Back still leaves the page rather than stepping through settings. The keys are
written in a fixed order: seed, likes, dislikes, ranker, exclude. Clearing the seed
clears the fragment. "Copy link to these results", under the results, copies the
address; if the browser has no clipboard or refuses it, the page shows the address
in a selected field to copy by hand.

- **What is ignored.** Only values this page can show are applied: a track id in
  `catalog.json`, a quality that has a chip and a known ranker. The rest are
  ignored, and the results status adds "Some settings in this link are not
  available here and were ignored." A link without a usable seed still applies its
  valid qualities, ranker and exclusion but shows the usual empty state. A fragment
  with none of these keys, such as an ordinary in-page anchor, is left alone.
- **One demo per page.** The fragment is read and written only when the page has
  exactly one `[data-music-discovery]` root. With several, no demo reads or writes
  it and the copy button is not shown, since one fragment cannot describe them all.
- **Same catalog only.** Ranking is deterministic, so a link reproduces a list
  exactly only while the catalog it was made with is published. A rebuilt or
  different `catalog.json` can give a different list, or drop the seed track.
- **Read once.** The fragment is read when the catalog loads. Editing it by hand
  afterwards (`hashchange`) is ignored; reload the page to apply it.
- **Not the study.** `study.html` never reads or writes the fragment, so a link
  cannot unblind it. Ratings, the feedback log and its schema do not record links.

## Study page

`study.html` is the blind listener study: two unlabelled lists per task, ratings
and an overall preference, exported as one JSON file per session. It shares the
bundle, CSP rules and checks with the demo (`study.js`, `study-lib.js` and the
shared `ui.js`). The protocol, consent text and analysis plan are in
[listener-study.md](listener-study.md); `musicdiscovery analyze-study` turns the
returned files into a report.

## Putting it on dflippojr.dev

This repository only delivers the bundle. To copy it into a site checkout:

```sh
musicdiscovery export-demo --release build/demo
scripts/vendor-into-site build/demo "<site checkout>/public/<chosen path>"
```

The script copies the runtime files and `SOURCE.json`, and refuses a bundle
without `SOURCE.json` or with a `-dirty` SHA. The site's copy of `SOURCE.json`
uses the site's provenance format: `vendored`, the time of the copy, replaces
`built`. [production-signals.md](production-signals.md) says how to compare the
live copy with `main`.

The site's repository decides the path (it must be inside what that site deploys)
and any header or cache rules, as well as where the demo is linked from.
To embed it on another page, load `demo.css`, add `<div id="music-discovery-demo"
data-music-discovery></div>` and `<script type="module" src="…/demo.js">`; the
catalog is fetched from next to `demo.js` (or from `data-catalog`).

## Disposable UI pass

[UI testing contract](ui-testing.md) defines the synthetic-only launcher, journeys,
mobile/theme matrix, responsive checks and teardown. After installing the test
tools, use npm run ui:start for exploration, npm run ui:stop for cleanup, and
npm run test:ui -- --workers=1 for the committed Playwright Test journeys.
The test runner creates its own instance and refuses an occupied port.
