"""Simple delay step -- runs on the control plane, not on a compute node.

Useful for inserting pauses between steps (e.g., waiting for an external
service to converge before polling).
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
    """Parameters for the sleep step."""

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
    """Pause execution for a specified duration."""

    PARAMS_SCHEMA = SleepParams
    OUTPUT_KEYS: list[str] = []
    DESCRIPTION = "Pause job execution for a specified number of seconds."
    REQUIRES_NODE = False

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = SleepParams(**resolved)
        wake_at = time.time() + validated.seconds
        return {
            "wake_at": wake_at,
            "seconds": validated.seconds,
            "cancelled": False,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if state.get("cancelled"):
            return StepResult.FAILED
        if time.time() >= state["wake_at"]:
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        state["cancelled"] = True
