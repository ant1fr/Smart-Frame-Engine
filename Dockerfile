# syntax=docker/dockerfile:1
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Smart Frame Engine" \
      org.opencontainers.image.description="Immich / TMDB images optimized for color eInk and pushed to a Fraimic frame" \
      org.opencontainers.image.source="https://github.com/ant1fr/Smart-Frame-Engine" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8080

WORKDIR /app

RUN groupadd --system --gid 1000 app \
 && useradd --system --uid 1000 --gid app --home-dir /app --no-create-home \
      --shell /usr/sbin/nologin app \
 && mkdir -p /data \
 && chown app:app /data

# Install dependencies first so code changes do not invalidate this layer.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
      > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

COPY README.md LICENSE ./
COPY app ./app
RUN pip install --no-deps .

# numba compiles the dithering kernels on first use (seconds on a Raspberry Pi). Compile them
# at build time into an image-local cache so the first render is fast; the app user may
# refresh it, and DATA_DIR/.numba_cache is the fallback outside Docker.
ENV NUMBA_CACHE_DIR=/app/.numba_cache
RUN mkdir -p /app/.numba_cache && chown app:app /app/.numba_cache

# Marks images built outside a release tag (``+<build>`` after the version in the UI); empty for
# release images. Declared late so changing it never invalidates the layers above.
ARG APP_BUILD=""
ENV APP_BUILD=${APP_BUILD}

USER app
RUN python -c "from app.processor.dither import warm_up; warm_up()"
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8080'), timeout=4)"]

CMD ["python", "-m", "app"]
