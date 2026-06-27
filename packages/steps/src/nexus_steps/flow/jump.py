"""Jump to another step index -- runs on the control plane.

Used for simple looping or conditional branching within a job.  The
executor honours the ``__jump_target`` key in the step state to redirect
the step pointer after this step succeeds.

The ``on`` parameter selects when the jump fires:

- ``always`` (default) — jumps every visit; useful for plain loops.
- ``fail`` — jumps only when the previous step recorded a failure
  (``_last_failed`` truthy in the job context, set by the runner when an
  upstream step has ``on_fail="continue"``).
- ``success`` — jumps only when the previous step did NOT fail.

A ``max_jumps`` safety limit prevents infinite loops.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FieldError, FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class JumpParams(BaseModel):
    """Parameters for the jump step."""

    target_step: int = Field(
        ...,
        description="Zero-based index of the step to jump to.",
        ge=0,
        examples=[0, 3],
    )
    on: Literal["always", "fail", "success"] = Field(
        "always",
        description=(
            "Condition that triggers the jump. 'fail' fires only when the "
            "previous step recorded _last_failed; 'success' fires only when "
            "it did not. Default 'always' jumps every visit (plain loop)."
        ),
    )
    max_jumps: int = Field(
        10,
        description="Maximum number of times this jump may fire before failing.",
        ge=1,
        le=10000,
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("jump")
class JumpStep(FlowStep):
    """Jump to another step index within the same job."""

    PARAMS_SCHEMA = JumpParams
    OUTPUT_KEYS: list[str] = []
    DESCRIPTION = "Jump to another step index (simple loop / branch control)."
    REQUIRES_NODE = False

    # ── Validation ──

    @classmethod
    def validate_semantic(
        cls, params: dict, context: StepContext,
    ) -> list[FieldError]:
        errors: list[FieldError] = []
        target = params.get("target_step")
        if target is not None and target < 0:
            errors.append(FieldError("target_step", "must be >= 0"))
        return errors

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = JumpParams(**resolved)

        last_failed = bool(ctx.outputs.get("_last_failed", False))
        should_jump = (
            validated.on == "always"
            or (validated.on == "fail" and last_failed)
            or (validated.on == "success" and not last_failed)
        )

        if not should_jump:
            # Condition not met — succeed without setting __jump_target so the
            # runner advances to the next step.
            return {"jumped": False, "on": validated.on, "last_failed": last_failed}

        # Read persistent jump counter from context outputs (survives across
        # repeated visits to this step within a single job).
        jump_counter_key = f"__jump_count_{id(self)}"
        jump_count = ctx.outputs.get(jump_counter_key, 0)

        if jump_count >= validated.max_jumps:
            return {
                "error": (
                    f"Max jumps ({validated.max_jumps}) exceeded for "
                    f"target_step={validated.target_step}"
                ),
            }

        return {
            "__jump_target": validated.target_step,
            "jumped": True,
            "on": validated.on,
            "last_failed": last_failed,
            "jump_counter_key": jump_counter_key,
            "jump_count": jump_count + 1,
            "max_jumps": validated.max_jumps,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED
        return StepResult.SUCCESS

    def cancel(self, state: dict[str, Any]) -> None:
        # Nothing to cancel -- the jump is instantaneous.
        pass
