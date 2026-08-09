"""Unit tests for control-plane flow steps: sleep and jump.

These steps run on the control plane (REQUIRES_NODE = False) and implement
the FlowStep lifecycle directly (startup -> check -> cancel) without touching
a compute node. We exercise PARAMS_SCHEMA validation, the startup/check/cancel
lifecycle, and jump's targeting/condition semantics.
"""

from __future__ import annotations

import json
import time

import pytest

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import StepContext
from nexus_common.steps.registry import get_step
from nexus_steps.flow.jump import JumpParams, JumpStep
from nexus_steps.flow.sleep import SleepParams, SleepStep


# ── Sleep: class metadata & registration ─────────────────────────────────


def test_sleep_is_control_plane_step():
    """Sleep runs on the control plane (no node) and publishes no context outputs.

    REQUIRES_NODE=False keeps the scheduler from occupying an agent for the whole
    sleep duration.
    """
    assert SleepStep.REQUIRES_NODE is False
    assert SleepStep.PARAMS_SCHEMA is SleepParams
    # Sleep produces no context outputs.
    assert SleepStep.OUTPUT_KEYS == []


def test_sleep_registered_under_name():
    """SleepStep is registered as 'sleep' — the name job authors write in .nexus files."""
    assert get_step("sleep") is SleepStep


# ── Sleep: PARAMS_SCHEMA validation ──────────────────────────────────────


def test_sleep_params_accepts_valid_duration():
    """SleepParams accepts a plain numeric duration."""
    params = SleepParams(seconds=5)
    assert params.seconds == 5


def test_sleep_validate_params_ok():
    """A fractional duration passes all validation passes."""
    errors = SleepStep.validate_params({"seconds": 1.5})
    assert errors == []


def test_sleep_validate_params_missing_required():
    # 'seconds' is required (RequiredRule fires before pydantic).
    """'seconds' is required; Pass 2 (RequiredRule) fires before Pydantic."""
    errors = SleepStep.validate_params({})
    assert errors
    assert any(e.field == "seconds" for e in errors)


def test_sleep_validate_params_negative_rejected():
    # ge=0 bound is enforced by pydantic in pass 3.
    """A negative duration is rejected by the ge=0 bound (Pass 3 -> _schema)."""
    errors = SleepStep.validate_params({"seconds": -1})
    assert errors
    assert any(e.field == "_schema" for e in errors)


def test_sleep_validate_params_above_max_rejected():
    # le=86400 upper bound.
    """Durations above the le=86400 (24h) cap are rejected.

    Bounds the worst case: an unbounded sleep would pin a job slot indefinitely.
    """
    errors = SleepStep.validate_params({"seconds": 86401})
    assert errors
    assert any(e.field == "_schema" for e in errors)


def test_sleep_validate_params_unknown_param_rejected():
    """An unknown param is rejected (Pass 1), catching typos at submit time."""
    errors = SleepStep.validate_params({"seconds": 1, "bogus": True})
    assert errors
    assert any(e.field == "bogus" for e in errors)


# ── Sleep: lifecycle ─────────────────────────────────────────────────────


def test_sleep_startup_returns_serializable_state(step_context):
    """startup() records wake_at = now + seconds plus the cancelled flag.

    wake_at is an absolute wall-clock deadline, which is what allows the runner to
    resume a persisted sleep after a restart.
    """
    before = time.time()
    state = SleepStep().startup({"seconds": 10}, step_context)
    assert state["seconds"] == 10
    assert state["cancelled"] is False
    # wake_at is now + duration.
    assert state["wake_at"] >= before + 10


def test_sleep_check_running_before_wake(step_context):
    """check() reports RUNNING while the deadline is in the future."""
    state = SleepStep().startup({"seconds": 60}, step_context)
    assert SleepStep().check(state) == StepResult.RUNNING


def test_sleep_reaches_success_after_duration(step_context):
    """check() flips from RUNNING to SUCCESS once the wall-clock deadline passes.

    Uses a 50ms sleep so the transition is observed for real rather than mocked.
    """
    step = SleepStep()
    state = step.startup({"seconds": 0.05}, step_context)
    # Initially running.
    assert step.check(state) == StepResult.RUNNING
    time.sleep(0.1)
    # After the tiny duration elapses, it succeeds.
    assert step.check(state) == StepResult.SUCCESS


