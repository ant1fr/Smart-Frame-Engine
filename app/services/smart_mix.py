"""Smart Mix: weighted, source-agnostic choice of the next :class:`SourceRule`.

The resolver knows nothing about providers: it only orders configured entries (and an
optional temporary context boost) by weight. The scheduler walks that order, re-rolling when
an entry has no candidate or fails.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.core.config_models import SmartMixConfig, SourceRule

DEFAULT_CONTEXT_WEIGHT = 3.0


class MixContext(BaseModel):
    """A temporary boost (or override) of the playlist, e.g. pushed by Home Assistant.

    The rule joins the playlist with ``weight`` until ``expires_at``. With ``exclusive`` it is
    the only candidate meanwhile.
    """

    model_config = ConfigDict(extra="ignore")

    rule: SourceRule
    weight: float = Field(default=DEFAULT_CONTEXT_WEIGHT, gt=0, le=1000)
    expires_at: AwareDatetime
    exclusive: bool = False
    label: str | None = Field(default=None, max_length=80)

    def live(self, now: datetime) -> bool:
        """Whether the context has not expired yet."""
        return now < self.expires_at


@dataclass(frozen=True, slots=True)
class MixCandidate:
    """One pickable rule. ``index`` is the position in ``smart_mix.entries`` (``None`` for
    the context boost or a plain pinned/last rule)."""

    rule: SourceRule
    weight: float = 1.0
    index: int | None = None
    label: str | None = None


class SmartMixResolver:
    """Orders candidates by weighted random sampling without replacement."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()  # noqa: S311 - not security relevant

    @staticmethod
    def pool(config: SmartMixConfig, context: MixContext | None = None) -> list[MixCandidate]:
        """Enabled entries plus the live context (alone when it is exclusive)."""
        boost = (
            MixCandidate(context.rule, context.weight, None, context.label or "context")
            if context is not None
            else None
        )
        if boost is not None and context is not None and context.exclusive:
            return [boost]
        pool = [
            MixCandidate(entry.to_rule(), entry.weight, index, entry.label)
            for index, entry in enumerate(config.entries)
            if entry.enabled
        ]
        if boost is not None:
            pool.append(boost)
        return pool

    def order(
        self,
        config: SmartMixConfig,
        context: MixContext | None = None,
        *,
        last_index: int | None = None,
    ) -> list[MixCandidate]:
        """Every candidate once, in the order they should be tried.

        Higher weights tend to come first (Efraimidis-Spirakis keys). With
        ``avoid_consecutive`` the entry shown last is moved to the end, so it is still a
        fallback when everything else fails.
        """
        pool = self.pool(config, context)
        keyed = sorted(
            pool,
            key=lambda candidate: math.log(1.0 - self._rng.random()) / candidate.weight,
            reverse=True,
        )
        if config.avoid_consecutive and last_index is not None and len(keyed) > 1:
            keyed.sort(key=lambda candidate: candidate.index == last_index)  # stable
        return keyed
