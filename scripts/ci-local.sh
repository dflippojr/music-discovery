#!/usr/bin/env bash
# Run the same checks as .github/workflows/ci.yml, in the same order: the test,
# demo and ui jobs. Keep this file in step with ci.yml.
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
}

job_demo() {
  run python scripts/build_demo_fixture.py .demo-fixture
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