def test_sleep_zero_duration_succeeds_immediately(step_context):
    """seconds=0 succeeds on the very first poll (wake_at is already reached).

    Boundary case: a strict '>' comparison would leave a zero sleep RUNNING forever.
    """
    step = SleepStep()
    state = step.startup({"seconds": 0}, step_context)
    # wake_at == now, so check should already be SUCCESS.
    assert step.check(state) == StepResult.SUCCESS


def test_sleep_cancel_then_check_is_failed(step_context):
    """cancel() sets the flag and makes the next check() report FAILED.

    Cancellation must be visible through the persisted state, since check() may run
    in a different process than cancel().
    """
    step = SleepStep()
    state = step.startup({"seconds": 60}, step_context)
    step.cancel(state)
    assert state["cancelled"] is True
    assert step.check(state) == StepResult.FAILED


def test_sleep_check_idempotent_after_success(step_context):
    """Repeated polling after success keeps returning SUCCESS.

    The runner may poll again after recording completion; the result must be stable.
    """
    step = SleepStep()
    state = step.startup({"seconds": 0}, step_context)
    assert step.check(state) == StepResult.SUCCESS
    # Repeated polling is safe and stable.
    assert step.check(state) == StepResult.SUCCESS


def test_sleep_startup_resolves_context_outputs():
    # 'seconds' supplied via upstream context output, not explicit params.
    """'seconds' can come from an upstream context output instead of an explicit param."""
    ctx = StepContext(outputs={"seconds": 0})
    step = SleepStep()
    state = step.startup({}, ctx)
    assert state["seconds"] == 0
    assert step.check(state) == StepResult.SUCCESS


def test_sleep_explicit_param_overrides_context_output():
    # resolve() lets explicit (non-None) params win over context outputs.
    """An explicit 'seconds' beats an upstream value of the same name."""
    ctx = StepContext(outputs={"seconds": 999})
    state = SleepStep().startup({"seconds": 0}, ctx)
    assert state["seconds"] == 0


def test_sleep_none_param_falls_back_to_context_output():
    # resolve() drops None-valued params, so the context value is used.
    """An explicit None does not clobber the upstream value (resolve() drops Nones)."""
    ctx = StepContext(outputs={"seconds": 0})
    state = SleepStep().startup({"seconds": None}, ctx)
    assert state["seconds"] == 0
    assert step_state_is_json_serializable(state)


def test_sleep_startup_state_is_json_serializable(step_context):
    # The runner persists state to the DB for crash recovery; it must be JSON-safe.
    """The state dict persists to the DB as JSON and holds exactly the three lifecycle keys.

    A non-serializable value would break crash recovery; an unexpected extra key
    would mean the lifecycle reads state check() doesn't know about.
    """
    state = SleepStep().startup({"seconds": 1}, step_context)
    assert step_state_is_json_serializable(state)
    # Round-trips through the same keys the lifecycle reads back.
    restored = json.loads(json.dumps(state))
    assert set(restored) == {"wake_at", "seconds", "cancelled"}


def test_sleep_cancel_overrides_elapsed_wake(step_context):
    # cancelled takes precedence over wake_at: a cancelled-but-elapsed sleep FAILS.
    """The cancelled flag takes precedence over an already-elapsed deadline.

    Ordering invariant: a sleep cancelled after its deadline must report FAILED,
    not retroactively SUCCESS.
    """
    step = SleepStep()
    state = step.startup({"seconds": 0}, step_context)
    assert step.check(state) == StepResult.SUCCESS  # would succeed on its own
    step.cancel(state)
    assert step.check(state) == StepResult.FAILED


def step_state_is_json_serializable(state) -> bool:
    """Helper: the agent persists state to the DB, so it must survive json round-trip."""
    json.dumps(state)
    return True


# ── Jump: class metadata & registration ──────────────────────────────────


def test_jump_is_control_plane_step():
    """Jump runs on the control plane and publishes no outputs."""
    assert JumpStep.REQUIRES_NODE is False
    assert JumpStep.PARAMS_SCHEMA is JumpParams
    assert JumpStep.OUTPUT_KEYS == []


def test_jump_registered_under_name():
    """JumpStep is registered as 'jump'."""
    assert get_step("jump") is JumpStep


# ── Jump: PARAMS_SCHEMA validation ───────────────────────────────────────


def test_jump_params_defaults():
    """Defaults: fire unconditionally ('always') with a 10-iteration safety cap.

    max_jumps is the loop-runaway guard — see the max-jumps tests.
    """
    params = JumpParams(target_step=3)
    assert params.target_step == 3
    assert params.on == "always"
    assert params.max_jumps == 10


