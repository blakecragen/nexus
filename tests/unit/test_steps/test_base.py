"""
Unit tests for nexus_common.steps.base.

Covers:
- StepContext.resolve (context merge, explicit-wins, None-dropping)
- FieldError.__str__ (with/without example)
- InputRule subclasses: RequiredRule, OptionalRule, ContextSatisfiableRule,
  AtLeastOneRule — both validate() and to_schema()
- FlowStep.validate_params 4-pass logic (unknown short-circuit, required missing,
  context-satisfiable via context.outputs, pydantic type errors -> _schema,
  validate_semantic hook)
- resolve_for_os (OS_VARIANTS merge, explicit wins, None dropped)
- supports_os
- input_rules() default derivation from PARAMS_SCHEMA
- to_schema() field + rule export

A throwaway FlowStep subclass is defined here (NOT registered) to drive the
classmethods without depending on any real registered step.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, Field

from nexus_common.models.enums import OSType, StepResult
from nexus_common.steps.base import (
    AtLeastOneRule,
    ContextSatisfiableRule,
    FieldError,
    FlowStep,
    InputRule,
    OptionalRule,
    RequiredRule,
    StepContext,
)


# ── Throwaway step (NOT @register'd) ─────────────────────────────────────


class _Params(BaseModel):
    """Params for the throwaway step: one required, one optional, one int."""

    command: str = Field(..., description="Shell command to run", examples=["echo hi"])
    timeout: int = Field(60, description="Timeout in seconds")
    cwd: str | None = Field(None, description="Working directory")


class _DummyStep(FlowStep):
    """Minimal concrete FlowStep used to exercise the base classmethods."""

    PARAMS_SCHEMA = _Params
    DESCRIPTION = "A dummy step"
    OUTPUT_KEYS = ["result_path"]
    REQUIRES_NODE = True
    SUPPORTED_OS = ["macos", "linux"]
    OS_VARIANTS = {
        "macos": {"command": "sw_vers", "timeout": 30},
        "linux": {"command": "uname -a"},
    }

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Return a trivial state dict; the base classmethods are what's under test."""
        return {"started": True}

    def check(self, state: dict[str, Any]) -> StepResult:
        """Always report SUCCESS — the lifecycle is not what this file exercises."""
        return StepResult.SUCCESS

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op cancel hook (nothing is spawned)."""
        return None


# ── StepContext.resolve ──────────────────────────────────────────────────


def test_resolve_merges_context_and_params():
    """resolve() unions upstream context outputs with the step's explicit params.

    This union is how steps chain: a downstream step can consume an upstream
    output without the author restating it in the .nexus file.
    """
    ctx = StepContext(outputs={"from_ctx": "ctx_value"})
    merged = ctx.resolve({"explicit": "param_value"})
    assert merged == {"from_ctx": "ctx_value", "explicit": "param_value"}


def test_resolve_explicit_param_overrides_context():
    """An explicit param wins over a same-named context output.

    The author's written value must always beat an inherited one, otherwise an
    upstream step could hijack a deliberately-set parameter.
    """
    ctx = StepContext(outputs={"command": "old"})
    merged = ctx.resolve({"command": "new"})
    assert merged["command"] == "new"


def test_resolve_drops_none_valued_params():
    """A None-valued param is dropped so it can't clobber a context value.

    Pydantic models emit None for unset optional fields; without this rule every
    unset optional would erase the corresponding upstream output.
    """
    ctx = StepContext(outputs={"command": "keep_me"})
    merged = ctx.resolve({"command": None, "timeout": 5})
    # None param does NOT clobber the context value.
    assert merged["command"] == "keep_me"
    assert merged["timeout"] == 5


def test_resolve_empty_inputs_returns_empty_dict():
    """An empty context and empty params resolve to an empty dict (no injected keys)."""
    assert StepContext().resolve({}) == {}


def test_resolve_keeps_falsy_non_none_values():
    # Only None is dropped; falsy-but-not-None values (0, "", False) survive.
    """Only None is dropped — 0, '' and False are preserved.

    A truthiness check instead of an ``is not None`` check would silently discard
    meaningful values like timeout=0 or a deliberate empty string.
    """
    merged = StepContext().resolve({"timeout": 0, "name": "", "flag": False})
    assert merged == {"timeout": 0, "name": "", "flag": False}


# ── FieldError.__str__ ───────────────────────────────────────────────────


def test_field_error_str_without_example():
    """FieldError renders as 'field: issue' when no example is attached."""
    assert str(FieldError("command", "required")) == "command: required"


def test_field_error_str_with_example():
    """An example is appended as '(e.g. ...)' — this text is shown to users in the UI."""
    fe = FieldError("command", "must be a string", example="echo hi")
    assert str(fe) == "command: must be a string (e.g. echo hi)"


# ── RequiredRule ─────────────────────────────────────────────────────────


def test_required_rule_passes_when_in_params():
    """RequiredRule is satisfied by an explicit param."""
    rule = RequiredRule("command")
    assert rule.validate({"command": "x"}, StepContext()) == []


def test_required_rule_passes_when_in_context():
    """RequiredRule is ALSO satisfied by an upstream context output.

    This is what makes chained jobs validate at submit time: step 2 need not
    restate a value step 1 will publish.
    """
    rule = RequiredRule("command")
    ctx = StepContext(outputs={"command": "from upstream"})
    assert rule.validate({}, ctx) == []


def test_required_rule_fails_when_missing():
    """A field absent from both params and context yields exactly one 'required' error."""
    rule = RequiredRule("command")
    errors = rule.validate({}, StepContext())
    assert len(errors) == 1
    assert errors[0].field == "command"
    assert errors[0].issue == "required"


def test_required_rule_to_schema():
    """to_schema() exports the exact dict shape the /steps API and UI consume.

    The UI renders required-field markers from rule_type/fields/description, so the
    key names are a public contract.
    """
    schema = RequiredRule("command", "the command").to_schema()
    assert schema == {
        "rule_type": "required",
        "fields": ["command"],
        "description": "the command",
    }


# ── OptionalRule ─────────────────────────────────────────────────────────


def test_optional_rule_always_passes():
    """OptionalRule never produces an error, present or absent.

    It exists purely to advertise the field in the schema export.
    """
    rule = OptionalRule("cwd")
    assert rule.validate({}, StepContext()) == []
    assert rule.validate({"cwd": "/tmp"}, StepContext()) == []


def test_optional_rule_to_schema():
    """OptionalRule's schema export omits 'description' when none was supplied."""
    assert OptionalRule("cwd").to_schema() == {
        "rule_type": "optional",
        "fields": ["cwd"],
    }


