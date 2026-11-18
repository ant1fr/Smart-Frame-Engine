# Roadmap

Live planning happens on the **[roadmap board](https://github.com/users/ant1fr/projects/1)**
(GitHub Projects): every item is an issue you can comment on or vote for. This file is the
short, stable summary. Dates are intentionally absent: this is a hobby-scale project and
priorities follow what people actually use. Ideas are welcome in
[Discussions](https://github.com/ant1fr/Smart-Frame-Engine/discussions).

## v1.0.0: the foundation (done)

- [x] Immich provider: persons (any/all), albums, favorites, CLIP smart search, "on this day",
      multiple accounts, anti-repeat history, burst cooldown
- [x] Photo quality gate (screenshots, low resolution, documents, blank frames)
- [x] Movie and TV posters (TMDB) with the no-crop poster template, regional ranking and fallbacks
- [x] Now-playing album covers (Deezer, MusicBrainz / Cover Art Archive), sleeve and vinyl styles,
      one picture per album so the frame does not refresh on every track
- [x] Spectra 6 pipeline: automatic profile and dithering per image, official `.bin` encoding
- [x] Delivery for mains-powered (`continuous`) and battery (`deepsleep`) frames, with an upload queue
- [x] Ephemeral images with automatic restore, autonomous scheduler with quiet hours
- [x] Smart Mix weighted playlists, Random Discovery quota
- [x] Family Mode (members, private people and albums) and the always-on Immich Locked Folder rule
- [x] Ambient context API (`presence`, `weather`, `season`, boosts, `family_mode`) with per-field TTLs
- [x] Web UI: dashboard, playground (render lab), settings; admin login, API key, session security
- [x] Zeroconf / mDNS advertisement
- [x] Multi-arch Docker images, CI, release pipeline, documentation wiki

## v1.1.0: Editorial templates

New picture types built from data instead of photos, in the same Spectra 6-friendly style:

- [ ] **Quote** card: a quote of the day, or yours ([#23](https://github.com/ant1fr/Smart-Frame-Engine/issues/23))
- [ ] **Weather** card: current conditions and a short forecast
- [ ] **Agenda** card: today's calendar events
- [ ] **Collage**: several photos composed on one frame
- [ ] Template picker in the playground and the Smart Mix, so cards can join the rotation

## v1.2.0: GenAI and custom engines

- [ ] **Custom engines**: a documented plugin interface for your own image sources (beyond Immich
      and TMDB), starting with a local-folder and a URL/RSS source
- [ ] **Generative art** from a prompt (bring your own API key or local model), tuned for six inks
- [ ] Captions and "memory" cards written from a photo's metadata

## Later / ideas

- [ ] Dedicated Home Assistant integration (`Smart-Frame-Engine-HA`) using the Zeroconf service,
      installable through HACS
- [ ] Several frames from one engine
- [ ] Palette calibration for individual panels

Have a different priority? [Open a feature request](https://github.com/ant1fr/Smart-Frame-Engine/issues/new/choose).