def test_jump_validate_params_ok():
    """target_step=0 (jump to the first step) is valid."""
    errors = JumpStep.validate_params({"target_step": 0})
    assert errors == []


def test_jump_validate_params_missing_target():
    """target_step is required — a jump with no destination is rejected at submit time."""
    errors = JumpStep.validate_params({})
    assert errors
    assert any(e.field == "target_step" for e in errors)


def test_jump_validate_params_bad_on_literal():
    """'on' is a Literal; an unrecognized condition is rejected by Pydantic.

    Without the Literal, a typo like 'faill' would silently never fire.
    """
    errors = JumpStep.validate_params({"target_step": 0, "on": "maybe"})
    assert errors
    assert any(e.field == "_schema" for e in errors)


def test_jump_validate_params_max_jumps_bounds():
    # ge=1 lower bound.
    """max_jumps must be >= 1; zero would mean a jump that can never fire."""
    errors = JumpStep.validate_params({"target_step": 0, "max_jumps": 0})
    assert errors
    assert any(e.field == "_schema" for e in errors)


def test_jump_validate_semantic_negative_target():
    # validate_semantic guards target_step < 0 with a dedicated message.
    """validate_semantic gives a negative target_step its own readable message.

    More actionable than the raw Pydantic ge=0 error for the same input.
    """
    errors = JumpStep.validate_semantic({"target_step": -1}, StepContext())
    assert errors
    assert errors[0].field == "target_step"
    assert "must be >= 0" in errors[0].issue


def test_jump_validate_semantic_ok_for_valid_target():
    """A non-negative target passes the semantic hook."""
    assert JumpStep.validate_semantic({"target_step": 2}, StepContext()) == []


# ── Jump: lifecycle & targeting semantics ────────────────────────────────


def test_jump_always_sets_jump_target(step_context):
    """on='always' publishes __jump_target so the runner redirects execution.

    __jump_target is the sole channel by which a step alters control flow; the
    double-underscore marks it as a runner-internal key rather than a user output.
    """
    step = JumpStep()
    state = step.startup({"target_step": 2, "on": "always"}, step_context)
    assert state["__jump_target"] == 2
    assert state["jumped"] is True
    assert state["on"] == "always"
    assert step.check(state) == StepResult.SUCCESS


def test_jump_check_success_when_no_error(step_context):
    """check() reports SUCCESS whenever startup() recorded no error."""
    step = JumpStep()
    state = step.startup({"target_step": 0}, step_context)
    assert "error" not in state
    assert step.check(state) == StepResult.SUCCESS


def test_jump_on_fail_fires_only_when_last_failed():
    """on='fail' fires when the upstream context reports _last_failed=True.

    This is the DSL's retry primitive: jump backwards only when the prior step failed.
    """
    step = JumpStep()
    # Upstream recorded a failure -> jump fires.
    ctx_failed = StepContext(outputs={"_last_failed": True})
    state = step.startup({"target_step": 1, "on": "fail"}, ctx_failed)
    assert state["jumped"] is True
    assert state["__jump_target"] == 1
    assert state["last_failed"] is True


def test_jump_on_fail_skips_when_not_failed():
    """on='fail' with a successful upstream does NOT set __jump_target and still reports SUCCESS.

    A non-firing conditional jump is a normal outcome, not a failure — reporting
    FAILED here would abort jobs that simply didn't need to retry.
    """
    step = JumpStep()
    ctx_ok = StepContext(outputs={"_last_failed": False})
    state = step.startup({"target_step": 1, "on": "fail"}, ctx_ok)
    # Condition not met: no jump target, advances to next step.
    assert state["jumped"] is False
    assert "__jump_target" not in state
    assert state["on"] == "fail"
    # Still a successful (non-firing) outcome.
    assert step.check(state) == StepResult.SUCCESS


def test_jump_on_success_fires_when_not_failed():
    """on='success' fires when the upstream step did not fail."""
    step = JumpStep()
    ctx_ok = StepContext(outputs={"_last_failed": False})
    state = step.startup({"target_step": 4, "on": "success"}, ctx_ok)
    assert state["jumped"] is True
    assert state["__jump_target"] == 4
    assert state["last_failed"] is False


def test_jump_on_success_skips_when_failed():
    """on='success' does not fire after an upstream failure."""
    step = JumpStep()
    ctx_failed = StepContext(outputs={"_last_failed": True})
    state = step.startup({"target_step": 4, "on": "success"}, ctx_failed)
    assert state["jumped"] is False
    assert "__jump_target" not in state


