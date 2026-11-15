"""Autonomous refresh: replay the active source rule on a timer, outside quiet hours.

The **active rule** is, in order of precedence:

1. the pinned ``scheduler.source_rule`` from the configuration;
2. the Smart Mix playlist (``smart_mix.enabled`` with at least one enabled entry, or a live
   context boost): each refresh tries entries in weighted random order and re-rolls when one
   has no candidate or fails;
3. the last manual display request (kept in ``/data/scheduler_state.json`` so that showing a
   photo does not rewrite the user's settings).

Timing:

* The next refresh is due ``interval_hours`` after the timer was last reset. **Any
  display resets the timer** (a manual photo, an upload, a scheduled refresh), so a
  picture chosen by hand stays up for a full interval.
* A refresh that falls inside the quiet window runs when the window ends.
* After a failure (Immich down, nothing eligible...) the refresh is retried after
  ``retry_minutes``, doubling on each consecutive failure and capped by the interval.
* A scheduled refresh is skipped while the previous image still waits for a sleeping
  frame (``skip_if_pending``); skipped slots restart the timer.

A single APScheduler ``DateTrigger`` job re-arms itself after every run and whenever
the configuration changes. The state survives restarts; a refresh that became due while
the app was down runs shortly after startup. Manual refreshes (``POST /api/refresh``)
ignore quiet hours and the pending check, and share a lock with scheduled ones.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, ValidationError

from app.core.config_models import RuntimeConfig, SchedulerConfig, SourceRule
from app.core.config_store import ConfigStore
from app.core.context import RuleContext, expand_params
from app.core.json_store import atomic_write_json, read_json
from app.core.security import InvalidImageError
from app.providers.base import ProviderError
from app.providers.immich import ImmichDisplayRequest, ImmichProvider
from app.services.display import DisplayResult, DisplayService
from app.services.schedule import QuietWindow, in_quiet, next_run_at
from app.services.smart_mix import MixCandidate, MixContext, SmartMixResolver

logger = logging.getLogger(__name__)

JOB_ID: Final = "refresh"
DEFAULT_MIN_DELAY_S: Final = 30.0
_MAX_BACKOFF_EXPONENT: Final = 16

RefreshReason = Literal["scheduled", "manual"]
RunStatus = Literal["ok", "failed", "skipped"]
RuleSource = Literal["pinned", "smart_mix", "last"]
Clock = Callable[[], datetime]
RuleRunner = Callable[[SourceRule], Awaitable[DisplayResult]]

#: Request fields that describe one action rather than the selection; never replayed.
NON_REPLAYED_FIELDS: Final = frozenset(
    {"push_to_fraimic", "ignore_history", "ephemeral", "hold_minutes"}
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Errors -------------------------------------------------------------------------
class RefreshError(Exception):
    """Base class of refresh errors (messages are safe to show to API clients)."""


class RefreshBusyError(RefreshError):
    """Another refresh is already running."""


class NoRuleError(RefreshError):
    """There is no rule to replay yet."""


class UnsupportedRuleError(RefreshError):
    """The active rule's source cannot be replayed (its provider is not available)."""


class InvalidRuleError(RefreshError):
    """The active rule's parameters are not a valid request for its source."""


# --- State --------------------------------------------------------------------------
class LastRun(BaseModel):
    """Outcome of the most recent refresh attempt (scheduled or manual)."""

    at: AwareDatetime
    reason: RefreshReason
    status: RunStatus
    rule_kind: str | None = None
    entry_label: str | None = Field(default=None, description="Smart Mix entry that was shown")
    entry_index: int | None = Field(default=None, description="Position in smart_mix.entries")
    detail: str | None = Field(default=None, description="Skip reason or error message")


class SchedulerState(BaseModel):
    """Scheduler bookkeeping persisted to ``scheduler_state.json``."""

    model_config = ConfigDict(extra="ignore")

    last_rule: SourceRule | None = Field(
        default=None, description="Last manual display request (replayed when nothing is pinned)"
    )
    last_display_at: AwareDatetime | None = None
    timer_reset_at: AwareDatetime | None = Field(
        default=None, description="When the refresh timer last restarted (display or skip)"
    )
    retry_at: AwareDatetime | None = Field(
        default=None, description="When a failed scheduled refresh is retried"
    )
    last_run: LastRun | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_mix_index: int | None = Field(
        default=None, description="Smart Mix entry shown last (avoid_consecutive)"
    )
    context: MixContext | None = Field(
        default=None, description="Temporary Smart Mix boost or override"
    )
    hold_until: AwareDatetime | None = Field(
        default=None, description="A pushed image stays up at least until then"
    )


