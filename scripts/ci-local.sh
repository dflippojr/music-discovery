#!/usr/bin/env bash
# Run the same checks as .github/workflows/ci.yml, in the same order: the test,
# demo and ui jobs. Keep this file in step with ci.yml: tests/test_workflows.py
# fails when a command of those jobs is missing here, unless its CI_ONLY says why.
#
# Run it from the repository root with .venv active, after the one-time setup:
#   python -m pip install -e ".[dev]"
#   npm ci
#   npx playwright install chromium webkit
# On Windows, run it from Git Bash.
#
# Usage: scripts/ci-local.sh [test|demo|ui]...   (no argument runs all three)
set -euo pipefail

cd "$(dirname "$0")/.."

run() {
  echo
  echo "==> $*"
  "$@"
}

job_test() {
  run ruff check .
  run ruff format --check .
  run pytest --cov=musicdiscovery --cov-report=term-missing --cov-report=xml:coverage.xml
  # CI runs the console smoke on its Windows leg only, at the default encoding.
  case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*)
      run python scripts/build_console_catalog.py data/console-smoke
      run python -c "import sys; e = sys.stdout.encoding; print('stdout encoding:', e); sys.exit('stdout is UTF-8, so the smoke cannot catch #28' if e.lower() == 'utf-8' else 0)"
      run musicdiscovery recommend --data-dir data/console-smoke --seed 100 -k 5
      ;;
  esac
}

job_demo() {
  run python scripts/build_demo_fixture.py .demo-fixture
  # CI compares the stamp with $GITHUB_SHA. Here the commit under test is HEAD,
  # with -dirty while tracked files have uncommitted changes, as build_source()
  # in src/musicdiscovery/demo.py stamps it.
  echo
  echo "==> Check the bundle is stamped with the commit under test"
  local sha expected
  sha=$(python -c 'import json; print(json.load(open(".demo-fixture/SOURCE.json"))["sha"])')
  echo "SOURCE.json sha: $sha"
  expected=$(git rev-parse HEAD)
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    expected="$expected-dirty"
  fi
  if [ "$sha" != "$expected" ]; then
    echo "SOURCE.json is not stamped with the commit under test, $expected" >&2
    exit 1
  fi
  run npm run test:parity
  run npm run test:study
  echo
  echo "==> STUDY_EXPORT_DIR=.study-fixture COVERAGE_DIR=coverage-js npm run test:browser"
  STUDY_EXPORT_DIR=.study-fixture COVERAGE_DIR=coverage-js npm run test:browser
  run musicdiscovery analyze-study .study-fixture --out-dir .study-fixture/report
}

job_ui() {
  # Stop the owned disposable instance even when the journeys fail.
  trap 'npm run ui:stop' EXIT
  run npm run test:ui -- --workers=1
  run npm run ui:stop
  trap - EXIT
}

jobs=("$@")
if [ ${#jobs[@]} -eq 0 ]; then
  jobs=(test demo ui)
fi

for job in "${jobs[@]}"; do
  case "$job" in
    test | demo | ui) "job_$job" ;;
    *)
      echo "Unknown job: $job (expected test, demo or ui)" >&2
      exit 2
      ;;
  esac
done

echo
echo "All CI jobs passed locally: ${jobs[*]}"
