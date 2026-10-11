# UI testing contract

Contract version: 1

## Scope and isolation

Target: music-discovery's static recommendation demo and blind listener study.
Only http://127.0.0.1:4173 is allowed. Never attach to an existing instance.
The launcher binds loopback exclusively before generating a fresh synthetic
catalog with scripts/build_demo_fixture.py (450 made-up tracks, IDs 100–549).
Data stays in ignored data/ui-testing/run-<random>/; its owned process handle
is data/ui-testing/instance.json. No catalog data or audio is downloaded.
The pages have no backend, accounts, telemetry, email, payments, webhooks or
model calls. Source and license links are rewritten to local synthetic stubs;
CSP restricts requests to self. The launcher refuses DEMO_BUNDLE, DATABASE_URL,
API_KEY, OPENAI_API_KEY and nonempty MUSICDISCOVERY_* or FMA_* configuration.
Unknown launcher arguments (including endpoints and credentials) are rejected.
No credentials or secrets are necessary. Browser tests additionally reject
non-loopback requests and create fresh, nonpersistent contexts.

Tools: Python 3.12 in .venv, Node 24, npm, gh 2.99+ for screenshot uploads,
and installed playwright-cli. From the repository root, create .venv using
Python 3.12, activate it, then run python -m pip install -e ".[dev]", npm ci,
and npx playwright install chromium webkit (add --with-deps on Linux).

## Start

One command, from the repo root in PowerShell or POSIX shell:

~~~sh
npm run ui:start
~~~

Keep this foreground process in an owned terminal. It prints
UI_URL=http://127.0.0.1:4173/ after seeding and binding, followed by UI_INSTANCE,
PID and HANDLE. Readiness: GET /__ui_health returns that instance ID. Startup
logs go to that terminal; redirect them to the evidence directory if desired.
Occupied ports or an existing handle fail; no existing server is reused.
Partial seed failure closes the socket and removes its own files and handle.
SIGTERM, Ctrl+C or the stop command triggers cleanup. Register teardown in a
finally/trap before launching. Never use the handle's token in evidence.

## Seed and sign-in

Authentication: none; role: anonymous synthetic visitor. The deterministic
catalog has Synthetic track 100 by artist0. Search for Synthetic track 100 and
choose the exact matching title/artist, or ArrowDown/Enter. Random track selection
must result in any synthetic label; do not assert a particular random track.
Reset command: npm run ui:stop followed by npm run ui:start. For independent
browser/device/theme cases, use fresh contexts and reload: all writes are
in page memory, never shared with another context or saved on the server.
Study order and session ID are intentionally random.

## Journeys

| ID | Path / readiness | Actions and observable outcomes | Reload / audit |
| --- | --- | --- | --- |
| D1 search | /; combobox visible, source links loaded | Search nonexistent text: no options/results. Search Synthetic track 100, inspect dropdown, Escape cancels, reopen with arrow keys, select with Enter or pointer/tap; ten explained results and ten local source/license links. Clear search removes results. Random pick restores results. | Reload clears seed/results. No audit/backend. |
| D2 preferences | /; ten results | Cycle Calmer neutral → liked → disliked → neutral. Toggle Baseline/Hybrid and exclude seed artist; seed/qualities/exclusion survive ranker changes, results update, exclusion contains no artist0. | Reload resets preferences to Hybrid, no exclusion. |
| D3 ratings | /; ten results | Up toggles on/off; down replaces up. Export enables only with ratings. Download JSON v2 with seed, ranker, qualities, exclusion and rating; exported contents match choices. Return to same query restores its in-page rating. | Reload discards ratings and disables export, explicitly advertised. No import/edit/delete database paths; remove by toggling same rating. |
| S1 consent/search | /study.html; Listener study heading and consent button | Consent opens Task 1 of 5. Show lists disabled without a seed. Search/cancel/invalid query and select as D1, cycle qualities; Show lists reveals two five-track lists A/B without ranker labels/explanations. | Reload returns to consent with new session; persistence deliberately absent. |
| S2 rating/validation | /study.html; lists A/B | Next task disabled until all ten tracks rated and preference chosen. Toggle up/down, new-to-me; preference A/B/none. Next advances and clears seed/qualities for the next task. | No server writes or audit; progress is memory only. |
| S3 complete/export | /study.html; Task 1 | Complete all five tasks; Finish leads to All done. Download synthetic study JSON: five complete tasks, ten ratings each, preference, new-to-me flags, random ID, blind rankers only in file. | Reload returns to consent; downloaded file remains valid independently. |
| E1 unavailable/recovery | both pages | Locally intercept catalog with 503: visible load alert, busy removed. Intercept sources with 503: source unavailable status, retry enabled, licenses and ratings usable. Remove interception and retry: source links recovered once, choices/ratings unchanged. Restore routes. | Reload with routes removed restores healthy page. |
| N1 links/navigation | both pages | Open local source and license popup, verify synthetic stub, close popup. Fresh deep link /study.html starts consent. Navigate between URLs and browser Back returns useful page. | Back may restore browser page cache or fresh state; no promised persisted study. |

