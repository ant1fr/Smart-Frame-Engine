# Security Policy

## Supported versions

Security fixes go to the latest release (`v1.x`). Please update before reporting.

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting: [Security → Report a vulnerability](https://github.com/ant1fr/Smart-Frame-Engine/security/advisories/new).
Include what you found, how to reproduce it, and the version (or commit) you tested.

You can expect an acknowledgement within a few days. Fixes are developed privately, released,
then disclosed in the release notes; reporters are credited unless they prefer otherwise.

## Scope and hardening notes

Smart Frame Engine is meant for a home network. Things worth knowing:

- With neither `ADMIN_PASSWORD` nor `APP_API_KEY` set, the API and UI are **open**. That is
  intended for a trusted LAN; set `ADMIN_PASSWORD` before exposing the engine anywhere else.
- Secrets (Immich and TMDB keys, the API key) are never returned by the API and are masked in
  logs. They live in `/data/config.json` and `/data/secrets.json`: keep that folder private.
- Outgoing image fetches can be restricted with `BLOCK_PRIVATE_URLS` and `URL_ALLOWLIST`.
- Behind a reverse proxy, set `FORWARDED_ALLOW_IPS` to the proxy's address and serve it over HTTPS.
