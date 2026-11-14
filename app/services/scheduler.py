"""Autonomous refresh: replay the active source rule on a timer, outside quiet hours.

The **active rule** is the pinned ``scheduler.source_rule`` from the configuration, or
else the last manual display request (kept in ``/data/scheduler_state.json`` so that
showing a photo does not rewrite the user's settings).

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
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from app.core.config_models import RuntimeConfig, SchedulerConfig, SourceRule
from app.core.config_store import ConfigStore
from app.core.json_store import atomic_write_json, read_json
from app.core.security import InvalidImageError
from app.providers.base import ProviderError
from app.providers.immich import ImmichDisplayRequest, ImmichProvider
from app.services.display import DisplayResult, DisplayService
from app.services.schedule import QuietWindow, in_quiet, next_run_at

logger = logging.getLogger(__name__)

JOB_ID: Final = "refresh"
DEFAULT_MIN_DELAY_S: Final = 30.0
_MAX_BACKOFF_EXPONENT: Final = 16

RefreshReason = Literal["scheduled", "manual"]
RunStatus = Literal["ok", "failed", "skipped"]
RuleSource = Literal["pinned", "last"]
Clock = Callable[[], datetime]
RuleRunner = Callable[[SourceRule], Awaitable[DisplayResult]]

#: Request fields that describe one action rather than the selection; never replayed.
NON_REPLAYED_FIELDS: Final = frozenset({"push_to_fraimic", "ignore_history"})


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


@dataclass(frozen=True, slots=True)
class ActiveRule:
    """The rule a refresh replays, and where it comes from."""

    rule: SourceRule
    source: RuleSource


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """Result of a successful refresh."""

    reason: RefreshReason
    active: ActiveRule
    display: DisplayResult


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
        """
        self._path = state_path
        self._config_store = config_store
        self._runners = dict(runners)
        self._has_pending = has_pending
        self._clock = clock
        self._min_delay = timedelta(seconds=min_delay_s)
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

    def active_rule(self) -> ActiveRule | None:
        """The pinned rule, else the last manual one, else ``None``."""
        pinned = self._config.scheduler.source_rule
        if pinned is not None:
            return ActiveRule(pinned, "pinned")
        if self._state.last_rule is not None:
            return ActiveRule(self._state.last_rule, "last")
        return None

    def in_quiet_hours(self) -> bool:
        """Whether the current time falls inside the configured quiet window."""
        config = self._config.scheduler
        return in_quiet(self._clock(), quiet_window(config), ZoneInfo(config.timezone))

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
                runner = self._runner(active.rule)
                try:
                    display = await runner(active.rule)
                except Exception as exc:
                    await self._save(
                        last_run=self._last_run(reason, "failed", active, _safe_detail(exc))
                    )
                    raise
                await self._record_success(reason, active)
                return RefreshOutcome(reason=reason, active=active, display=display)
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

    async def record_manual(self, rule: SourceRule | None) -> None:
        """Note a successful manual display: it resets the timer.

        Args:
            rule: The request to replay from now on, or ``None`` for sources that cannot
                be replayed (e.g. an uploaded file), which keep the previous rule.
        """
        update: dict[str, Any] = self._timer_reset(self._clock())
        if rule is not None:
            update["last_rule"] = rule
        await self._save(**update)
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
        active = self.active_rule()
        if active is None:
            return await self._skip(
                "No rule to replay yet: display a photo or pin scheduler.source_rule", None
            )
        if config.skip_if_pending and self._has_pending():
            return await self._skip("The previous image is still waiting for the frame", active)
        try:
            runner = self._runner(active.rule)
        except UnsupportedRuleError as exc:
            return await self._skip(str(exc), active)

        try:
            await runner(active.rule)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not isinstance(exc, ProviderError | RefreshError | InvalidImageError):
                logger.exception("Scheduled refresh failed")
            return await self._record_failure(active, exc)
        return await self._record_success("scheduled", active)

    def _last_run(
        self,
        reason: RefreshReason,
        status: RunStatus,
        active: ActiveRule | None,
        detail: str | None = None,
    ) -> LastRun:
        return LastRun(
            at=self._clock(),
            reason=reason,
            status=status,
            rule_kind=active.rule.kind if active else None,
            detail=detail,
        )

    @staticmethod
    def _timer_reset(now: datetime) -> dict[str, Any]:
        return {
            "last_display_at": now,
            "timer_reset_at": now,
            "retry_at": None,
            "consecutive_failures": 0,
        }

    async def _record_success(self, reason: RefreshReason, active: ActiveRule) -> LastRun:
        last_run = self._last_run(reason, "ok", active)
        await self._save(last_run=last_run, **self._timer_reset(last_run.at))
        logger.info("Refresh done (%s, %s rule %s)", reason, active.source, active.rule.kind)
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
def _validation_summary(exc: ValidationError) -> str:
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
            raise InvalidRuleError(f"Invalid immich rule: {_validation_summary(exc)}") from None
        asset = await provider.fetch(request)
        return await display.display_asset(asset, fit=request.fit, push=True)

    return run
