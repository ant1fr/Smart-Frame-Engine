# Contributing

Thanks for helping! Bug reports, ideas and pull requests are all welcome.

## Quick start

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
DATA_DIR=./data python -m app                    # http://localhost:8080
```

Before opening a pull request, run what CI runs:

```bash
ruff check . && ruff format --check .
mypy app
pytest
```

## Workflow (Gitflow)

- `main` is the released code: never commit to it directly.
- `develop` is the integration branch. Branch from it and open pull requests **into `develop`**:
  `feat/<name>`, `fix/<name>`, `docs/<name>`, `chore/<name>`.
- Releases are cut from `develop` into `main` by the maintainer, then tagged `vX.Y.Z`.

## Commits and pull requests

- Write everything in **English**, with [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `test:`). The pull request title uses the
  same prefix: it decides the release-notes category.
- Keep changes focused, with tests (HTTP is mocked with `respx`). Strict typing and `ruff` are enforced.
- Never commit secrets, real IP addresses or personal photos.
- The UI is a single `app/templates/index.html` (Tailwind and Alpine from a CDN): no build step,
  relative URLs only (`./api/...`), so it works behind a sub-path proxy.

## Adding an image provider

Providers live in `app/providers/`. Implement the provider base class, return an `ImageAsset`,
register it where the existing providers are wired in `app/main.py`, and add tests next to the
existing provider tests. To make the source usable in Smart Mix, describe the rules it accepts in
`app/providers/<name>/rule_spec.py` (see `immich_rule_spec()`) and add it to `default_catalog()`
in `app/services/rule_catalog.py`: the Smart Mix editor builds its form from that descriptor, and
`tests/test_rule_catalog.py` checks it against your request model. Open a *Feature request* first if you want to discuss the design.