def test_jump_exposes_counter_metadata_on_fire(step_context):
    """A firing jump reports jump_count, max_jumps and the counter key it used.

    The counter key is what the runner threads back into context so the next visit
    sees an incremented count; exposing it makes loop behavior debuggable from the
    job log.
    """
    step = JumpStep()
    state = step.startup({"target_step": 0, "max_jumps": 5}, step_context)
    # On a fresh visit (no prior counter), the next count is 1.
    assert state["jump_count"] == 1
    assert state["max_jumps"] == 5
    assert state["jump_counter_key"].startswith("__jump_count_")


def test_jump_max_jumps_exceeded_fails():
    """Reaching max_jumps blocks the jump and FAILS the step instead of looping forever.

    The runaway-loop guard: without it a backward jump would re-execute until the
    job timeout, burning a node the whole time.
    """
    step = JumpStep()
    # Pre-seed the persistent counter at the limit so the next fire is blocked.
    # AI Note: the key is derived from target_step+on, NOT from id(step). It must
    # be stable across instances because the runner builds a fresh step object on
    # every visit — an identity-keyed counter reset each iteration and max_jumps
    # was unreachable, hanging the job forever.
    counter_key = "__jump_count_0_always"
    ctx = StepContext(outputs={counter_key: 3})
    state = step.startup({"target_step": 0, "max_jumps": 3}, ctx)
    assert "error" in state
    assert "Max jumps" in state["error"]
    assert "__jump_target" not in state
    assert step.check(state) == StepResult.FAILED


def test_jump_under_limit_still_fires():
    """A counter below the limit still fires and reports the incremented count.

    Boundary check on the other side of test_jump_max_jumps_exceeded_fails.
    """
    step = JumpStep()
    counter_key = "__jump_count_0_always"
    ctx = StepContext(outputs={counter_key: 2})
    state = step.startup({"target_step": 0, "max_jumps": 3}, ctx)
    # 2 < 3 -> still allowed; reported count becomes 3.
    assert state["jumped"] is True
    assert state["jump_count"] == 3
    assert step.check(state) == StepResult.SUCCESS


def test_jump_cancel_is_noop(step_context):
    """cancel() leaves state untouched — a jump is instantaneous, with nothing to stop."""
    step = JumpStep()
    state = step.startup({"target_step": 0}, step_context)
    # cancel does nothing for an instantaneous jump; state is unchanged.
    snapshot = dict(state)
    assert step.cancel(state) is None
    assert state == snapshot


# ── Jump: validation pipeline & serialization edge cases ──────────────────


def test_jump_validate_params_negative_target_reports_both_passes():
    # target_step=-1 trips pydantic ge=0 (Pass 3 -> _schema) AND validate_semantic
    # (Pass 4 -> target_step). The full pipeline surfaces both.
    """target_step=-1 surfaces BOTH the Pydantic and the semantic error.

    Concrete evidence that Pass 4 is not gated on Pass 3 (see test_base's
    test_validate_params_semantic_appends_alongside_type_error).
    """
    errors = JumpStep.validate_params({"target_step": -1})
    assert errors
    fields = {e.field for e in errors}
    assert "_schema" in fields
    assert "target_step" in fields


def test_jump_validate_params_above_max_jumps_rejected():
    # le=10000 upper bound on max_jumps.
    """max_jumps above the le=10000 cap is rejected."""
    errors = JumpStep.validate_params({"target_step": 0, "max_jumps": 10001})
    assert errors
    assert any(e.field == "_schema" for e in errors)


def test_jump_on_fail_skips_when_last_failed_key_absent():
    # Empty context => _last_failed defaults to False, so 'fail' does not fire.
    """With no _last_failed key at all, 'fail' treats the upstream as successful.

    Matters for the first step in a job, where no upstream result exists yet — the
    absent key must default to False rather than raising or firing.
    """
    step = JumpStep()
    state = step.startup({"target_step": 1, "on": "fail"}, StepContext())
    assert state["jumped"] is False
    assert "__jump_target" not in state
    assert step.check(state) == StepResult.SUCCESS


def test_jump_on_success_fires_when_last_failed_key_absent():
    # Empty context => not failed => 'success' fires.
    """The mirror case: an absent _last_failed means 'success' fires."""
    step = JumpStep()
    state = step.startup({"target_step": 7, "on": "success"}, StepContext())
    assert state["jumped"] is True
    assert state["__jump_target"] == 7