@dataclass(frozen=True, slots=True)
class ActiveRule:
    """What a refresh replays, and where it comes from.

    ``rule`` is the single rule of a pinned/last source; it is ``None`` for ``smart_mix``,
    where the rule is chosen per refresh.
    """

    rule: SourceRule | None
    source: RuleSource

    @property
    def kind(self) -> str:
        """Source kind of the rule, or ``smart_mix`` for the playlist."""
        return self.rule.kind if self.rule is not None else "smart_mix"


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Result of a successful refresh."""

    reason: RefreshReason
    active: ActiveRule
    display: DisplayResult
    rule: SourceRule
    entry_label: str | None = None


class QuietHoursView(BaseModel):
    """The configured quiet window (local time of ``timezone``)."""

    start: str
    end: str


class ActiveRuleView(BaseModel):
    """Which rule a refresh would replay."""

    kind: str
    source: RuleSource = Field(
        description="pinned = scheduler.source_rule; smart_mix = the weighted playlist; "
        "last = the last manual display request"
    )


class ContextView(BaseModel):
    """The temporary Smart Mix boost, as exposed by the API."""

    label: str | None
    kind: str
    weight: float
    exclusive: bool
    expires_at: AwareDatetime


class SmartMixStatus(BaseModel):
    """Smart Mix summary for ``GET /api/status``."""

    enabled: bool
    entries: int = Field(description="Enabled playlist entries")
    context: ContextView | None = Field(description="Live temporary boost, if any")


class SchedulerSnapshot(BaseModel):
    """Scheduler state exposed through ``GET /api/status`` (no network access)."""

    enabled: bool
    interval_hours: float
    timezone: str
    quiet_hours: QuietHoursView | None = Field(description="null when quiet hours are off")
    in_quiet_hours: bool
    next_run_at: AwareDatetime | None = Field(
        description="Next scheduled refresh in the scheduler timezone (null when disabled)"
    )
    running: bool = Field(description="Whether a refresh is in progress")
    last_run: LastRun | None
    last_display_at: AwareDatetime | None
    consecutive_failures: int
    hold_until: AwareDatetime | None = Field(
        description="A directly pushed image is protected from scheduled refreshes until then"
    )
    smart_mix: SmartMixStatus
    active_rule: ActiveRuleView | None = Field(description="null until something can be replayed")


def quiet_window(config: SchedulerConfig) -> QuietWindow | None:
    """The configured quiet window, or ``None`` when quiet hours are off."""
    return QuietWindow.parse(config.quiet_hours_start, config.quiet_hours_end)


def _safe_detail(exc: BaseException) -> str:
    """A client-safe description of a refresh failure."""
    if isinstance(exc, ProviderError | RefreshError | InvalidImageError):
        return str(exc) or type(exc).__name__
    return "Unexpected error (see the application logs)"


# --- Service ------------------------------------------------------------------------
class SchedulerService:
    """Owns the refresh timer, the replayable rule and the scheduler state file."""

    def __init__(
        self,
        state_path: Path,
        config_store: ConfigStore,
        runners: Mapping[str, RuleRunner],
        *,
        has_pending: Callable[[], bool] = lambda: False,
        clock: Clock = _utcnow,
        min_delay_s: float = DEFAULT_MIN_DELAY_S,
        resolver: SmartMixResolver | None = None,
    ) -> None:
        """Create the service (call :meth:`load` then :meth:`start`).

        Args:
            state_path: Location of ``scheduler_state.json``.
            config_store: Runtime configuration (the ``scheduler`` section).
            runners: Coroutine replaying a rule, per :attr:`SourceRule.kind`.
            has_pending: Whether an image still waits for the frame.
            clock: Returns the current aware time (tests inject a fake clock).
            min_delay_s: Minimum delay before a refresh, so an overdue run does not
                fire in the middle of startup or of a configuration edit.
            resolver: Weighted Smart Mix ordering (tests inject a seeded one).
        """
        self._path = state_path
        self._config_store = config_store
        self._runners = dict(runners)
        self._has_pending = has_pending
        self._clock = clock
        self._min_delay = timedelta(seconds=min_delay_s)
        self._resolver = resolver or SmartMixResolver()
        self._lock = asyncio.Lock()
        self._state = SchedulerState()
        self._scheduler: AsyncIOScheduler | None = None
        self._next_run_at: datetime | None = None
        self._listening = False

    # --- Lifecycle ----------------------------------------------------------------------
    def load(self) -> SchedulerState:
        """Restore the state file, ignoring it (with a warning) when it is malformed."""
        raw = read_json(self._path, lambda: None)
        state = SchedulerState()
        if raw is not None:
            try:
                state = SchedulerState.model_validate(raw)
            except ValidationError:
                logger.warning("Ignoring malformed scheduler state in %s", self._path)
        self._state = state
        self._next_run_at = self.compute_next_run()
        return state

    async def start(self) -> None:
        """Start the timer and follow configuration changes."""
        if not self._listening:
            self._config_store.add_listener(self._on_config_change)
            self._listening = True
        if self._scheduler is None:
            scheduler = AsyncIOScheduler(timezone=UTC)
            scheduler.start()
            self._scheduler = scheduler
        self._arm()

    async def stop(self) -> None:
        """Stop the timer (a refresh in progress is cancelled)."""
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
            # AsyncIOScheduler shuts down on the next loop iteration; let it run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    # --- Read access ------------------------------------------------------------------
    @property
    def state(self) -> SchedulerState:
        """The current scheduler state."""
        return self._state

    @property
    def next_run_at(self) -> datetime | None:
        """When the next scheduled refresh runs (``None`` when disabled)."""
        return self._next_run_at

    @property
    def running(self) -> bool:
        """Whether a refresh is in progress."""
        return self._lock.locked()

    def now(self) -> datetime:
        """The current time of the service clock."""
        return self._clock()

    @property
    def supported_kinds(self) -> list[str]:
        """Source kinds that can be replayed (the runner registry)."""
        return sorted(self._runners)

    def _live_hold(self) -> datetime | None:
        hold = self._state.hold_until
        return hold if hold is not None and hold > self._clock() else None

    def context_view(self) -> ContextView | None:
        """API view of the live context boost."""
        context = self.live_context()
        if context is None:
            return None
        return ContextView(
            label=context.label,
            kind=context.rule.kind,
            weight=context.weight,
            exclusive=context.exclusive,
            expires_at=context.expires_at,
        )

    def mix_pool(self) -> list[MixCandidate]:
        """Candidates currently in the Smart Mix (enabled entries plus the live context)."""
        return self._resolver.pool(self._config.smart_mix, self.live_context())

    def live_context(self) -> MixContext | None:
        """The Smart Mix context boost, unless it has expired."""
        context = self._state.context
        return context if context is not None and context.live(self._clock()) else None

    def active_rule(self) -> ActiveRule | None:
        """The pinned rule, else the Smart Mix, else the last manual rule, else ``None``."""
        pinned = self._config.scheduler.source_rule
        if pinned is not None:
            return ActiveRule(pinned, "pinned")
        mix = self._config.smart_mix
        if mix.enabled and self._resolver.pool(mix, self.live_context()):
            return ActiveRule(None, "smart_mix")
        if self._state.last_rule is not None:
            return ActiveRule(self._state.last_rule, "last")
        return None

    def in_quiet_hours(self) -> bool:
        """Whether the current time falls inside the configured quiet window."""
        config = self._config.scheduler
        return in_quiet(self._clock(), quiet_window(config), ZoneInfo(config.timezone))

    def snapshot(self) -> SchedulerSnapshot:
        """Current scheduler state for the status endpoint."""
        config = self._config.scheduler
        active = self.active_rule()
        quiet = (
            QuietHoursView(start=config.quiet_hours_start, end=config.quiet_hours_end)
            if config.quiet_hours_start and config.quiet_hours_end
            else None
        )
        return SchedulerSnapshot(
            enabled=config.enabled,
            interval_hours=config.interval_hours,
            timezone=config.timezone,
            quiet_hours=quiet,
            in_quiet_hours=self.in_quiet_hours(),
            next_run_at=self._next_run_at,
            running=self.running,
            last_run=self._state.last_run,
            last_display_at=self._state.last_display_at,
            consecutive_failures=self._state.consecutive_failures,
            hold_until=self._live_hold(),
            smart_mix=SmartMixStatus(
                enabled=self._config.smart_mix.enabled,
                entries=sum(1 for e in self._config.smart_mix.entries if e.enabled),
                context=self.context_view(),
            ),
            active_rule=(
                ActiveRuleView(kind=active.kind, source=active.source) if active else None
            ),
        )

    def compute_next_run(self) -> datetime | None:
        """When the next scheduled refresh should run, from the state and the config."""
        config = self._config.scheduler
        if not config.enabled:
            return None
        now = self._clock()
        state = self._state
        if state.retry_at is not None:
            due = state.retry_at
        elif state.timer_reset_at is not None:
            due = state.timer_reset_at + timedelta(hours=config.interval_hours)
        else:
            due = now
        if state.hold_until is not None:
            due = max(due, state.hold_until)
        due = max(due, now + self._min_delay)
        return next_run_at(due, quiet_window(config), ZoneInfo(config.timezone))

    # --- Operations -------------------------------------------------------------------
    async def run(self, reason: RefreshReason = "manual") -> RefreshOutcome:
        """Replay the active rule now, ignoring quiet hours and the pending check.

        A successful refresh resets the timer. A failed manual refresh is recorded in
        :attr:`SchedulerState.last_run` but does not count as a scheduled failure.

        Raises:
            RefreshBusyError: Another refresh is running.
            NoRuleError: Nothing to replay yet.
            UnsupportedRuleError: The rule's source cannot be replayed.
            InvalidRuleError: The rule's parameters are invalid.
            ProviderError: The image source failed (see :mod:`app.providers.base`).
            InvalidImageError: The source returned bytes that are not an image.
        """
        if self._lock.locked():
            raise RefreshBusyError("A refresh is already running")
        try:
            async with self._lock:
                active = self.active_rule()
                if active is None:
                    raise NoRuleError(
                        "Nothing to refresh yet: display a photo first, or pin a rule in "
                        "scheduler.source_rule"
                    )
                try:
                    display, chosen = await self._play(active)
                except Exception as exc:
                    await self._save(
                        last_run=self._last_run(reason, "failed", active, _safe_detail(exc))
                    )
                    raise
                await self._record_success(reason, active, chosen)
                return RefreshOutcome(
                    reason=reason,
                    active=active,
                    display=display,
                    rule=chosen.rule,
                    entry_label=chosen.label,
                )
        finally:
            self._arm()

    async def tick(self) -> LastRun | None:
        """Scheduled refresh (the timer callback). Never raises.

        Returns:
            The recorded outcome, or ``None`` when nothing was attempted (disabled, or a
            manual refresh is already running and will re-arm the timer itself).
        """
        if self._lock.locked():
            logger.info("Scheduled refresh skipped: a refresh is already running")
            return None
        try:
            async with self._lock:
                return await self._scheduled()
        except Exception:
            logger.exception("Scheduled refresh crashed")
            return None
        finally:
            self._arm()

    async def record_manual(
        self,
        rule: SourceRule | None,
        *,
        ephemeral: bool = False,
        hold_minutes: int | None = None,
    ) -> None:
        """Note a direct display: it resets the timer and never touches the configuration.

        Args:
            rule: The request to replay from now on, or ``None`` for sources that cannot
                be replayed (e.g. an uploaded file), which keep the previous rule.
            ephemeral: A temporary push (movie poster, now playing...): it does not become
                the fallback ``last_rule``.
            hold_minutes: Protect the image from scheduled refreshes for at least this long
                (the next refresh is not due before then, even if the interval is shorter).
        """
        now = self._clock()
        update: dict[str, Any] = self._timer_reset(now)
        if rule is not None and not ephemeral:
            update["last_rule"] = rule
        if hold_minutes:
            update["hold_until"] = now + timedelta(minutes=hold_minutes)
        await self._save(**update)
        self._arm()

    async def set_context(self, context: MixContext | None) -> None:
        """Set (or clear with ``None``) the temporary Smart Mix boost."""
        await self._save(context=context)
        self._arm()

    # --- Internals --------------------------------------------------------------------
    @property
    def _config(self) -> RuntimeConfig:
        return self._config_store.config

    def _runner(self, rule: SourceRule) -> RuleRunner:
        runner = self._runners.get(rule.kind)
        if runner is None:
            raise UnsupportedRuleError(f"Replaying {rule.kind!r} rules is not supported yet")
        return runner

    async def _scheduled(self) -> LastRun | None:
        config = self._config.scheduler
        if not config.enabled:
            return None
        if self.in_quiet_hours():
            # The configuration changed after the timer was armed: wait for the window
            # end without restarting the interval.
            return await self._skip("Quiet hours", None, restart_timer=False)
        hold = self._live_hold()
        if hold is not None:
            # A direct push arrived after the timer was armed: leave it up until the hold ends.
            return await self._skip(
                f"A pushed image is held until {hold.isoformat()}", None, restart_timer=False
            )
        active = self.active_rule()
        if active is None:
            return await self._skip(
                "No rule to replay yet: display a photo or pin scheduler.source_rule", None
            )
        if config.skip_if_pending and self._has_pending():
            return await self._skip("The previous image is still waiting for the frame", active)
        if active.rule is not None:
            try:
                self._runner(active.rule)
            except UnsupportedRuleError as exc:
                return await self._skip(str(exc), active)

        try:
            _, chosen = await self._play(active)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, ProviderError | RefreshError | InvalidImageError):
                logger.exception("Scheduled refresh failed")
            return await self._record_failure(active, exc)
        return await self._record_success("scheduled", active, chosen)

    async def _play(self, active: ActiveRule) -> tuple[DisplayResult, MixCandidate]:
        """Run the active rule; a Smart Mix tries up to ``max_attempts`` entries.

        Raises:
            The error of the last attempt when every attempt failed.
        """
        if active.rule is not None:
            return await self._attempt(MixCandidate(active.rule))
        mix = self._config.smart_mix
        order = self._resolver.order(
            mix, self.live_context(), last_index=self._state.last_mix_index
        )
        failure: Exception | None = None
        for candidate in order[: mix.max_attempts]:
            try:
                return await self._attempt(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not isinstance(exc, ProviderError | RefreshError | InvalidImageError):
                    logger.exception("Smart Mix entry %r crashed", candidate.label)
                logger.info(
                    "Smart Mix entry %r skipped, trying another: %s",
                    candidate.label or candidate.rule.kind,
                    _safe_detail(exc),
                )
                failure = exc
        if failure is None:  # pragma: no cover - active_rule() guarantees a candidate
            raise NoRuleError("Smart Mix has no enabled entry")
        raise failure

    async def _attempt(self, candidate: MixCandidate) -> tuple[DisplayResult, MixCandidate]:
        rule = candidate.rule.model_copy(update={"params": self.expand(candidate.rule.params)})
        runner = self._runner(rule)
        return await runner(rule), candidate

    def expand(self, params: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Resolve ``{season}``-style placeholders against the current time."""
        context = RuleContext(
            self._clock(),
            ZoneInfo(self._config.scheduler.timezone),
            self._config.smart_mix.hemisphere,
        )
        expanded = expand_params(params, context)
        return expanded if isinstance(expanded, dict) else params

    def _last_run(
        self,
        reason: RefreshReason,
        status: RunStatus,
        active: ActiveRule | None,
        detail: str | None = None,
        chosen: MixCandidate | None = None,
    ) -> LastRun:
        rule = chosen.rule if chosen is not None else (active.rule if active else None)
        return LastRun(
            at=self._clock(),
            reason=reason,
            status=status,
            rule_kind=rule.kind if rule is not None else None,
            entry_label=chosen.label if chosen is not None else None,
            entry_index=chosen.index if chosen is not None else None,
            detail=detail,
        )

    @staticmethod
    def _timer_reset(now: datetime) -> dict[str, Any]:
        return {
            "last_display_at": now,
            "timer_reset_at": now,
            "retry_at": None,
            "consecutive_failures": 0,
            "hold_until": None,
        }

    async def _record_success(
        self, reason: RefreshReason, active: ActiveRule, chosen: MixCandidate
    ) -> LastRun:
        last_run = self._last_run(reason, "ok", active, chosen=chosen)
        update = self._timer_reset(last_run.at)
        if active.source == "smart_mix":
            update["last_mix_index"] = chosen.index
        await self._save(last_run=last_run, **update)
        logger.info("Refresh done (%s, %s rule %s)", reason, active.source, last_run.rule_kind)
        return last_run

    async def _skip(
        self, detail: str, active: ActiveRule | None, *, restart_timer: bool = True
    ) -> LastRun:
        last_run = self._last_run("scheduled", "skipped", active, detail)
        update: dict[str, Any] = {"last_run": last_run}
        if restart_timer:
            update.update(timer_reset_at=last_run.at, retry_at=None)
        await self._save(**update)
        logger.info("Scheduled refresh skipped: %s", detail)
        return last_run

    async def _record_failure(self, active: ActiveRule, exc: Exception) -> LastRun:
        config = self._config.scheduler
        failures = self._state.consecutive_failures + 1
        exponent = min(failures - 1, _MAX_BACKOFF_EXPONENT)
        delay = min(
            timedelta(minutes=config.retry_minutes * 2**exponent),
            timedelta(hours=config.interval_hours),
        )
        last_run = self._last_run("scheduled", "failed", active, _safe_detail(exc))
        await self._save(
            last_run=last_run, consecutive_failures=failures, retry_at=last_run.at + delay
        )
        logger.warning(
            "Scheduled refresh failed (%d in a row), retrying in %s: %s",
            failures,
            delay,
            last_run.detail,
        )
        return last_run

    async def _save(self, **update: Any) -> None:
        """Apply ``update`` to the state and persist it (a write error is only logged)."""
        self._state = self._state.model_copy(update=update)
        payload = self._state.model_dump(mode="json")
        try:
            await asyncio.to_thread(atomic_write_json, self._path, payload)
        except OSError:
            logger.exception("Could not write the scheduler state to %s", self._path)

    def _arm(self) -> None:
        """Recompute the next refresh and (re)schedule or remove the timer job."""
        if self._lock.locked():
            return  # the refresh in progress re-arms the timer when it finishes
        previous, self._next_run_at = self._next_run_at, self.compute_next_run()
        scheduler = self._scheduler
        if scheduler is None:
            return
        if self._next_run_at is None:
            if scheduler.get_job(JOB_ID) is not None:
                scheduler.remove_job(JOB_ID)
                logger.info("Scheduled refresh disabled")
            return
        scheduler.add_job(
            self.tick,
            trigger=DateTrigger(run_date=self._next_run_at),
            id=JOB_ID,
            name="Scheduled refresh",
            replace_existing=True,
            misfire_grace_time=None,  # run late rather than never (e.g. after a suspend)
            coalesce=True,
            max_instances=1,
        )
        if self._next_run_at != previous:
            logger.info("Next scheduled refresh at %s", self._next_run_at.isoformat())

    async def _on_config_change(self, _: RuntimeConfig) -> None:
        self._arm()


# --- Runners --------------------------------------------------------------------------
def validation_summary(exc: ValidationError) -> str:
    """Field errors without echoing input values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in err['loc']) or 'params'}: {err['msg']}"
        for err in exc.errors(include_url=False, include_input=False, include_context=False)
    )


def immich_rule(request: ImmichDisplayRequest) -> SourceRule:
    """The replayable rule of an Immich display request (only the fields it set)."""
    params = request.model_dump(mode="json", exclude_unset=True, exclude=set(NON_REPLAYED_FIELDS))
    return SourceRule(kind="immich", params=params)


def immich_runner(provider: ImmichProvider, display: DisplayService) -> RuleRunner:
    """Replay ``immich`` rules: pick a new photo with the same filters and push it."""

    async def run(rule: SourceRule) -> DisplayResult:
        params = {
            key: value for key, value in rule.params.items() if key not in NON_REPLAYED_FIELDS
        }
        try:
            request = ImmichDisplayRequest.model_validate(params)
        except ValidationError as exc:
            raise InvalidRuleError(f"Invalid immich rule: {validation_summary(exc)}") from None
        asset = await provider.fetch(request)
        return await display.display_asset(asset, fit=request.fit, push=True)

    return run
