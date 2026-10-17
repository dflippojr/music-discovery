# Production signals

The only deployed copy of this project is the static demo on dflippojr.dev,
vendored into the personal-website repository at `public/demo/music-discovery`
(see [demo.md](demo.md#putting-it-on-dflippojrdev)). It has no server, so there
are no health endpoints, error logs or metrics. These are the signals it has.
Every command below only reads; none changes the site, a repository or a run.

| Signal | Where it comes from | Expected freshness |
| --- | --- | --- |
| Deployed commit | the live `SOURCE.json` | no more than 1 day behind `main` on `paths` |
| Live-monitor result | personal-website's live monitor | a run within the last 2 hours |
| Real use | feedback logs visitors download and send | none; only as fresh as the newest log received |

## Deployed commit

`export-demo` stamps the bundle with `SOURCE.json`, and `scripts/vendor-into-site`
copies it into the site with the time of the copy as `vendored`. Read the live copy:

```sh
curl -fsS https://dflippojr.dev/demo/music-discovery/SOURCE.json
```

It holds `repo`, `sha` (the full commit), `vendored` (ISO time) and `paths`.
Compare `sha` with `main`, counting only changes to `paths`, because other changes
do not reach the bundle:

```sh
live=$(curl -fsS https://dflippojr.dev/demo/music-discovery/SOURCE.json \
  | python -c 'import json, sys; print(json.load(sys.stdin)["sha"])')
gh api "repos/dflippojr/music-discovery/compare/$live...main" \
  --jq '{behind_by: .ahead_by, bundle_files: [.files[].filename
    | select(startswith("src/musicdiscovery/") or . == "scripts/vendor-into-site")]}'
```

An empty `bundle_files` list means the live demo matches `main`, whatever
`behind_by` says. Otherwise the live demo is stale, and its age is the date of the
oldest commit after `$live` that touches those files. The site's daily freshness
job (personal-website#216) is meant to re-vendor within a day, so a stale demo
older than a day means that job has stopped or failed. A missing `SOURCE.json`
(HTTP 404) means the live bundle predates the stamp.

## Live-monitor result

personal-website's scheduled live monitor (personal-website#215) checks the live
pages, the demo page among them, every 30 minutes. Until that workflow exists
this signal is missing. Once it does, read its latest run (the repository is
private, so `gh` must be signed in as the owner):

```sh
gh run list -R dflippojr/personal-website --workflow live-monitor.yml --limit 1 \
  --json conclusion,createdAt,headSha,url
```

A `failure` conclusion is a live defect. A newest run older than 2 hours means the
monitor itself has stopped.

## Real use: feedback logs

The demo never uploads anything. A visitor's thumbs actions stay in their
browser until they choose "Download feedback log" and send the file on
([demo.md](demo.md#feedback-log)). Logs therefore arrive by hand, rarely and
unevenly, and there is no freshness bound to alarm on: the signal is as fresh as
the newest log received, and silence is normal.

Keep received logs under the ignored `data/` directory and summarise them there:

```sh
mkdir -p data/feedback
musicdiscovery feedback-report data/feedback/music-discovery-feedback.jsonl
```

[evaluation.md](evaluation.md#feedback-logs-from-the-demo) explains the report
and its limits. Never commit a log: it records what one visitor searched for and
rated. The report on stdout holds only per-ranker counts, rates and term counts,
and may be quoted in an issue or report. The skipped-line messages on stderr can
quote a log's contents, so do not paste those.