# ── ContextSatisfiableRule ───────────────────────────────────────────────


def test_context_satisfiable_passes_when_in_params():
    """ContextSatisfiableRule accepts an explicit param."""
    rule = ContextSatisfiableRule("binary_path", "build_output")
    assert rule.validate({"binary_path": "/x"}, StepContext()) == []


def test_context_satisfiable_passes_when_context_key_present():
    """ContextSatisfiableRule accepts a DIFFERENTLY-named upstream context key.

    This is the key difference from RequiredRule: 'binary_path' can be satisfied by
    an upstream 'build_output', letting steps compose without matching names.
    """
    rule = ContextSatisfiableRule("binary_path", "build_output")
    ctx = StepContext(outputs={"build_output": "/built/bin"})
    assert rule.validate({}, ctx) == []


def test_context_satisfiable_fails_when_neither_present():
    """With neither the param nor the named context key, the error names the expected key.

    Naming the context key in the message tells the author which upstream step to
    add.
    """
    rule = ContextSatisfiableRule("binary_path", "build_output")
    errors = rule.validate({}, StepContext())
    assert len(errors) == 1
    assert errors[0].field == "binary_path"
    assert "build_output" in errors[0].issue


def test_context_satisfiable_to_schema():
    """The schema export carries the extra 'context_key' field so the UI can hint at it."""
    schema = ContextSatisfiableRule("binary_path", "build_output", "the binary").to_schema()
    assert schema == {
        "rule_type": "context_satisfiable",
        "fields": ["binary_path"],
        "description": "the binary",
        "context_key": "build_output",
    }


