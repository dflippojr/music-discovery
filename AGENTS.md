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
  `npx playwright install chromium webkit`. The commands, in CI order:

  ```sh
  # test
  ruff check .
  ruff format --check .
  pytest --cov=musicdiscovery --cov-report=term-missing --cov-report=xml:coverage.xml
  # demo
  python scripts/build_demo_fixture.py .demo-fixture
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

The required check is the `gate` job in `ci.yml`. It passes only when the
`test`, `demo` and `ui` jobs all passed.

Use `ruff format .` to format code. The package uses the `src/` layout;
tests require an installed package. CI runs these checks on Python 3.12.

Never commit datasets or audio, whether raw or generated. Keep datasets under
the ignored `data/` directory. Tests must use synthetic fixtures only and must
not download catalog data or call external services.

SonarCloud settings live in `sonar-project.properties`. The SonarCloud workflow is
`.github/workflows/sonar.yml`; it imports the coverage that `ci.yml` uploads for the same
commit and the quality gate fails the check. CI itself is `.github/workflows/ci.yml`.
