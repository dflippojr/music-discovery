# Working on music-discovery

Use Python 3.12 and a virtual environment. Install the package and dev tools:

```sh
python -m venv .venv
# Activate .venv using your shell's activation script.
python -m pip install -e ".[dev]"
```

Before committing, run:

```sh
ruff check .
ruff format --check .
pytest --cov=musicdiscovery --cov-report=term-missing --cov-report=xml:coverage.xml
```

Coverage below 98% fails the run. The floor is `fail_under` in `pyproject.toml`,
so local runs and CI use the same floor.

## How to prove a change works

- **Run what CI runs.** `scripts/ci-local.sh` runs the `test`, `demo` and `ui`
  jobs of `ci.yml` in order. Run it from Git Bash on Windows. Pass job names to
  run only some of them, for example `scripts/ci-local.sh test demo`. Before the
  first run, install the Node tools with `npm ci` and
  `npx playwright install chromium webkit`. `tests/test_workflows.py` fails
  when the script misses a command of those jobs or runs one out of order.
  The UI suite and the verify skill serve the demo on `127.0.0.1:$UI_PORT`
  (default 4173, as in CI). On a shared host where 4173 is taken, pick another
  unprivileged port, for example `UI_PORT=4180 scripts/ci-local.sh ui`.
  The commands, in CI order:

  ```sh
  # test
  ruff check .
  ruff format --check .
  pytest --cov=musicdiscovery --cov-report=term-missing --cov-report=xml:coverage.xml
  # test, Windows only: recommend to a console at its default encoding
  python scripts/build_console_catalog.py data/console-smoke
  musicdiscovery recommend --data-dir data/console-smoke --seed 100 -k 5
  # demo
  python scripts/build_demo_fixture.py .demo-fixture
  # demo: the bundle is stamped with the commit under test. CI wants $GITHUB_SHA;
  # locally it is `git rev-parse HEAD`, plus -dirty when `git status
  # --porcelain --untracked-files=no` lists a change or `git ls-files --others
  # --exclude-standard -- src/musicdiscovery scripts/vendor-into-site` lists a file.
  python -c 'import json; print(json.load(open(".demo-fixture/SOURCE.json"))["sha"])'
  npm run test:parity
  npm run test:study
  STUDY_EXPORT_DIR=.study-fixture COVERAGE_DIR=coverage-js npm run test:browser
  musicdiscovery analyze-study .study-fixture --out-dir .study-fixture/report
  # ui
  npm run test:ui -- --workers=1
  npm run ui:stop
  ```

- **UI changes.** Use the verify skill (`.claude/skills/verify/SKILL.md`), which
  follows `docs/ui-testing.md`, to check the change in the real demo page.
- **Bug fixes.** Write the test first and show that it fails on `main`.
  Then fix the bug and show that the same test passes.
- **PR body.** Fill in the `## Proof` table from the PR template: one row per
  acceptance criterion, giving how you verified it (command, test name or verify
  skill) and the evidence (output excerpt, artifact link, or red before and
  green after). Under `### Not verified`, list what you did not check and why.
  Under `### Gate changes`, list any test you deleted or skipped and any check
  you loosened, with the reason.

The required checks are the `gate` job in `ci.yml` and `gate-guard`. `gate`
passes only when the `test`, `demo`, `ui` and `gate-guard` jobs all passed.
`gate-guard` is the check run that `.github/workflows/gate-guard-target.yml`
posts from `main` under `pull_request_target`, so a PR can't rewrite its own
judge. Deleting or skipping a test, adding a retry, or editing workflows,
coverage or Sonar config needs the owner-applied `gate-change-approved` label and
an entry under `### Gate changes`. Never apply that label yourself. A push after
the owner applied it makes it stale, so the owner has to apply it again.
`tests/test_workflows.py` pins how both guards are wired.

`tests/test_ranker_contract.py` checks the laws every ranker keeps (result
shape, abstaining without signal, no crash on missing metadata, determinism,
true explanations and JS parity) on catalogs Hypothesis generates. The JS
parity law needs `node` on PATH. By default it runs the bounded, derandomized
`ci` profile from `tests/conftest.py`. Before changing a ranker, search much
further with the `deep` profile (a few minutes):

```sh
HYPOTHESIS_PROFILE=deep pytest tests/test_ranker_contract.py
```

A failure prints the shrunk catalog and query, plus a `@reproduce_failure`
line that replays it.

The `eval-gate` job in `ci.yml` is separate from pytest: it downloads the FMA
metadata, evaluates the rankers on the real catalog and fails when `hybrid` or
`profile` drops below the baseline in `reports/baseline.json`. It runs on
ranker-path pull requests and nightly. `docs/evaluation.md` ("The evaluation
gate") explains how to raise the baseline, which needs `gate-change-approved`.

Use `ruff format .` to format code. The package uses the `src/` layout;
tests require an installed package. CI runs these checks on Python 3.12. The
`test` job runs on both Linux and Windows; `demo` and `ui` run on Linux only.
On Windows, CI leaves the console at the runner's default encoding, so do not
set `PYTHONIOENCODING` or `PYTHONUTF8` in `ci.yml` to make a failure go away.
`tests/test_workflows.py` fails when a workflow names either one outside a comment.
`tests/test_ui_lifecycle.py` starts two real launchers on one port and stops
one, so #87's class of bug fails on the Windows leg.

Never commit datasets or audio, whether raw or generated. Keep datasets under
the ignored `data/` directory. Tests must use synthetic fixtures only and must
not download catalog data or call external services.
`tests/test_repo_hygiene.py` fails on any tracked path under `data/`, any
tracked audio or array file, and any tracked file over 1 MiB that is not on its
allow-list. `pytest-socket` (`--allow-hosts` in `pyproject.toml`) lets tests
bind any socket but connect only to `127.0.0.1` and `::1`.

SonarCloud settings live in `sonar-project.properties`. The SonarCloud workflow is
`.github/workflows/sonar.yml`; it imports the coverage that `ci.yml` uploads for the same
commit and the quality gate fails the check. CI itself is `.github/workflows/ci.yml`.