# ── AtLeastOneRule ───────────────────────────────────────────────────────


def test_at_least_one_passes_when_a_param_present():
    """AtLeastOneRule is satisfied when any one of its fields is in params."""
    rule = AtLeastOneRule(["repo_url", "local_path"])
    assert rule.validate({"local_path": "/src"}, StepContext()) == []


def test_at_least_one_passes_when_present_in_context():
    """AtLeastOneRule can also be satisfied from upstream context outputs."""
    rule = AtLeastOneRule(["repo_url", "local_path"])
    ctx = StepContext(outputs={"repo_url": "git@x"})
    assert rule.validate({}, ctx) == []


def test_at_least_one_fails_when_none_present():
    """With none of the alternatives present, one error is reported against the FIRST field.

    Reporting a single error against a stable field (rather than one per
    alternative) keeps the UI from flagging every field in the group.
    """
    rule = AtLeastOneRule(["repo_url", "local_path"])
    errors = rule.validate({}, StepContext())
    assert len(errors) == 1
    # Error is reported against the first field name.
    assert errors[0].field == "repo_url"
    assert "at least one" in errors[0].issue


def test_at_least_one_to_schema():
    """AtLeastOneRule exports all its alternative field names in one rule entry."""
    schema = AtLeastOneRule(["repo_url", "local_path"], "source").to_schema()
    assert schema == {
        "rule_type": "at_least_one",
        "fields": ["repo_url", "local_path"],
        "description": "source",
    }


# ── input_rules() default derivation ─────────────────────────────────────


def test_input_rules_derives_required_and_optional():
    """By default, rules are derived from PARAMS_SCHEMA: no-default -> required, has-default -> optional.

    Steps get sensible validation for free; only steps with cross-field logic need
    to override input_rules(). Note cwd defaults to None and is still optional.
    """
    rules = _DummyStep.input_rules()
    by_field = {}
    for r in rules:
        # Each default rule wraps exactly one field.
        if isinstance(r, RequiredRule):
            by_field[r.field_name] = "required"
        elif isinstance(r, OptionalRule):
            by_field[r.field_name] = "optional"
    # command is required (no default); timeout has default 60; cwd defaults None.
    assert by_field["command"] == "required"
    assert by_field["timeout"] == "optional"
    assert by_field["cwd"] == "optional"


def test_input_rules_required_carries_description():
    """The Pydantic Field description is carried onto the derived rule.

    That description is the tooltip text in the job-builder UI.
    """
    rules = _DummyStep.input_rules()
    cmd_rule = next(r for r in rules if isinstance(r, RequiredRule))
    assert cmd_rule.description == "Shell command to run"


# ── validate_params: 4-pass logic ────────────────────────────────────────


def test_validate_params_happy_path():
    """A minimal valid payload produces no errors across all four validation passes."""
    assert _DummyStep.validate_params({"command": "echo hi"}) == []


def test_validate_params_unknown_param_short_circuits():
    # Both an unknown key AND a missing required key are present, but Pass 1
    # short-circuits: only the unknown-param error is returned.
    """Pass 1 (unknown keys) short-circuits — no other errors are reported alongside it.

    Deliberate: an unknown key usually means a typo, and reporting the resulting
    cascade of 'missing required' errors would bury the real cause.
    """
    errors = _DummyStep.validate_params({"bogus": 1})
    assert len(errors) == 1
    assert errors[0].field == "bogus"
    assert "unknown parameter" in errors[0].issue
    # The required-field error must NOT appear (short circuit).
    assert all(e.field != "command" for e in errors)


def test_validate_params_required_missing():
    """Pass 2 reports a missing required field as field='<name>', issue='required'."""
    errors = _DummyStep.validate_params({"timeout": 5})
    assert len(errors) == 1
    assert errors[0].field == "command"
    assert errors[0].issue == "required"


def test_validate_params_required_satisfied_via_context():
    """An upstream context output satisfies a required field at validation time.

    This is what lets the API validate a whole multi-step job at submit time,
    accumulating each step's OUTPUT_KEYS into the context as it walks the list.
    """
    ctx = StepContext(outputs={"command": "echo from-ctx"})
    assert _DummyStep.validate_params({}, ctx) == []


