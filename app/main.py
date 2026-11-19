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
from fastapi.responses import HTMLResponse, JSONResponse

from app import __version__
from app.api.routes import activity as activity_routes
from app.api.routes import auth as auth_routes
from app.api.routes import config as config_routes
from app.api.routes import context as context_routes
from app.api.routes import display as display_routes
from app.api.routes import fraimic as fraimic_routes
from app.api.routes import images as image_routes
from app.api.routes import immich as immich_routes
from app.api.routes import now_playing as now_playing_routes
from app.api.routes import playground as playground_routes
from app.api.routes import queue as queue_routes
from app.api.routes import refresh as refresh_routes
from app.api.routes import smart_mix as smart_mix_routes
from app.api.routes import system as system_routes
from app.api.routes import tmdb as tmdb_routes
from app.clients.http_images import ImageFetcher
from app.core.auth import AuthMiddleware, AuthState
from app.core.config_models import RuntimeConfig
from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.identity import load_identity
from app.core.limits import MULTIPART_OVERHEAD_BYTES, BodySizeLimitMiddleware
from app.core.logging_config import masking_filter, setup_logging
from app.core.proxy import RootPathMiddleware
from app.core.secrets_store import SecretsStore
from app.core.security import URLPolicy
from app.core.settings import EnvSettings, get_env_settings
from app.providers.immich import ImmichGateway, ImmichProvider, PeopleDirectory
from app.providers.music import ArtworkResolver
from app.providers.tmdb import PosterResolver, TmdbProvider
from app.services.activity import ActivityLog
from app.services.current_image import CurrentImageStore
from app.services.delivery import DeliveryService
from app.services.discovery import ZeroconfAdvertiser
from app.services.display import DisplayService
from app.services.ephemeral import EphemeralService
from app.services.now_playing import NowPlayingService
from app.services.scheduler import SchedulerService, immich_runner, tmdb_runner

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Fetch images from modular sources (Immich, TMDB posters, URLs), optimize them for a
color eInk panel and push them to a Fraimic frame.

