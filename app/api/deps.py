"""FastAPI dependencies giving routes access to application-scoped services."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import Depends, Request

from app.core.config_store import ConfigStore
from app.core.history_store import HistoryStore
from app.core.settings import EnvSettings


def get_settings(request: Request) -> EnvSettings:
    """Environment settings the application was created with."""
    return cast(EnvSettings, request.app.state.settings)


def get_config_store(request: Request) -> ConfigStore:
    """The runtime configuration store (initialized during app startup)."""
    return cast(ConfigStore, request.app.state.config_store)


def get_history_store(request: Request) -> HistoryStore:
    """The display history store (initialized during app startup)."""
    return cast(HistoryStore, request.app.state.history_store)


SettingsDep = Annotated[EnvSettings, Depends(get_settings)]
ConfigStoreDep = Annotated[ConfigStore, Depends(get_config_store)]
HistoryStoreDep = Annotated[HistoryStore, Depends(get_history_store)]