CRUD and audit logs: not applicable; this is a static, read-only catalog with
local ephemeral rating controls and downloadable files. Do not invent an
account, persisted write or audit feed. After each rating/preference transition
verify its UI state; verify reload resets for every journey that changes state.

## Theme and layout

Use media preference light/dark; no app switch. Verify computed --md-bg is
#ffffff/light or #14171a/dark. Primary states: demo empty/results, study
consent/seed/lists/done. Test every journey on desktop 1440x900 and iPhone SE,
iPhone 15, Pixel 7, iPad Mini in both themes, phones/tablet portrait/landscape.
The typeahead is the menu and its partly entered query is the form for live
resize: open suggestions at 1440, shrink stepwise to 375 and return to 1440
without reload; retain query, active option, selected ranker/quality, focus,
scroll (apart from legitimate clamping); Escape closes and Enter selects.
Repeat rotation mid-query on each phone. Check 320/360/375/390/414/600/768/820/
1024/1280/1440/1920 widths, 1280x600, 1024x500, 2560 wide, 200% root-font
scaling at desktop/phone, and focused field at 60% viewport height.
No modal, drawer, fixed header/footer, swipe/drag, chart, PWA manifest,
service worker or standalone install branch exists: those checks are N/A.
Physical notch insets, OS keyboard, browser zoom and native device behavior
need real hardware; root-font scaling and reduced viewport height are
approximations only. Input is text with autocomplete off and inherited 16px
font at default scaling. Source/license popups must remain local.

## Playwright and CI

npm with committed package-lock.json. playwright.config.mjs and tests/ui/*.spec.mjs
use Playwright Test with desktop Chromium light/dark and iPhone SE WebKit
light/dark projects. Command: npm run test:ui -- --workers=1. No retries/skips.
webServer runs npm run ui:start with reuseExistingServer=false and local
health readiness. Global teardown invokes npm run ui:stop; each fresh browser
context rejects external requests. Run npm run ui:stop even after interruption.
Each test starts from reload/fresh context; no shared mutating records exist.
Reports: playwright-report/ and test-results/ (ignored), HTML/JUnit and traces
on failure. Exploratory evidence: output/ui-pass/music-discovery/YYYY-MM-DD/.
GitHub visibility verified public. Existing runners are ubuntu-latest;
UI CI MUST use GitHub-hosted ubuntu-latest, never a self-hosted runner.
The scoped UI job installs Python 3.12/package, Node 24, npm ci, Chromium and
WebKit with dependencies, runs all projects with one worker, always tears down
and uploads reports/traces. Existing Python/Node/CSP/Sonar checks stay intact.

## Teardown

One command from the repo root, safe to repeat:

~~~sh
npm run ui:stop
~~~

It validates the health instance ID before sending an authenticated local stop
request. The server finally closes its socket and removes only its flat owned
bundle and handle. Unexpected directories/symlinks or a mismatched health ID
are refused; investigate rather than deleting another process's resources.
If interrupted before a handle exists, stop reports no owned instance.
Verify the ledger PID has exited, port 4173 no longer listens, and handle and
run directory are absent. Preserve screenshots/reports. Close owned named
playwright-cli sessions promptly; run playwright-cli close-all at completion
in this dedicated worktree namespace. Never close another worker's sessions.
