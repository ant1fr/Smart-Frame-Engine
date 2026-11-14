"""FastAPI application factory.

Run with ``python -m app`` (recommended, honors ``PORT``/``FORWARDED_ALLOW_IPS``) or
``uvicorn --factory app.main:create_app``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app import __version__
from app.api.routes import config as config_routes
from app.api.routes import display as display_routes
from app.api.routes import fraimic as fraimic_routes
from app.api.routes import images as image_routes
from app.api.routes import system as system_routes
from app.core.auth import APIKeyMiddleware
from app.core.config_models import RuntimeConfig
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.limits import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
from app.core.logging_config import masking_filter, setup_logging
from app.core.proxy import RootPathMiddleware
from app.core.settings import EnvSettings, get_env_settings
from app.services.current_image import CurrentImageStore
from app.services.delivery import DeliveryService
from app.services.display import DisplayService

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Fetch images from modular sources (Immich, TMDB posters, URLs), optimize them for a
color eInk panel and push them to a Fraimic frame.

**Authentication:** when `APP_API_KEY` is set, every `POST`/`PUT`/`DELETE` route and
`/api/config` require the `X-API-Key` header (or `?api_key=`). Use **Authorize** above.
"""


def _prepare_data_dir(settings: EnvSettings) -> None:
    for directory in (settings.data_dir, settings.cache_dir, settings.queue_dir):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Data directory {directory} is not writable ({exc}). "
                "Check the /data volume mount and its ownership (container runs as uid 1000)."
            ) from exc


def create_app(settings: EnvSettings | None = None) -> FastAPI:
    """Build the FastAPI application.

    Args:
        settings: Environment settings; defaults to :func:`get_env_settings`.
    """
    settings = settings or get_env_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level)
        _prepare_data_dir(settings)

        config_store = ConfigStore(settings.config_path, settings.runtime_seed())
        config_store.load()
        history_store = HistoryStore(settings.history_path)
        history_store.load()

        def _refresh_masked_secrets() -> None:
            masking_filter.set_secrets([*settings.secret_values(), *config_store.secret_values()])

        async def _on_config_change(_: RuntimeConfig) -> None:
            _refresh_masked_secrets()

        _refresh_masked_secrets()
        config_store.add_listener(_on_config_change)

        current_store = CurrentImageStore(settings.cache_dir)
        current_store.load()
        delivery = DeliveryService(settings.queue_dir, config_store)
        delivery.load()
        display = DisplayService(config_store, current_store, delivery)

        app.state.config_store = config_store
        app.state.history_store = history_store
        app.state.current_image_store = current_store
        app.state.delivery_service = delivery
        app.state.display_service = display

        await delivery.start()
        config = config_store.config
        logger.info(
            "Smart Frame Engine %s started (data_dir=%s, auth=%s, root_path=%r, panel=%s, "
            "power_mode=%s, pending=%s)",
            __version__,
            settings.data_dir,
            "enabled" if settings.auth_enabled else "disabled",
            settings.root_path,
            config.fraimic.panel,
            config.fraimic.power_mode,
            delivery.pending is not None,
        )
        try:
            yield
        finally:
            await delivery.stop()
            logger.info("Smart Frame Engine stopped")

    app = FastAPI(
        title="Smart Frame Engine",
        version=__version__,
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.started_at = time.monotonic()

    # Middleware added last runs first. Order (outermost first):
    # RootPath -> BodySizeLimit -> CORS -> auth.
    # CORS wraps auth so 401 responses keep CORS headers; RootPath must run before auth
    # so protected paths are recognized under a sub-path.
    app.add_middleware(
        APIKeyMiddleware,
        api_key=settings.api_key,
        public_image_endpoints=settings.public_image_endpoints,
    )
    origins = settings.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_body_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES,
    )
    app.add_middleware(RootPathMiddleware, root_path=settings.root_path)

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        """Report request validation errors without echoing submitted values."""
        errors: list[dict[str, Any]] = [
            {"loc": list(err.get("loc", ())), "msg": err.get("msg", ""), "type": err.get("type")}
            for err in exc.errors()
        ]
        return JSONResponse({"detail": jsonable_encoder(errors)}, status_code=422)

    app.include_router(system_routes.router)
    app.include_router(config_routes.router)
    app.include_router(image_routes.router)
    app.include_router(display_routes.router)
    app.include_router(fraimic_routes.router)

    @app.get("/", include_in_schema=False)
    async def index(request: Request) -> RedirectResponse:
        """Redirect to the API docs (the web UI replaces this in a later milestone)."""
        root_path: str = request.scope.get("root_path", "")
        return RedirectResponse(url=f"{root_path}/docs", status_code=307)

    return app
