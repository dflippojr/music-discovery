# music-discovery

[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=dflippojr_music-discovery&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=dflippojr_music-discovery)

Music discovery over a fixed, openly licensed catalog. Start from a track, describe the qualities you like, and get recommendations that explain why each one was picked. The point of the project is the evaluation: a simple similarity baseline is compared with a more capable ranker, first with offline metrics and then with real listeners, and the write-up says what those results can and cannot show.

No streaming-service API is involved; the catalog and its license are chosen up front.

## Status

A pilot-stage evaluation, not a product. Built and merged:

- Catalog ingest for the FMA `small` set: [docs/catalog.md](docs/catalog.md)
- Content-based baseline ranker with explanations: [docs/ranking.md](docs/ranking.md)
- Hybrid ranker with diversity re-ranking: [docs/ranking.md](docs/ranking.md)
- Offline evaluation harness and reports: [docs/evaluation.md](docs/evaluation.md), [reports/](reports/)
- Static demo page: [docs/demo.md](docs/demo.md)
- Listener study tooling (blind study page and analysis command): [docs/listener-study.md](docs/listener-study.md), [#9](https://github.com/dflippojr/music-discovery/issues/9)

Remaining:

- Run the listener pilot and publish the blind comparison: [#8](https://github.com/dflippojr/music-discovery/issues/8)

Results are in the reports, not restated here; the latest is [reports/offline-2026-10-06.md](reports/offline-2026-10-06.md).

## Where things are

- `reports/`: committed offline evaluation reports (`.md` and `.json`)
- `eval/queries.json`: the stored queries used by `evaluate`
- `scripts/`: helper scripts, including `vendor-into-site` for the demo

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
see [docs/catalog.md](docs/catalog.md). `export-demo <out_dir>` writes the static demo page bundle ([docs/demo.md](docs/demo.md)); `analyze-study <ratings dir>` reports on returned listener-study files ([docs/listener-study.md](docs/listener-study.md)). `recommend` ranks tracks
([docs/ranking.md](docs/ranking.md)) and `evaluate` scores rankers on stored
queries ([docs/evaluation.md](docs/evaluation.md)). Tests use synthetic fixtures
only; datasets and audio must never be committed. Store raw and generated datasets in `data/`.

## CI and dependency updates

Dependabot checks Python packages and GitHub Actions at the repository root,
and npm manifests at `/` and `/src/musicdiscovery/demo`, every week. The demo
manifest currently declares only its module type, with no dependencies to update.

The SonarCloud workflow analyses `src` and `tests`, imports Python and browser
JavaScript coverage, and waits for the quality gate. Keep SonarCloud Automatic
Analysis disabled when using this workflow. Fork PRs are skipped. Same-repository
PRs, including Dependabot PRs, run analysis when their token is available.

Store an authorized analysis token under the exact name `SONARCLOUD_TOKEN` in
both repository secret stores: **Settings → Secrets and variables → Actions**
and **Settings → Secrets and variables → Dependabot**. Dependabot-triggered
workflows use Dependabot secrets, rather than Actions secrets, through the same
`secrets.SONARCLOUD_TOKEN` reference. See the
[GitHub documentation](https://docs.github.com/en/code-security/reference/supply-chain-security/troubleshoot-dependabot/dependabot-on-actions).
When the token is absent, the job emits a notice and summary and skips analysis;
a green job in this state is not evidence of analysis or a passing quality gate.

After configuring the secrets and landing the workflow, verify a `main` push,
an ordinary same-repository PR, and a real Dependabot PR. Attach workflow-run and
SonarCloud PR/head-commit result links to [#31](https://github.com/dflippojr/music-discovery/issues/31).

## License

Code is MIT licensed (see [LICENSE](LICENSE)). Catalog data (FMA metadata and features) is CC BY 4.0; see [docs/catalog.md](docs/catalog.md) for the source, checksums and attribution.

Track metadata and audio features come from the Free Music Archive dataset (FMA), Defferrard et al., ISMIR 2017, <https://github.com/mdeff/fma>, licensed under CC BY 4.0.