def test_validate_params_type_error_reported_as_schema():
    # timeout must be int-coercible; a non-numeric string fails Pydantic (Pass 3).
    """Pydantic type failures (Pass 3) are collapsed under the pseudo-field '_schema'.

    Pydantic errors can span multiple fields and nested locations, so they are
    reported under one synthetic field rather than being mapped back individually.
    """
    errors = _DummyStep.validate_params({"command": "echo", "timeout": "not-an-int"})
    assert len(errors) == 1
    assert errors[0].field == "_schema"


def test_validate_params_semantic_hook_runs_after_passes():
    """validate_semantic errors are appended in Pass 4 (after type validation)."""

    class _SemStep(_DummyStep):
        """Step whose semantic hook enforces a cross-field/business bound on timeout."""
        @classmethod
        def validate_semantic(cls, params, context):
            """Reject a timeout above 100 (a rule Pydantic bounds cannot express here)."""
            if params.get("timeout", 60) > 100:
                return [FieldError("timeout", "must be <= 100")]
            return []

    # Valid types but semantically invalid -> only the semantic error.
    errors = _SemStep.validate_params({"command": "echo", "timeout": 9999})
    assert len(errors) == 1
    assert errors[0].field == "timeout"
    assert errors[0].issue == "must be <= 100"

    # Within bounds -> clean.
    assert _SemStep.validate_params({"command": "echo", "timeout": 50}) == []


def test_validate_params_uses_empty_context_when_none():
    # context defaults to an empty StepContext when None. Pass 3 calls
    # ctx.resolve(params), which would AttributeError if ctx were left as None,
    # so a fully-valid payload with context=None must validate cleanly.
    """context=None is normalized to an empty StepContext, not left as None.

    Pass 3 calls ctx.resolve(params); if the default were left as None this would
    AttributeError for every caller that omits the context (which is most of them).
    """
    assert _DummyStep.validate_params({"command": "echo hi"}, context=None) == []
    # ...and required-missing still behaves normally with the default context.
    errors = _DummyStep.validate_params({}, context=None)
    assert len(errors) == 1
    assert errors[0].field == "command"
    assert errors[0].issue == "required"


def test_validate_params_rule_failure_short_circuits_before_pydantic():
    # Pass 2 (required missing) returns before Pass 3 (Pydantic). A payload that
    # is BOTH missing a required field AND has a bad type for another field must
    # surface only the rule error, never the _schema type error.
    """Pass 2 returns before Pass 3, so a rule failure suppresses type errors.

    A missing required field would also produce a confusing Pydantic error; showing
    only the actionable 'required' error is the intended behavior.
    """
    errors = _DummyStep.validate_params({"timeout": "not-an-int"})
    assert len(errors) == 1
    assert errors[0].field == "command"
    assert errors[0].issue == "required"
    assert all(e.field != "_schema" for e in errors)


def test_validate_params_type_error_message_mentions_field():
    """The Pydantic message is passed through verbatim so the offending field is still named.

    Since Pass 3 errors all report field='_schema', the field name is only
    recoverable from the message text — it must not be stripped.
    """
    errors = _DummyStep.validate_params({"command": "echo", "timeout": "not-an-int"})
    assert len(errors) == 1
    assert errors[0].field == "_schema"
    # The Pydantic error text is carried through verbatim and references the field.
    assert "timeout" in errors[0].issue


def test_validate_params_semantic_appends_alongside_type_error():
    # Pass 4 runs UNCONDITIONALLY (not gated on Pass 3 success), so a payload that
    # fails BOTH Pydantic typing AND the semantic hook produces two errors.
    """Pass 4 runs unconditionally, so semantic and type errors can both appear.

    Unlike passes 1 and 2, the semantic hook is NOT gated on Pass 3 succeeding —
    which means validate_semantic implementations must tolerate params that failed
    type validation.
    """
    class _SemStep(_DummyStep):
        """Step whose semantic hook always fails, to prove Pass 4 is not gated on Pass 3."""
        @classmethod
        def validate_semantic(cls, params, context):
            """Always return one error regardless of input."""
            return [FieldError("command", "must not be empty-ish")]

    errors = _SemStep.validate_params({"command": "echo", "timeout": "not-an-int"})
    fields = {e.field for e in errors}
    assert "_schema" in fields  # from Pydantic Pass 3
    assert "command" in fields  # from semantic Pass 4
    assert len(errors) == 2


