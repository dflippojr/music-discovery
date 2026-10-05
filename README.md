# music-discovery

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

`musicdiscovery ingest` downloads the FMA metadata archive (about 342 MiB),
verifies its checksum and writes the cleaned `small` catalog under `data/`;
see [docs/catalog.md](docs/catalog.md). `recommend` and `evaluate` currently
exit with status 1 and a "not implemented yet" message. Tests use synthetic fixtures only; datasets
and audio must never be committed. Store raw and generated datasets in `data/`.

## License

Code is MIT licensed (see [LICENSE](LICENSE)). Catalog data (FMA metadata and features) is CC BY 4.0; see [docs/catalog.md](docs/catalog.md) for the source, checksums and attribution.

Track metadata and audio features come from the Free Music Archive dataset (FMA), Defferrard et al., ISMIR 2017, <https://github.com/mdeff/fma>, licensed under CC BY 4.0.