def test_jump_startup_state_is_json_serializable_on_fire(step_context):
    # Firing state carries the jump target + counter metadata; must be DB-persistable.
    """The firing state (jump target + counter metadata) is DB-persistable."""
    state = JumpStep().startup({"target_step": 2}, step_context)
    assert step_state_is_json_serializable(state)


def test_jump_startup_state_is_json_serializable_on_skip():
    """The non-firing state is DB-persistable too."""
    state = JumpStep().startup(
        {"target_step": 2, "on": "fail"}, StepContext(outputs={"_last_failed": False}),
    )
    assert step_state_is_json_serializable(state)


def test_jump_counter_key_is_stable_across_instances():
    """The jump counter key is derived from params, so it is STABLE per instance.

    This is the regression test for the infinite-loop bug: the key used to be
    ``f"__jump_count_{id(self)}"``, but the runner constructs a fresh step object
    on every visit, so the key changed each iteration, the counter always read
    back 0, ``max_jumps`` was never reachable, and a backward ``on="always"``
    jump looped until the job was cancelled. Two separate instances configured
    the same way must now agree on the key, which is what lets the count survive
    across visits.
    """
    a, b = JumpStep(), JumpStep()
    state_a = a.startup({"target_step": 0}, step_context_with())
    state_b = b.startup({"target_step": 0}, step_context_with())
    assert state_a["jump_counter_key"] == state_b["jump_counter_key"]
    assert state_a["jump_counter_key"] == "__jump_count_0_always"


def test_jump_counter_key_distinguishes_target_and_condition():
    """Jumps with different targets or conditions get separate counters.

    Stability must not collapse every jump in a job onto one shared budget:
    distinct loops need independent guards, so the key includes both
    ``target_step`` and ``on``.
    """
    s = JumpStep()
    to_0 = s.startup({"target_step": 0}, step_context_with())["jump_counter_key"]
    to_5 = s.startup({"target_step": 5}, step_context_with())["jump_counter_key"]
    on_success = s.startup(
        {"target_step": 0, "on": "success"}, step_context_with(),
    )["jump_counter_key"]
    assert to_0 != to_5
    assert to_0 != on_success


def test_jump_seeded_counter_blocks_a_new_instance():
    """A counter seeded in the context blocks a DIFFERENT instance's jump.

    The behavioural payoff of the stable key, and the inverse of the old buggy
    contract: the loop guard now survives the runner rebuilding the step object,
    so a job cannot evade ``max_jumps`` simply because each visit gets a fresh
    instance.
    """
    a, b = JumpStep(), JumpStep()
    # Seed via instance `a`'s key; `b` must see and honour the same counter.
    seeded_key = a.startup({"target_step": 0}, step_context_with())["jump_counter_key"]
    ctx = StepContext(outputs={seeded_key: 99})
    state_b = b.startup({"target_step": 0, "max_jumps": 1}, ctx)
    assert "error" in state_b
    assert "Max jumps" in state_b["error"]
    assert "__jump_target" not in state_b
    assert b.check(state_b) == StepResult.FAILED


def step_context_with():
    """Helper producing a fresh empty StepContext (avoids fixture sharing)."""
    return StepContext()


# ── Schema export (public API/UI surface) ─────────────────────────────────


def test_sleep_to_schema_is_serializable_control_plane():
    """Sleep's schema export is JSON-safe and advertises exactly the 'seconds' field.

    This payload drives the job-builder form, so field set and required-ness are a
    user-facing contract.
    """
    schema = SleepStep.to_schema()
    assert json.dumps(schema)  # API serializes this to the UI
    assert schema["requires_node"] is False
    assert schema["output_keys"] == []
    field_names = {f["name"] for f in schema["fields"]}
    assert field_names == {"seconds"}
    seconds = next(f for f in schema["fields"] if f["name"] == "seconds")
    assert seconds["required"] is True


def test_jump_to_schema_exposes_fields_and_defaults():
    """Jump's schema exports its three fields with 'on' optional and defaulting to 'always'.

    The serialized default is what the UI pre-fills in the form.
    """
    schema = JumpStep.to_schema()
    assert json.dumps(schema)
    assert schema["requires_node"] is False
    field_names = {f["name"] for f in schema["fields"]}
    assert field_names == {"target_step", "on", "max_jumps"}
    on_field = next(f for f in schema["fields"] if f["name"] == "on")
    # 'on' is optional with a serialized default of 'always'.
    assert on_field["required"] is False
    assert on_field["default"] == "always"
    target = next(f for f in schema["fields"] if f["name"] == "target_step")
    assert target["required"] is True
