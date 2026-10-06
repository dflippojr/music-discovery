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

Use `ruff format .` to format code. The package uses the `src/` layout;
tests require an installed package. CI runs these checks on Python 3.12.

Never commit datasets or audio, whether raw or generated. Keep datasets under
the ignored `data/` directory. Tests must use synthetic fixtures only and must
not download catalog data or call external services.

SonarCloud settings live in `sonar-project.properties`. The SonarCloud workflow is
`.github/workflows/sonar.yml`; it runs the tests for coverage and the quality gate
fails the check. CI itself is `.github/workflows/ci.yml`.
