"""Simple delay step -- runs on the control plane, not on a compute node.

Useful for inserting pauses between steps (e.g., waiting for an external
service to converge before polling).

Where this fits
---------------
Registered as ``"sleep"`` with ``REQUIRES_NODE = False``, so the server's
:class:`~nexus_server.runner.runner.JobRunner` executes it locally through
``_execute_local_step`` — no agent, no node reservation, no WebSocket round
trip. The runner calls ``startup()`` once, persists the returned state for
crash recovery, then loops on ``check()`` with ``await asyncio.sleep(1)``
between polls.

AI Note: the delay is implemented as a *deadline* (``wake_at``) plus a
non-blocking ``check()``, never as ``time.sleep()``. Blocking here would stall
the server's event loop and freeze every other job. Any change to this file
must preserve that property.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class SleepParams(BaseModel):
    """Parameters for the sleep step.

    The ``description``/``examples`` text is user-facing — ``to_schema()``
    publishes it to ``/api/steps`` for the frontend step palette.
    """

    seconds: float = Field(
        ...,
        description="Duration to sleep in seconds.",
        ge=0,
        le=86400,
        examples=[5, 30, 300],
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("sleep")
class SleepStep(FlowStep):
    """Pause execution for a specified duration.

    Effective resolution is roughly one second regardless of ``seconds``,
    because the runner only re-evaluates ``check()`` once per second; a
    sub-second sleep will still cost about a full poll interval.
    """

    PARAMS_SCHEMA = SleepParams
    OUTPUT_KEYS: list[str] = []
    DESCRIPTION = "Pause job execution for a specified number of seconds."
    # Control-plane step: runs in the server process, never dispatched to a node.
    REQUIRES_NODE = False

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Compute the wake-up deadline; performs no waiting itself.

        Args:
            params: Raw step params.
            ctx: Job context; ``ctx.resolve()`` lets an upstream step's output
                named ``seconds`` supply the duration when it is omitted here.

        Returns:
            State dict with the absolute ``wake_at`` deadline, the original
            ``seconds`` (kept for display/debugging) and a ``cancelled`` flag.

        Raises:
            pydantic.ValidationError: if ``seconds`` is missing or outside
                the 0..86400 bound.

        AI Note: ``wake_at`` is an absolute ``time.time()`` (wall-clock, not
        monotonic) value. Two consequences: it survives being serialised into
        the DB for crash recovery — a job resumed after a restart still wakes
        at the right instant instead of restarting the delay — but a backwards
        system clock adjustment will extend the sleep, and a forward jump will
        cut it short. That trade is deliberate; monotonic time would be
        meaningless across a process restart.
        """
        resolved = ctx.resolve(params)
        validated = SleepParams(**resolved)
        wake_at = time.time() + validated.seconds
        return {
            "wake_at": wake_at,
            "seconds": validated.seconds,
            "cancelled": False,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        """Report whether the deadline has passed. Idempotent and side-effect free.

        Args:
            state: The ``startup()`` state dict (or the copy restored from the
                DB after a crash).

        Returns:
            ``FAILED`` if ``cancel()`` flipped the ``cancelled`` flag,
            ``SUCCESS`` once the deadline has been reached, else ``RUNNING``.

        AI Note: a cancelled sleep is reported as FAILED, not SUCCESS. That is
        what stops the job from continuing to the next step after the user
        cancels it (subject to the step's ``on_fail`` setting).
        """
        if state.get("cancelled"):
            return StepResult.FAILED
        if time.time() >= state["wake_at"]:
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """Mark the sleep cancelled so the next ``check()`` returns FAILED.

        Args:
            state: The ``startup()`` state dict — mutated in place.

        AI Note: this only sets a flag; the cancellation takes effect on the
        runner's next poll (up to ~1 s later), and only if the runner is still
        holding the same in-memory ``state`` object that was passed here. There
        is nothing to kill, so no process signalling is involved.
        """
        state["cancelled"] = True