def test_validate_params_context_satisfiable_rule_via_override():
    # Default input_rules() never yields a ContextSatisfiableRule, so drive it
    # through validate_params with an override to confirm Pass 2 honors context.
    # The param is OPTIONAL in the schema (the realistic shape for a
    # context-satisfiable field): the rule enforces presence, while Pydantic's
    # Pass 3 won't independently require it.
    """A step overriding input_rules() with a ContextSatisfiableRule is honored by Pass 2.

    The default derivation never emits this rule type, so it can only be reached
    via an override — the realistic shape is an OPTIONAL schema field (so Pydantic
    doesn't independently require it) whose presence the rule enforces. Covers all
    three outcomes: unsatisfied, satisfied via the named upstream key, and
    satisfied explicitly.
    """
    class _CtxParams(BaseModel):
        """Params where 'command' is optional, letting the rule own its required-ness."""
        command: str | None = Field(None)
        timeout: int = Field(60)
        cwd: str | None = Field(None)

    class _CtxStep(_DummyStep):
        """Step overriding input_rules() to require 'command' or upstream 'upstream_cmd'."""
        PARAMS_SCHEMA = _CtxParams

        @classmethod
        def input_rules(cls):
            """Replace the derived rules with a single ContextSatisfiableRule."""
            return [ContextSatisfiableRule("command", "upstream_cmd")]

    # Neither in params nor context -> rule fails in Pass 2.
    errors = _CtxStep.validate_params({})
    assert len(errors) == 1
    assert errors[0].field == "command"
    assert "upstream_cmd" in errors[0].issue

    # Satisfied via the named upstream context key -> Pass 2 passes; Pass 3
    # validates the merged dict (command stays optional) -> clean.
    ctx = StepContext(outputs={"upstream_cmd": "echo from-upstream"})
    assert _CtxStep.validate_params({}, ctx) == []

    # Satisfied by an explicit param -> also clean.
    assert _CtxStep.validate_params({"command": "echo direct"}) == []


# ── resolve_for_os ───────────────────────────────────────────────────────


def test_resolve_for_os_applies_variant_defaults():
    """OS_VARIANTS defaults for the target OS are merged into the params.

    This is how one step definition adapts to per-platform binaries/shells without
    the job author branching.
    """
    merged = _DummyStep.resolve_for_os({}, "macos")
    assert merged["command"] == "sw_vers"
    assert merged["timeout"] == 30


def test_resolve_for_os_explicit_param_wins():
    """An explicit param beats the OS default, while other OS defaults still apply.

    Partial override must not discard the rest of the variant.
    """
    merged = _DummyStep.resolve_for_os({"command": "my-cmd"}, "macos")
    assert merged["command"] == "my-cmd"
    # Non-overridden variant default still present.
    assert merged["timeout"] == 30


def test_resolve_for_os_none_param_dropped():
    # An explicit None does not override the OS default.
    """An explicit None does not override an OS default (same None-dropping rule as resolve())."""
    merged = _DummyStep.resolve_for_os({"command": None}, "linux")
    assert merged["command"] == "uname -a"


def test_resolve_for_os_unknown_os_returns_explicit_params_only():
    """An OS with no variant entry yields the explicit params unchanged (no error).

    Steps that only define variants for some platforms still work elsewhere.
    """
    merged = _DummyStep.resolve_for_os({"command": "x"}, "windows")
    assert merged == {"command": "x"}


def test_resolve_for_os_does_not_mutate_class_os_variants():
    # resolve_for_os copies the OS defaults; the class attribute must be untouched.
    """resolve_for_os() copies the variant dict instead of mutating the class attribute.

    OS_VARIANTS is shared CLASS state across every job on the process; mutating it
    would leak one job's params into unrelated future jobs.
    """
    before = {k: dict(v) for k, v in _DummyStep.OS_VARIANTS.items()}
    _DummyStep.resolve_for_os({"command": "override", "extra": "z"}, "macos")
    assert _DummyStep.OS_VARIANTS == before


