# music-discovery

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=dflippojr_music-discovery&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=dflippojr_music-discovery)

Music discovery over a fixed, openly licensed catalog. Start from a track, describe the qualities you like, and get recommendations that explain why each one was picked. The point of the project is the evaluation: a simple similarity baseline is compared with a more capable ranker, first with offline metrics and then with real listeners, and the write-up says what those results can and cannot show.

No streaming-service API is involved; the catalog and its license are chosen up front.

## Status

Planning. Nothing is built yet. The plan for v1 lives in the [issues](https://github.com/dflippojr/music-discovery/issues):

1. Choose a permitted catalog and ingest it
2. Project scaffold and CI, including SonarCloud
3. Preference input and a content-based baseline with explanations
4. Offline evaluation harness, then a hybrid ranker compared against the baseline
5. A static demo page and a small blind listener study

## Development

Use Python 3.12 and activate a virtual environment before installing:

```sh
python -m venv .venv
# Activate .venv using your shell's activation script.
python -m pip install -e ".[dev]"
ruff check .
ruff format --check .
pytest --cov=musicdiscovery --cov-report=term-missing --cov-report=xml:coverage.xml
musicdiscovery --version
```

`ingest`, `recommend`, and `evaluate` currently exit with status 1 and a
"not implemented yet" message. Tests use synthetic fixtures only; datasets
and audio must never be committed. Store raw and generated datasets in `data/`.

## License

Code is MIT licensed (see [LICENSE](LICENSE)). Catalog data keeps its own license, which will be recorded in `docs/catalog.md` once the catalog is chosen.
