"""Immich image provider: multi-account client, people resolution and face-aware selection."""

from app.providers.immich.client import ImmichGateway
from app.providers.immich.models import ImmichDisplayRequest
from app.providers.immich.people import PeopleDirectory
from app.providers.immich.provider import ImmichProvider

__all__ = ["ImmichDisplayRequest", "ImmichGateway", "ImmichProvider", "PeopleDirectory"]