# ── supports_os ──────────────────────────────────────────────────────────


def test_supports_os_true():
    """supports_os() accepts each OS listed in SUPPORTED_OS.

    The scheduler uses this to filter candidate nodes.
    """
    assert _DummyStep.supports_os("macos") is True
    assert _DummyStep.supports_os("linux") is True


def test_supports_os_false():
    """supports_os() rejects an OS absent from SUPPORTED_OS, so it won't be scheduled there."""
    assert _DummyStep.supports_os("windows") is False


def test_supports_os_accepts_enum_value():
    # OSType is a str-enum; its .value is what SUPPORTED_OS contains.
    """OSType enum .value strings match the SUPPORTED_OS entries.

    Callers pass either a raw string or an enum value; both must compare equal.
    """
    assert _DummyStep.supports_os(OSType.MACOS.value) is True


# ── to_schema ────────────────────────────────────────────────────────────


def test_to_schema_top_level_metadata():
    """to_schema() exports the step-level metadata the /steps API and UI render.

    Without a _registry_name the class name is used, and large_output defaults to
    False (it gates artifact-upload handling in the runner).
    """
    schema = _DummyStep.to_schema()
    assert schema["name"] == "_DummyStep"  # no _registry_name set
    assert schema["description"] == "A dummy step"
    assert schema["requires_node"] is True
    assert schema["supported_os"] == ["macos", "linux"]
    assert schema["output_keys"] == ["result_path"]
    assert schema["large_output"] is False
    assert schema["os_variants"] == _DummyStep.OS_VARIANTS


def test_to_schema_fields_export():
    """Per-field export carries required/description/field_type/examples/default.

    Note a required field reports default=None (there is none), which is how the UI
    distinguishes 'no default' from 'defaults to null'.
    """
    schema = _DummyStep.to_schema()
    fields = {f["name"]: f for f in schema["fields"]}

    assert fields["command"]["required"] is True
    assert fields["command"]["description"] == "Shell command to run"
    assert fields["command"]["field_type"] == "string"
    assert fields["command"]["examples"] == ["echo hi"]
    assert fields["command"]["default"] is None  # required -> no default

    assert fields["timeout"]["required"] is False
    assert fields["timeout"]["default"] == 60
    assert fields["timeout"]["field_type"] == "integer"

    assert fields["cwd"]["required"] is False
    assert fields["cwd"]["default"] is None


def test_to_schema_rules_export_matches_input_rules():
    """The exported rules mirror input_rules(), so the UI's validation hints match the server's."""
    schema = _DummyStep.to_schema()
    rule_types = {r["rule_type"] for r in schema["rules"]}
    # Default derivation: command -> required, timeout/cwd -> optional.
    assert "required" in rule_types
    assert "optional" in rule_types
    required_fields = [
        f for r in schema["rules"] if r["rule_type"] == "required" for f in r["fields"]
    ]
    assert "command" in required_fields


def test_to_schema_respects_registry_name_override():
    """When registered, the registry name (not the class name) is exported.

    The registry name is what job authors type in a .nexus file.
    """
    class _NamedStep(_DummyStep):
        """Step with an explicit _registry_name, as the @register decorator would set."""
        _registry_name = "named_step"

    assert _NamedStep.to_schema()["name"] == "named_step"


# ── to_schema: field_type mapping + value serialization edge cases ────────


