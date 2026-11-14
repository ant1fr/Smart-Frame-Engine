# Smart Frame Engine

Self-hosted web app and REST API that fetches images from modular sources (Immich,
TMDB movie/TV posters, direct URLs), optimizes them for a color eInk panel (Spectra 6),
and pushes them to a [Fraimic](https://fraimic.com) picture frame. Home Assistant
friendly, runs standalone in Docker.

> **Status:** early development (`v0.x`). The core service, configuration, security and
> container packaging are in place; image providers, the eInk pipeline and the web UI
> are being added milestone by milestone.

## Quickstart

```bash
cp .env.example .env        # then fill in your values
mkdir -p data               # must be writable by PUID:PGID (default 1000:1000)
docker compose up -d
```

- API docs: `http://<host>:8000/docs`
- Health: `http://<host>:8000/api/health`

Runtime settings are stored in `./data/config.json` and can be edited with
`GET`/`PUT /api/config`. Values saved there take precedence over `.env`, which in turn
takes precedence over built-in defaults. `POST /api/config/reset` removes saved values.

## Development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy app && pytest
DATA_DIR=./data python -m app      # serve on http://localhost:8000
```

## Credits

The `.bin` encoding follows the official
[Fraimic Bin Converter](https://github.com/Fraimic/fraimic_bin_converter) (MIT).

## License

[MIT](LICENSE)
