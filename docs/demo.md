# Static demo page

A small page that lets a visitor pick a seed track, like or dislike qualities,
and read explained recommendations from the hybrid ranker (the default) or the
baseline. It is fully static: ranking runs in the browser from precomputed
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
| `demo.js`, `ranker.js` | the UI and the JavaScript port of both rankers (ES modules) |
| `demo.css` | styles, scoped to `.md-demo`; light and dark by `prefers-color-scheme` |
| `catalog.json` | compact catalog and precomputed data (see below) |
| `parity.json` | expected top 10 from Python on fixed queries; for tests, not vendored |

`export-demo` prints each file's raw and gzip size and the bundle total, and
exits non-zero if the compressed bundle reaches 2 MiB (the target for the small
catalog). The generated bundle is never committed.

## What is in `catalog.json`

Per track: id, title, artist, top genre, license URL and source page link. Then
the data each ranker needs, as base64 little-endian integers:

- the 20-dimension PCA projection the baseline uses, as 16-bit integers with one
  scale, plus the three clipped axis z-scores (energy, brightness, noisiness);
- the hybrid's TF-IDF genre and tag matrix in sparse form, the popularity
  percentile, and the weights from `hybrid_config.json`.

Quantisation error is far below the score gaps that decide the top 10, and the
parity test guards that.

## Parity and browser tests

`ranker.js` is a line-for-line port of `ranking.py` and `hybrid.py`. Both rankers
must return the same top 10 as Python on 24 fixed queries (likes and dislikes):

```sh
python scripts/build_demo_fixture.py .demo-fixture   # synthetic catalog, no dataset
npm ci && npx playwright install chromium
npm run test:parity    # Node: JavaScript vs Python top 10
npm run test:browser   # Chromium: CSP, console errors, toggle, axe-core, 320 px, ratings
```

The browser test serves the bundle with `Content-Security-Policy: default-src
'self'; script-src 'self'; style-src 'self'`, fails on any console error, runs
axe-core in light and dark mode, and checks there is no horizontal scroll at 320 px.
The page uses no inline scripts, styles, handlers or third-party files.

## Ratings

Thumbs up or down are kept in the page only. "Export my ratings" downloads JSON:
`track_id`, `rating` (1 or -1), `ranker`, `seed_track_id`, `likes`, `dislikes`.
The listener study builds on this.

## Putting it on dflippojr.dev

This repository only delivers the bundle. To copy it into a site checkout:

```sh
scripts/vendor-into-site build/demo ../personal-website/demo/music-discovery
```

Where it is linked from, and any change to that site, happen in its own repository.
To embed it on another page, load `demo.css`, add `<div id="music-discovery-demo"
data-music-discovery></div>` and `<script type="module" src="…/demo.js">`; the
catalog is fetched from next to `demo.js` (or from `data-catalog`).