def test_to_schema_field_type_mapping_covers_all_simple_types():
    """Python annotations map to the UI's field_type tokens (list/object/number/boolean/string).

    These tokens choose the input widget in the job builder, so an unmapped type
    would render the wrong control.
    """
    class _TypesParams(BaseModel):
        """One field per supported primitive type, to pin the whole mapping table."""
        a_list: list[str] = []
        a_dict: dict[str, int] = {}
        a_float: float = 1.5
        a_bool: bool = True
        a_str: str = "x"

    class _TypesStep(_DummyStep):
        """Step carrying the multi-type params schema."""
        PARAMS_SCHEMA = _TypesParams

    fields = {f["name"]: f for f in _TypesStep.to_schema()["fields"]}
    assert fields["a_list"]["field_type"] == "list"
    assert fields["a_dict"]["field_type"] == "object"
    assert fields["a_float"]["field_type"] == "number"
    assert fields["a_bool"]["field_type"] == "boolean"
    assert fields["a_str"]["field_type"] == "string"


def test_to_schema_default_factory_resolves_sentinel():
    """A default_factory default is resolved by CALLING the factory, not stringifying the sentinel.

    Regression test for a real bug: ``field_info.default`` is the
    ``PydanticUndefined`` sentinel for ``default_factory`` fields while
    ``is_required()`` returns False, so the serializer used to publish the
    literal string ``"PydanticUndefined"`` as the field's default — into the
    public step-schema API and the job-builder UI. ``to_schema`` now invokes
    the factory first.

    A second bug, fixed alongside this one: list/dict defaults used to be
    stringified too (``"['a', 'b']"``), which JobBuilder's
    buildDefaultParams() then pre-filled into the form and submitted back
    VERBATIM AS A STRING — so a step could not even be submitted with its own
    default params (Pydantic: "Input should be a valid list"). They are now
    dumped as real JSON via the field's TypeAdapter instead.
    """
    class _DefParams(BaseModel):
        """Params using default_factory, which triggers the sentinel-export bug."""
        tags: list[str] = Field(default_factory=lambda: ["a", "b"])

    class _DefStep(_DummyStep):
        """Step carrying the default_factory params schema."""
        PARAMS_SCHEMA = _DefParams

    field = _DefStep.to_schema()["fields"][0]
    assert field["name"] == "tags"
    assert field["default"] == ["a", "b"]
    assert isinstance(field["default"], list)


def test_to_schema_non_primitive_scalar_default_serialized_to_string():
    """A non-collection, non-primitive default (e.g. a bare UUID/Path/enum field)
    still gets the str() display-hint treatment — only list/dict defaults were
    the ones a client could meaningfully edit and repost as JSON."""
    import uuid

    class _UuidParams(BaseModel):
        """A field whose default is a UUID instance, not a JSON primitive."""
        token: uuid.UUID = uuid.UUID("12345678-1234-5678-1234-567812345678")

    class _UuidStep(_DummyStep):
        """Step carrying the UUID-default params schema."""
        PARAMS_SCHEMA = _UuidParams

    field = _UuidStep.to_schema()["fields"][0]
    assert field["default"] == str(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    assert isinstance(field["default"], str)


def test_to_schema_non_string_examples_coerced_to_strings():
    # Examples may be non-strings; to_schema() coerces every element to str.
    """Example values are coerced to strings for the JSON/UI surface.

    The UI renders examples as placeholder text, so mixed types would need
    client-side handling.
    """
    class _ExParams(BaseModel):
        """Params with integer examples, to prove coercion happens."""
        port: int = Field(8000, examples=[80, 443])

    class _ExStep(_DummyStep):
        """Step carrying the integer-examples params schema."""
        PARAMS_SCHEMA = _ExParams

    field = _ExStep.to_schema()["fields"][0]
    assert field["examples"] == ["80", "443"]
    assert all(isinstance(e, str) for e in field["examples"])


def test_to_schema_is_json_serializable():
    """The full schema round-trips through json.dumps.

    It backs an API endpoint, so any non-serializable value (enum, type object,
    Pydantic sentinel) would 500 the /steps route.
    """
    import json

    # The whole schema must round-trip through JSON (it backs an API endpoint).
    dumped = json.dumps(_DummyStep.to_schema())
    assert "_DummyStep" in dumped


# ── InputRule is abstract ────────────────────────────────────────────────


def test_input_rule_cannot_be_instantiated():
    """The InputRule ABC cannot be constructed directly.

    A base instance would have no validate() semantics and would silently pass
    everything.
    """
    with pytest.raises(TypeError):
        InputRule()  # type: ignore[abstract]
