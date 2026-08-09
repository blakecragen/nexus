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

Where this fits
---------------
Registered as ``"jump"`` with ``REQUIRES_NODE = False``, so
:class:`~nexus_server.runner.runner.JobRunner` runs it locally via
``_execute_local_step``. The coupling with the runner is tight and implicit —
three magic keys form the contract:

* ``__jump_target`` (this step → runner): read out of the step state by
  ``_execute_local_step``, returned as ``ret["jump_target"]``, and used by the
  main loop to assign ``idx = jump_target`` instead of ``idx += 1``.
* ``_last_failed`` (runner → this step): set to ``True`` in
  ``context.outputs`` when a step fails under ``on_fail="continue"``, and
  popped again after any successful step so a stale failure cannot re-trigger
  a later ``on="fail"`` jump.
* ``error`` (this step → itself): presence makes ``check()`` return FAILED.

Renaming any of those strings breaks control flow silently — the runner just
falls through to the next step.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FieldError, FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class JumpParams(BaseModel):
    """Parameters for the jump step.

    The ``description``/``examples`` text is user-facing — ``to_schema()``
    publishes it to ``/api/steps`` for the frontend step palette.
    """

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
    """Jump to another step index within the same job.

    There is no bound on the target: jumping forward skips steps, jumping
    backward loops. The runner does not validate that ``target_step`` is
    within the job's step list — an out-of-range forward target simply ends
    the loop and the job completes.
    """

    PARAMS_SCHEMA = JumpParams
    # AI Note: intentionally empty. Because the runner harvests outputs as
    # ``{k: state[k] for k in OUTPUT_KEYS}``, NOTHING this step puts in its
    # state reaches ``context.outputs`` — see the jump-counter note in
    # ``startup()``.
    OUTPUT_KEYS: list[str] = []
    DESCRIPTION = "Jump to another step index (simple loop / branch control)."
    # Control-plane step: evaluated in the server process, never on a node.
    REQUIRES_NODE = False

    # ── Validation ──

    @classmethod
    def validate_semantic(
        cls, params: dict, context: StepContext,
    ) -> list[FieldError]:
        """Reject a negative ``target_step`` at job-submission time.

        Args:
            params: The raw params dict as submitted (not context-merged).
            context: Job context accumulated from prior steps' OUTPUT_KEYS.

        Returns:
            A list of :class:`~nexus_common.steps.base.FieldError`; empty when
            valid.

        AI Note: this duplicates the ``ge=0`` constraint already on the Pydantic
        field. It is kept because ``FlowStep.validate_params`` runs the schema
        pass and this semantic pass in the same call and concatenates both
        error lists, so the user gets a plain "must be >= 0" message alongside
        the noisier Pydantic one. Harmless duplication, not dead code.
        """
        errors: list[FieldError] = []
        target = params.get("target_step")
        if target is not None and target < 0:
            errors.append(FieldError("target_step", "must be >= 0"))
        return errors

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Evaluate the jump condition and emit the runner's redirect directive.

        The whole step executes here; ``check()`` is a pure read of the result.

        Args:
            params: Raw step params.
            ctx: Job context. ``ctx.outputs`` is read directly (not just via
                ``resolve``) to inspect the runner-managed ``_last_failed``
                flag.

        Returns:
            One of three shapes:

            * condition not met → ``{"jumped": False, ...}`` with no
              ``__jump_target``, so the runner advances normally;
            * jump allowed → a dict containing ``__jump_target``, which makes
              the runner set its step pointer to that index;
            * jump budget exhausted → ``{"error": ...}``, which makes
              ``check()`` return FAILED and stops the loop.

        Raises:
            pydantic.ValidationError: if ``target_step`` is missing or the
                params otherwise fail the schema.
        """
        resolved = ctx.resolve(params)
        validated = JumpParams(**resolved)

        # AI Note: `_last_failed` is only ever present when an upstream step
        # failed AND that step was configured with on_fail="continue" — a job
        # with the default on_fail="stop" terminates before reaching a jump, so
        # on="fail" is only meaningful in continue-style pipelines. The runner
        # pops the flag after every success, so it reflects the immediately
        # preceding step, not "any failure so far".
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
        #
        # AI Note: the key MUST derive from the step's params, never from
        # ``id(self)``. The runner constructs a fresh ``step_cls()`` on every
        # visit, so an identity-based key changed each iteration, the counter
        # always read back 0, ``max_jumps`` was unreachable, and a backward
        # ``on="always"`` jump looped until the job was cancelled. Keying on
        # target_step+on is stable across instances. Two distinct jump steps
        # sharing a target and condition also share this counter, which bounds
        # total iterations of that loop rather than per-site visits —
        # deliberate, and still terminates.
        #
        # The companion half of the fix lives in the runner: ``OUTPUT_KEYS`` is
        # a static list and cannot declare this dynamic key, so
        # ``_execute_local_step`` copies ``jump_counter_key``/``jump_count``
        # from state into the job context explicitly. Both halves are required.
        jump_counter_key = f"__jump_count_{validated.target_step}_{validated.on}"
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
        """Report the outcome computed by ``startup()``. Pure and idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when the max-jumps guard tripped (``error`` present),
            otherwise ``SUCCESS`` — including the "condition not met, did not
            jump" case, which is a normal, successful no-op.

        AI Note: a jump must report SUCCESS for the redirect to take effect;
        the runner only consults ``__jump_target`` on the success branch.
        """
        if "error" in state:
            return StepResult.FAILED
        return StepResult.SUCCESS

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the jump decision is made synchronously in ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.
        """
        # Nothing to cancel -- the jump is instantaneous.
        pass
