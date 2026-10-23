---
name: verify-music-discovery
description: Launch and check music-discovery in the real demo page, not only in unit tests. Starts a disposable synthetic instance, runs doctor checks, drives the main recommendation path with Playwright, saves screenshots and a summary, and cleans up every process it started. Use when asked to "verify this change", "launch and check", "does it work in the app", or "smoke test" the demo.
---

# Verify music-discovery in the app

[docs/ui-testing.md](../../../docs/ui-testing.md) is the contract for the
disposable instance: isolation, start, seed, teardown and crash recovery. This
skill runs those steps in order and records evidence. It does not change them.
Read the contract's "Scope and isolation" section first, and install the
tools it lists under "Tools".

## Run it

From the repository root, with `.venv` active:

```sh
python .claude/skills/verify/scripts/verify.py                    # whole main path
python .claude/skills/verify/scripts/verify.py --feature rankers  # one area
```

The script exits 0 only when every step passes, including cleanup. It prints
the evidence folder and its `summary.md`. Paste that summary into the PR or
your report. Run one verify at a time per checkout, because the contract
allows one owned instance.

The instance listens on `http://127.0.0.1:$UI_PORT`, with `UI_PORT`
defaulting to 4173. When 4173 is taken on a shared host, set another port from
1024 to 65535, for example `UI_PORT=4180 python
.claude/skills/verify/scripts/verify.py`. `verify.py --port`, `doctor.py --url`
and `VERIFY_BASE_URL` override it. An invalid `UI_PORT` stops the run before
anything starts.

## Steps

1. **Launch.** If `data/ui-testing/instance.json` already exists, another
   instance owns the checkout: the step fails at once, names that instance and
   leaves it running. Otherwise it runs the command behind `npm run ui:start`,
   as the contract's "Start" section describes, as an owned background
   process. Its output goes to `launcher.log`. Before waiting for readiness, `launch.json` records the
   wrapper's PID and creation time, the port, the URL and the log path. Once the
   `UI_INSTANCE` line appears, it adds the instance, its run directory and the
   launcher's child PIDs; it records them while waiting too, so a launcher that
   crashes before readiness still counts as this run's. It never stores the
   handle's token.
2. **Doctor.** `scripts/doctor.py` checks four things and prints PASS or FAIL for
   each. **ready**: `/__ui_health` returns the launched instance. **loopback**:
   the URL is 127.0.0.1 and the port listens only on loopback. **seed**: the
   catalog holds *Synthetic track 100* by artist0, every title is synthetic and
   every source and license link is local. **environment**: the launcher's own
   forbidden-configuration check passes. You can run it on its own against an
   instance you started: `python .claude/skills/verify/scripts/doctor.py`.
3. **Drive.** `scripts/drive.spec.mjs` runs with `scripts/drive.config.mjs`,
   using one worker in desktop (1440x900) and phone (Pixel 7) projects. It
   reuses `ready`, `pickSeed` and the external-request guard from
   `tests/ui/fixtures.mjs`. Use `--feature` to drive one row of the feature map.
4. **Evidence.** Everything goes in `.verify/<UTC timestamp>/`, which git
   ignores: `launch.json`, `launcher.log`, `doctor.txt`, `drive.txt`,
   `screenshots/<feature>-<desktop|phone>.png`, Playwright failure traces in
   `drive-results/`, `cleanup.txt` and `summary.md`.
5. **Cleanup.** This step always runs, even when an earlier step fails. It
   stops the recorded drive process and its browsers, then runs the contract's
   "Teardown" (`npm run ui:stop`), but only when the handle is this run's: it
   names the launched instance, or a recorded launcher process wrote it. If a
   recorded launcher PID is still alive after that, it stops that PID. It never
   picks a process by name or port. If this run's handle is still there, it runs
   the contract's "Recovery after a launcher crash" (`npm run ui:recover`). A
   handle another instance owns is left alone and noted in `cleanup.txt`. Then
   it checks that no recorded process is running, that the port has no listener
   (when this run launched anything), and that this run's handle and run
   directory are gone. If anything is left over, it fails and lists it. In
   that case, follow the contract's recovery section. Do not delete files by hand.

## Feature map

| Feature | Page / route | How it is driven | What proves it |
| --- | --- | --- | --- |
| `seed` | `/` search combobox | `pickSeed`: type *Synthetic track 100*, then Enter | Input shows *Synthetic track 100 — artist0*; ten results |
| `qualities` | `/` "Calmer" chip | Click once | `data-state="like"`; ten results |
| `rankers` | `/` Ranker radios | Like Calmer, then check Baseline and Hybrid in turn | Each ranker shows ten results, and each result has a "Why this track" item |
| `ratings` | `/` result controls and "Export my ratings" | Like Calmer, choose Baseline, thumbs up the first result, export | The downloaded JSON v2 holds that track, `baseline`, seed 100 and `less:energy` |

If your change touches the study page (`/study.html`) or another journey, the
contract's "Journeys" table and `npm run test:ui -- --workers=1` cover it.
Add a row here only when the main path changes.
