"""Container entry point: ``python -m app``.

Starts Uvicorn with reverse-proxy support (``X-Forwarded-Proto``/``X-Forwarded-For``)
using the ``HOST``, ``PORT``, ``LOG_LEVEL`` and ``FORWARDED_ALLOW_IPS`` settings.
"""

from __future__ import annotations

import uvicorn

from app.core.logging_config import setup_logging
from app.core.settings import get_env_settings


def main() -> None:
    """Configure logging and serve the application."""
    settings = get_env_settings()
    setup_logging(settings.log_level)
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        proxy_headers=True,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_config=None,
        log_level=settings.log_level.lower(),
        server_header=False,
    )


if __name__ == "__main__":
    main()