**Authentication:** with `ADMIN_PASSWORD` set, every route except `/api/health`, the login
and the public images needs a web UI session or the API key. With only `APP_API_KEY`, every
`POST`/`PUT`/`DELETE` route and `/api/config` need the key. Send it as `X-API-Key`,
`Authorization: Bearer <key>` or `?api_key=`; use **Authorize** above.
"""


def _prepare_data_dir(settings: EnvSettings) -> None:
    for directory in (
        settings.data_dir,
        settings.cache_dir,
        settings.queue_dir,
        settings.assets_dir,
    ):
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
    activity = ActivityLog()
    auth_state = AuthState(
        SecretsStore(
            settings.secrets_path,
            env_api_key=settings.api_key,
            env_session_secret=settings.session_secret.get_secret_value(),
        ),
        password=settings.password,
        session_ttl_s=settings.session_ttl_hours * 3600,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        setup_logging(settings.log_level)
        activity.attach()
        _prepare_data_dir(settings)
        auth_state.load()
        identity = load_identity(settings.instance_path, settings.instance_name)
        app.state.identity = identity

        config_store = ConfigStore(settings.config_path, settings.runtime_seed())
        config_store.load()
        history_store = HistoryStore(settings.history_path)
        history_store.load()

        def _refresh_masked_secrets() -> None:
            masking_filter.set_secrets(
                [
                    *settings.secret_values(),
                    *config_store.secret_values(),
                    *auth_state.secrets.values(),
                ]
            )

        app.state.refresh_masked_secrets = _refresh_masked_secrets

        async def _on_config_change(_: RuntimeConfig) -> None:
            _refresh_masked_secrets()

        _refresh_masked_secrets()
        config_store.add_listener(_on_config_change)

        current_store = CurrentImageStore(settings.cache_dir)
        current_store.load()
        delivery = DeliveryService(
            settings.queue_dir, config_store, last_delivery_path=settings.last_delivery_path
        )
        delivery.load()
        display = DisplayService(config_store, current_store, delivery, history_store)
        display.add_listener(activity.record_render)
        url_policy = URLPolicy.from_entries(
            settings.block_private_urls, settings.url_allowlist_entries
        )
        immich_gateway = ImmichGateway(config_store, url_policy)
        people = PeopleDirectory(config_store, immich_gateway)
        immich = ImmichProvider(
            config_store,
            immich_gateway,
            people,
            history_store,
            max_bytes=settings.max_upload_bytes,
            family_override=lambda: scheduler.family_override(),
        )
        image_fetcher = ImageFetcher(url_policy, settings.max_upload_bytes)
        tmdb = TmdbProvider(config_store, image_fetcher)
        poster_resolver = PosterResolver(tmdb, image_fetcher, config_store, settings.assets_dir)
        artwork = ArtworkResolver(config_store, image_fetcher)
        now_playing = NowPlayingService(
            config_store, display, current_store, image_fetcher, artwork
        )
        scheduler = SchedulerService(
            settings.scheduler_state_path,
            config_store,
            {
                "immich": immich_runner(immich, display),
                "tmdb": tmdb_runner(poster_resolver, display),
            },
            has_pending=lambda: delivery.pending is not None,
        )
        scheduler.load()
        ephemeral = EphemeralService(
            current_store,
            display,
            delivery,
            clear_hold=scheduler.clear_hold,
            refresh=lambda: scheduler.run_in_background("revert"),
        )

        app.state.config_store = config_store
        app.state.history_store = history_store
        app.state.current_image_store = current_store
        app.state.delivery_service = delivery
        app.state.display_service = display
        app.state.people_directory = people
        app.state.immich_gateway = immich_gateway
        app.state.url_policy = url_policy
        app.state.immich_provider = immich
        app.state.scheduler_service = scheduler
        app.state.ephemeral_service = ephemeral
        app.state.image_fetcher = image_fetcher
        app.state.poster_resolver = poster_resolver
        app.state.tmdb_provider = tmdb
        app.state.activity_log = activity
        app.state.now_playing_service = now_playing

        await ephemeral.start()  # before the delivery worker can drop an expired image
        await delivery.start()
        await scheduler.start()
        advertiser: ZeroconfAdvertiser | None = None
        if settings.zeroconf_enabled:
            advertiser = ZeroconfAdvertiser(
                port=settings.port,
                version=__version__,
                root_path=settings.root_path,
                auth=auth_state.mode,
                instance=identity.instance_name,
                instance_id=identity.instance_id,
            )
            await advertiser.start()
        config = config_store.config
        logger.info(
            "Smart Frame Engine %s started (instance=%s, data_dir=%s, auth=%s, root_path=%r, "
            "panel=%s, power_mode=%s, pending=%s, scheduler=%s)",
            __version__,
            identity.instance_id,
            settings.data_dir,
            auth_state.mode,
            settings.root_path,
            config.fraimic.panel,
            config.fraimic.power_mode,
            delivery.pending is not None,
            scheduler.next_run_at.isoformat() if scheduler.next_run_at else "off",
        )
        try:
            yield
        finally:
            if advertiser is not None:
                await advertiser.stop()
            await scheduler.stop()
            await ephemeral.stop()
            await delivery.stop()
            await artwork.aclose()
            await image_fetcher.aclose()
            logger.info("Smart Frame Engine stopped")
            activity.detach()

    app = FastAPI(
        title="Smart Frame Engine",
        version=__version__,
        description=API_DESCRIPTION,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.auth_state = auth_state
    app.state.started_at = time.monotonic()

    # Middleware added last runs first. Order (outermost first):
    # RootPath -> BodySizeLimit -> CORS -> auth.
    # CORS wraps auth so 401 responses keep CORS headers; RootPath must run before auth
    # so protected paths are recognized under a sub-path.
    app.add_middleware(
        AuthMiddleware,
        state=auth_state,
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

    app.include_router(auth_routes.router)
    app.include_router(system_routes.router)
    app.include_router(config_routes.router)
    app.include_router(image_routes.router)
    app.include_router(display_routes.router)
    app.include_router(immich_routes.display_router)
    app.include_router(tmdb_routes.display_router)
    app.include_router(now_playing_routes.display_router)
    app.include_router(immich_routes.router)
    app.include_router(refresh_routes.router)
    app.include_router(smart_mix_routes.display_router)
    app.include_router(smart_mix_routes.router)
    app.include_router(context_routes.router)
    app.include_router(fraimic_routes.router)
    app.include_router(queue_routes.router)
    app.include_router(tmdb_routes.router)
    app.include_router(playground_routes.router)
    app.include_router(activity_routes.router)

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        """The web UI (relative URLs: works under any reverse-proxy sub-path)."""
        return HTMLResponse(auth_routes.page("index.html"), headers={"Cache-Control": "no-store"})

    return app
