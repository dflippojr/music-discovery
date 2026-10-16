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

To prove a change works in the real demo page, use the verify skill
(`.claude/skills/verify/SKILL.md`), which follows `docs/ui-testing.md`.

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

Use `ruff format .` to format code. The package uses the `src/` layout;
tests require an installed package. CI runs these checks on Python 3.12.

Never commit datasets or audio, whether raw or generated. Keep datasets under
the ignored `data/` directory. Tests must use synthetic fixtures only and must
not download catalog data or call external services.

SonarCloud settings live in `sonar-project.properties`. The SonarCloud workflow is
`.github/workflows/sonar.yml`; it imports the coverage that `ci.yml` uploads for the same
commit and the quality gate fails the check. CI itself is `.github/workflows/ci.yml`.
