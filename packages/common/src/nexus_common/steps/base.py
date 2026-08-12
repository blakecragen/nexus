"""
FlowStep ABC and supporting types for the Nexus step system.

Adapted from HVE-Automation-Worker's step architecture with additions for:
- OS-aware execution (OS_VARIANTS, SUPPORTED_OS)
- Distributed node execution (REQUIRES_NODE)
- Capability-based scheduling

Steps are plain Python classes with zero framework dependencies, making them
testable in isolation and portable to any execution environment.

Where this fits
---------------
This module defines the contract; it contains no concrete steps. Implementations
live in the separate ``nexus_steps`` package and announce themselves through
``nexus_common.steps.registry.register``. Three consumers drive this contract:

    - ``nexus_server.api.routes.jobs.submit_job`` calls ``validate_params()`` for
      every step at submit time, threading a ``StepContext`` forward so a step can
      be satisfied by an upstream step's declared ``OUTPUT_KEYS``.
    - ``nexus_server.api.routes.steps`` publishes ``to_schema()`` to the frontend,
      which renders the parameter form from it.
    - ``nexus_agent.executor.StepExecutor`` (remote steps) and
      ``nexus_server.runner.JobRunner._execute_local_step`` (``REQUIRES_NODE=False``
      control-plane steps) drive the startup/check/cancel lifecycle.

AI Note: Validation runs on the server but execution runs on the agent, and the
two are separate processes with separate registries. A step class must therefore
be importable and behave identically on both sides; anything host-specific belongs
inside ``startup()``/``check()``, never in class attributes or ``input_rules()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic_core import PydanticUndefined

from nexus_common.models.enums import OSType, StepResult


# ── Step Context ────────────────────────────────────────────────────────


@dataclass
class StepContext:
    """Carries accumulated outputs from completed steps in a job.

    When a step declares OUTPUT_KEYS, its outputs are merged into the context
    after successful completion. Downstream steps can reference these outputs
    via context-satisfiable params.

    Two distinct lifetimes use this type:

    - **Validation context** (server, submit time): built by
      ``jobs.submit_job``, which walks the plan and pre-seeds ``outputs`` with each
      step's declared ``OUTPUT_KEYS`` mapped to ``None``. Only key *presence*
      matters there — the values do not exist yet.
    - **Execution context** (agent/runner, run time): holds the real values plus
      the fields needed to call back to the server.

    Attributes:
        outputs: Accumulated job context. Keys come from upstream ``OUTPUT_KEYS``
            and explicit params; values may legitimately be ``None`` during
            validation.
        os_type: OS of the executing node, used for OS-variant resolution.
        node_id: Node executing the step. None for control-plane steps.
        job_id: Owning job; set by the agent so steps can identify their upload.
        server_url: HTTP base (e.g. ``http://host:8000``) derived by the agent from
            its ``ws://`` URL. Steps that push results back (e.g. gem5 result
            collection) POST here.
        node_api_key: Node credential the step presents on those callbacks.

    AI Note: ``server_url``/``node_api_key``/``job_id`` are only populated on the
    agent side. A control-plane step running under ``JobRunner._execute_local_step``
    gets them as None, so any step relying on them must set ``REQUIRES_NODE = True``.
    """

    outputs: dict[str, Any] = field(default_factory=dict)
    os_type: OSType | None = None
    node_id: str | None = None
    # Set by the agent so steps can call back to the server (e.g. upload results).
    job_id: str | None = None
    server_url: str | None = None     # HTTP base, e.g. http://host:8000
    node_api_key: str | None = None

    def resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Merge context outputs with explicit params. Explicit params win.

        Args:
            params: The step's configured parameters.

        Returns:
            A new dict of context outputs overlaid with the non-None params.

        AI Note: ``None``-valued params are filtered out rather than overwriting
        context. That is deliberate — an omitted optional param materializes as
        ``None`` in a Pydantic dump, and without this filter it would blank out a
        value an upstream step legitimately provided. The consequence is that a
        step cannot explicitly set a context key back to ``None``.
        """
        merged = {}
        merged.update(self.outputs)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged


# ── Validation ──────────────────────────────────────────────────────────


@dataclass
class FieldError:
    """A single validation error for a step parameter.

    Returned in lists by every ``InputRule.validate`` and by
    ``FlowStep.validate_params``; an empty list means valid. ``jobs.submit_job``
    joins the ``str()`` forms into the 400 response body, so these strings are
    user-facing.

    Attributes:
        field: Parameter name at fault. The pseudo-name ``"_schema"`` is used for
            whole-model Pydantic failures that cannot be attributed to one field.
        issue: What is wrong, phrased to follow the field name.
        example: Optional sample value appended in parentheses to help the user.
    """

    field: str
    issue: str
    example: str | None = None

    def __str__(self) -> str:
        """Render as ``"field: issue (e.g. example)"`` for API error messages."""
        msg = f"{self.field}: {self.issue}"
        if self.example:
            msg += f" (e.g. {self.example})"
        return msg


class InputRule(ABC):
    """Base class for step parameter validation rules.

    Rules express constraints that Pydantic alone cannot: "required *unless* an
    upstream step supplies it", "at least one of these". They are produced by
    ``FlowStep.input_rules()``, run during validation pass 2, and serialized by
    ``to_schema()`` so the frontend can mirror the same checks in its form.
    """

    @abstractmethod
    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Return a list of FieldErrors (empty = valid).

        Args:
            params: The step's raw configured params (not context-merged).
            context: Accumulated upstream outputs; during submit-time validation
                the values are placeholders, so only key presence is meaningful.

        Returns:
            One ``FieldError`` per violation; empty list if the rule is satisfied.
        """

    @abstractmethod
    def to_schema(self) -> dict:
        """Export rule as a JSON-serializable dict for the API/UI.

        Returns:
            A dict with at least ``rule_type`` and ``fields``, matching
            ``schemas.InputRuleSchema``. The frontend switches on ``rule_type``.
        """


class RequiredRule(InputRule):
    """Field must be present in params or context.

    The default rule generated for any ``PARAMS_SCHEMA`` field without a default.

    Args:
        field_name: Parameter that must be supplied.
        description: Help text surfaced in the published schema.
    """

    def __init__(self, field_name: str, description: str = ""):
        self.field_name = field_name
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Pass if the field appears in params or anywhere in the job context.

        AI Note: Accepting a context hit makes this weaker than its name suggests
        — a step's required field is satisfied by *any* upstream step exporting a
        key of that name, even from an unrelated part of the plan. That is what
        makes implicit step chaining work, but it means name collisions across
        steps silently satisfy requirements.
        """
        if self.field_name not in params and self.field_name not in context.outputs:
            return [FieldError(self.field_name, "required")]
        return []

    def to_schema(self) -> dict:
        """Serialize as ``rule_type="required"`` over the single field."""
        return {"rule_type": "required", "fields": [self.field_name],
                "description": self.description}


class OptionalRule(InputRule):
    """Field is optional — always passes validation.

    Generated for every ``PARAMS_SCHEMA`` field that has a default. It exists
    purely so the published schema can list the field as known-and-optional;
    it never rejects anything.

    Args:
        field_name: Parameter being described.
    """

    def __init__(self, field_name: str):
        self.field_name = field_name

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Always valid. Type checking of the value happens in Pydantic pass 3."""
        return []

    def to_schema(self) -> dict:
        """Serialize as ``rule_type="optional"`` over the single field."""
        return {"rule_type": "optional", "fields": [self.field_name]}


class ContextSatisfiableRule(InputRule):
    """Field is required UNLESS an upstream step already provided it via context.

    The explicit form of step chaining: ``gem5_collect_results`` can omit
    ``m5out_path`` because an upstream gem5 run step declares it in ``OUTPUT_KEYS``.
    Unlike ``RequiredRule``, the context key checked can differ from the param name.

    Args:
        field_name: Parameter the user may omit.
        context_key: Upstream output key that satisfies it instead.
        description: Help text surfaced in the published schema.
    """

    def __init__(self, field_name: str, context_key: str, description: str = ""):
        self.field_name = field_name
        self.context_key = context_key
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Pass if the param is given, or if ``context_key`` exists in the context.

        AI Note: The error message names the satisfying context key, which is the
        main diagnostic a user gets when they reorder steps and break a chain —
        keep it intact.
        """
        if self.field_name in params:
            return []
        if self.context_key in context.outputs:
            return []
        return [FieldError(
            self.field_name,
            f"required (or provide via upstream step output '{self.context_key}')",
        )]

    def to_schema(self) -> dict:
        """Serialize with the extra ``context_key`` so the UI can explain the chain."""
        return {"rule_type": "context_satisfiable", "fields": [self.field_name],
                "description": self.description, "context_key": self.context_key}


class AtLeastOneRule(InputRule):
    """At least one of the listed fields must be present.

    For steps with mutually-alternative inputs (e.g. inline ``code`` vs a
    ``script_path``). Must be wired up by overriding ``FlowStep.input_rules()``;
    it is never generated automatically.

    Args:
        field_names: The alternatives. Order matters only for error reporting.
        description: Help text surfaced in the published schema.
    """

    def __init__(self, field_names: list[str], description: str = ""):
        self.field_names = field_names
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Pass if any alternative appears in params or in the job context.

        AI Note: The error is attributed to ``field_names[0]`` because a
        ``FieldError`` names exactly one field — the message lists them all, but the
        field attribute is arbitrary. A UI that highlights only ``error.field`` will
        flag just the first alternative.
        """
        combined = {**context.outputs, **params}
        if not any(f in combined for f in self.field_names):
            return [FieldError(
                self.field_names[0],
                f"at least one of {self.field_names} is required",
            )]
        return []

    def to_schema(self) -> dict:
        """Serialize as ``rule_type="at_least_one"`` listing every alternative."""
        return {"rule_type": "at_least_one", "fields": self.field_names,
                "description": self.description}


# ── FlowStep ABC ────────────────────────────────────────────────────────


def _simplify_type(annotation) -> str:
    """Convert a Python type annotation to a simple string for the UI.

    Args:
        annotation: The raw ``FieldInfo.annotation`` from a Pydantic model.

    Returns:
        One of "list", "object", "integer", "number", "boolean", "string".

    AI Note: This matches on ``str(annotation)`` substrings, not on the type
    itself, and the checks are ordered — first match wins. That has real
    consequences worth knowing before "fixing" it:
      - ``list[int]`` reports "list" (correct) because list is tested first.
      - ``bool`` is tested *after* ``int``, but "bool" does not contain "int", so
        booleans still resolve correctly.
      - Anything unrecognized, including ``Literal`` and enum types, falls through
        to "string".
    The output only selects a form widget in the frontend; the authoritative type
    check is Pydantic's, so a wrong guess degrades UX, not correctness.
    """
    s = str(annotation)
    if "list" in s.lower():
        return "list"
    if "dict" in s.lower():
        return "object"
    if "int" in s.lower():
        return "integer"
    if "float" in s.lower():
        return "number"
    if "bool" in s.lower():
        return "boolean"
    return "string"


class FlowStep(ABC):
    """Abstract base class for all Nexus step implementations.

    Adapted from HVE-Automation-Worker's FlowStep with Nexus extensions:
    - OS_VARIANTS: OS-specific parameter defaults merged before execution
    - SUPPORTED_OS: Which operating systems can execute this step
    - REQUIRES_NODE: Whether step needs a compute node (False for flow/control steps)
    - LARGE_OUTPUT: Hint for storage manager to prefer high-capacity backends

    Lifecycle:
        1. validate_params() — at submission time (on the server)
        2. resolve_for_os() — before dispatching to agent
        3. startup(params, ctx) — on the agent; returns serializable state dict
        4. check(state) → StepResult — polled until SUCCESS or FAILED
        5. cancel(state) — signal graceful termination

    Implementation contract:
        - Subclasses are instantiated with no arguments (``step_cls()``), so
          ``__init__`` must take no required parameters. All configuration arrives
          through ``startup(params, ctx)``.
        - A step instance is *not* reused across the startup/check boundary in the
          crash-recovery path: after a server restart, ``check(state)`` may be
          called on a fresh instance. All cross-call state must live in the
          returned state dict, never on ``self``.
        - The state dict is persisted as JSON on the StepRun row, so it must be
          JSON-serializable end to end.
        - Steps whose state contains a ``"command"`` key get special treatment on
          the agent: ``StepExecutor`` supervises it as a subprocess and streams its
          output, instead of polling ``check()``.
    """

    # ── Required class attributes ──

    PARAMS_SCHEMA: type[BaseModel]
    """Pydantic model defining the step's parameters.

    Not optional despite the bare annotation — ``input_rules()``, ``to_schema()``,
    and ``validate_params()`` all dereference it, so a subclass that omits it fails
    with AttributeError at first use rather than at class definition."""

    # ── Optional class attributes ──

    OUTPUT_KEYS: list[str] = []
    """Keys this step adds to the job context on success.

    Load-bearing in two places: the agent harvests exactly these keys out of the
    step's state dict to build ``StepCompleted.outputs``, and the server pre-seeds
    them into the validation context so downstream ``ContextSatisfiableRule``
    fields pass at submit time. A value a step writes into state but does not
    declare here is invisible to every later step."""

    DESCRIPTION: str = ""
    """Short one-line description shown in the step palette.

    Published verbatim as ``StepSchemaInfo.description``, so it is user-facing UI
    text rather than an internal note."""

    DOCS: str = ""
    """Full markdown documentation for this step.

    AI Note: Declared here but not included in ``to_schema()``, so it is currently
    not served by ``/api/steps``. Populating it is harmless but has no visible
    effect until the schema export carries it."""

    REQUIRES_NODE: bool = True
    """If False, step runs on the control plane (e.g., sleep, jump).

    Control-plane steps execute inside the server process via
    ``JobRunner._execute_local_step``: they are never dispatched to an agent, get
    no ``server_url``/``node_api_key`` in their context, and must not touch the
    node's filesystem or spawn long-running work."""

    SUPPORTED_OS: list[str] = ["macos", "linux", "windows"]
    """Which operating systems can execute this step.

    Values are the ``OSType`` string values. Narrowing this restricts which nodes
    the scheduler will consider."""

    OS_VARIANTS: dict[str, dict[str, Any]] = {}
    """OS-specific parameter defaults. Merged before dispatch; explicit params win.

    Example:
        OS_VARIANTS = {
            "macos":   {"shell": "/bin/zsh"},
            "linux":   {"shell": "/bin/bash"},
            "windows": {"shell": "powershell.exe"},
        }

    AI Note: Keys are matched by string against the node's ``os_type``; an OS with
    no entry simply gets no defaults rather than an error. ``resolve_for_os`` is
    applied twice — once server-side before dispatch and again agent-side against
    the locally detected OS — which is safe only because explicit params always
    beat variant defaults, making the operation idempotent. Keep it that way."""

    LARGE_OUTPUT: bool = False
    """Hint for the storage manager to prefer high-capacity backends.

    AI Note: Exported by ``to_schema()`` but dropped by ``StepSchemaInfo``, which
    does not declare the field — so it never reaches the frontend today."""

    # ── Validation ──

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        """Derive validation rules from PARAMS_SCHEMA.

        Override this method to provide custom rules (e.g., AtLeastOneRule).
        Default implementation: required fields → RequiredRule, optional → OptionalRule.

        Returns:
            One rule per declared parameter, in ``PARAMS_SCHEMA`` field order.

        AI Note: Overriding replaces the derived list wholesale — it is not additive.
        An override that returns only an ``AtLeastOneRule`` silently drops the
        RequiredRule for every other field; those fields are then only caught by
        Pydantic in pass 3, with a less friendly message. Rebuild the full list
        (typically ``super().input_rules() + [extra]``) unless you mean to replace it.
        """
        rules: list[InputRule] = []
        for name, field_info in cls.PARAMS_SCHEMA.model_fields.items():
            if field_info.is_required():
                rules.append(RequiredRule(name, field_info.description or ""))
            else:
                rules.append(OptionalRule(name))
        return rules

    @classmethod
    def validate_params(cls, params: dict, context: StepContext | None = None) -> list[FieldError]:
        """Three-pass validation (run at submission time on the server).

        Pass 1: Unknown params — reject keys not in PARAMS_SCHEMA
        Pass 2: Input rules — required, context-satisfiable, at-least-one
        Pass 3: Type/value — Pydantic type validation

        Args:
            params: The step's configured parameters, exactly as submitted.
            context: Accumulated upstream outputs. Defaults to an empty context,
                which makes every context-satisfiable field behave as required —
                fine for standalone validation, wrong for validating a step inside
                a plan, so ``jobs.submit_job`` always threads a real one.

        Returns:
            All ``FieldError``s found in the first failing pass; empty list if valid.

        AI Note: Passes 1 and 2 short-circuit — if either finds errors, later passes
        do not run. That is intentional: an unknown-key typo would otherwise also
        trigger a confusing Pydantic error, and a missing required field would
        produce two reports of the same problem. The trade-off is that a submission
        with several unrelated mistakes surfaces them one pass at a time.
        """
        ctx = context or StepContext()
        errors: list[FieldError] = []

        # Pass 1: unknown params
        known_fields = set(cls.PARAMS_SCHEMA.model_fields.keys())
        for key in params:
            if key not in known_fields:
                errors.append(FieldError(key, f"unknown parameter (valid: {sorted(known_fields)})"))
        if errors:
            return errors

        # Pass 2: input rules
        for rule in cls.input_rules():
            errors.extend(rule.validate(params, ctx))
        if errors:
            return errors

        # Pass 3: type/value validation via Pydantic
        #
        # AI Note: Validates the *context-merged* dict, not the raw params, so a
        # field supplied by an upstream step is type-checked too. During submit-time
        # validation those context values are placeholder ``None``s, which means a
        # required field satisfied only by context can still fail here on type — the
        # broad ``except`` funnels that into a single "_schema" error rather than
        # letting it 500 the request.
        try:
            merged = ctx.resolve(params)
            adapter = TypeAdapter(cls.PARAMS_SCHEMA)
            adapter.validate_python(merged)
        except Exception as e:
            errors.append(FieldError("_schema", str(e)))

        # Pass 4: semantic validation (step-specific hook)
        #
        # AI Note: Pass 4 runs even when pass 3 failed (no short-circuit here), so
        # semantic hooks must tolerate params that did not type-check. The docstring
        # above says "three-pass" for historical reasons; there are four.
        errors.extend(cls.validate_semantic(params, ctx))

        return errors

    @classmethod
    def validate_semantic(cls, params: dict, context: StepContext) -> list[FieldError]:
        """Override for step-specific semantic validation (e.g., bounds checks).

        Runs after Pydantic type validation, for constraints types cannot express:
        cross-field consistency, value ranges, mutually exclusive options.

        Args:
            params: Raw configured params (not context-merged).
            context: Accumulated upstream outputs.

        Returns:
            Violations found; empty list by default (base implementation is a no-op).

        AI Note: This runs on the *server* at submit time, so it must not touch the
        node's filesystem, network, or any host-specific resource — the checks would
        be evaluated against the wrong machine. Save those for ``startup()``.
        """
        return []

    # ── OS Resolution ──

    @classmethod
    def resolve_for_os(cls, params: dict, os_type: str) -> dict:
        """Merge OS-specific defaults into params. Explicit params always win.

        Args:
            params: Configured (already context-merged) parameters.
            os_type: Target OS string, e.g. "linux".

        Returns:
            A new dict: this OS's ``OS_VARIANTS`` entry overlaid with the non-None
            params. An unknown ``os_type`` yields the params unchanged.

        AI Note: Called twice per remote step — server-side before dispatch, then
        agent-side with the locally detected OS. Idempotent because explicit params
        override defaults, so the second pass cannot undo the first. Like
        ``StepContext.resolve``, ``None`` params are dropped rather than allowed to
        blank out an OS default.
        """
        os_defaults = cls.OS_VARIANTS.get(os_type, {})
        merged = dict(os_defaults)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged

    @classmethod
    def supports_os(cls, os_type: str) -> bool:
        """Check if this step supports the given OS.

        Args:
            os_type: OS string to test, e.g. "macos".

        Returns:
            True if listed in ``SUPPORTED_OS``. Plain string comparison, so an
            ``OSType`` member also matches (the enum subclasses ``str``).
        """
        return os_type in cls.SUPPORTED_OS

    # ── Schema Export ──

    @classmethod
    def to_schema(cls) -> dict:
        """Export step metadata as a JSON-serializable dict for the API/UI.

        Consumed by ``GET /api/steps`` (via ``schemas.StepSchemaInfo``) and is the
        only description the frontend has of a step, since it cannot import the
        Python class.

        Returns:
            A dict of name/description/targeting metadata plus a ``fields`` list
            (one per parameter) and a ``rules`` list. Everything is coerced to
            JSON-safe primitives.

        AI Note: The returned ``large_output`` key has no counterpart on
        ``StepSchemaInfo`` and is discarded when the route constructs that model.
        """
        fields = []
        for name, field_info in cls.PARAMS_SCHEMA.model_fields.items():
            # AI Note: Examples can arrive two ways depending on how the field was
            # declared — inside json_schema_extra (older style) or as the dedicated
            # FieldInfo.examples attribute. Both are checked so step authors can use
            # either; the extra dict wins when both are present.
            examples_raw = []
            if field_info.json_schema_extra and "examples" in field_info.json_schema_extra:
                examples_raw = field_info.json_schema_extra["examples"]
            elif hasattr(field_info, "examples") and field_info.examples:
                examples_raw = field_info.examples

            # Ensure all examples are strings (some fields use list/dict examples)
            examples = [str(e) if not isinstance(e, str) else e for e in (examples_raw or [])]

            # Serialize default values for JSON compatibility
            #
            # AI Note: three distinct cases, and they must be handled in this
            # order. A ``default_factory`` field (e.g. Field(default_factory=list))
            # reports is_required() == False but its ``.default`` is the
            # ``PydanticUndefined`` sentinel, NOT the real default — stringifying
            # it published the literal "PydanticUndefined" as the field's default
            # into the public step-schema API and the job-builder UI. Call the
            # factory instead. Required fields still report None (their real
            # default is the sentinel too, and there is nothing to publish).
            if field_info.default_factory is not None:
                default_val = field_info.default_factory()
            elif not field_info.is_required():
                default_val = field_info.default
            else:
                default_val = None
            # Belt-and-braces: never let the sentinel reach JSON.
            if default_val is PydanticUndefined:
                default_val = None
            # AI Note: list/dict defaults are dumped as real JSON via the
            # field's own TypeAdapter, not stringified. Regression fix — the
            # old `str(default_val)` path published e.g. `"['cpu', 'memory']"`
            # as the default, which JobBuilder's buildDefaultParams() then
            # pre-filled into the form and submitted back VERBATIM AS A
            # STRING on an unopened step, so Pydantic rejected it with
            # "Input should be a valid list" — a step could not be submitted
            # with its own default params. TypeAdapter (rather than bare
            # json.dumps) is what correctly JSON-serializes non-trivial
            # element types (UUID, Path, enum, ...) that could appear inside
            # the collection. Any other remaining non-primitive default (a
            # bare UUID/Path/enum field) still gets the str() display-hint
            # treatment below — those aren't collections a client could
            # meaningfully edit and repost anyway.
            if isinstance(default_val, (list, dict)):
                try:
                    default_val = TypeAdapter(field_info.annotation).dump_python(
                        default_val, mode="json",
                    )
                except Exception:
                    default_val = str(default_val)
            elif default_val is not None and not isinstance(default_val, (str, int, float, bool)):
                default_val = str(default_val)

            fields.append({
                "name": name,
                "required": field_info.is_required(),
                "description": field_info.description or "",
                "default": default_val,
                "examples": examples,
                "field_type": _simplify_type(field_info.annotation) if field_info.annotation else "string",
            })

        rules = [rule.to_schema() for rule in cls.input_rules()]

        return {
            # AI Note: `_registry_name` is stamped onto the class by the @register
            # decorator. The class-name fallback only fires for unregistered steps
            # (mainly tests) — for anything served by the API this must be the
            # registry key, because that is the string jobs use in StepConfig.step.
            "name": getattr(cls, "_registry_name", cls.__name__),
            "description": cls.DESCRIPTION,
            "requires_node": cls.REQUIRES_NODE,
            "supported_os": cls.SUPPORTED_OS,
            "output_keys": cls.OUTPUT_KEYS,
            "fields": fields,
            "rules": rules,
            "os_variants": cls.OS_VARIANTS,
            "large_output": cls.LARGE_OUTPUT,
        }

    # ── Execution Interface ──

    @abstractmethod
    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Initialize step execution. Returns a serializable state dict.

        Called on the agent. The returned state is persisted to the DB
        for crash recovery — if the server restarts, check(state) is
        called directly without re-running startup().

        Args:
            params: Fully resolved parameters (context-merged, OS variants applied).
            ctx: Execution context — OS, node id, and the server callback fields
                (``job_id``/``server_url``/``node_api_key``) when running on an agent.

        Returns:
            A JSON-serializable state dict carrying everything ``check()`` and
            ``cancel()`` need. Two conventions the executor keys off:
              - ``"command"``: the agent runs it as a supervised subprocess and
                streams its output instead of polling ``check()``.
              - ``OUTPUT_KEYS`` entries: written directly into this dict (not into
                a nested "outputs" key) and harvested on success.

        Side effects: this is where a step legitimately spawns processes, writes
        files, or opens connections. Handles must be recorded in the state dict in
        a resumable form (pids, paths), not held on ``self``.
        """

    @abstractmethod
    def check(self, state: dict[str, Any]) -> StepResult:
        """Poll step progress. Called repeatedly until SUCCESS or FAILED.

        Must be idempotent — safe to call multiple times.

        Args:
            state: The dict returned by ``startup()``, possibly reloaded from the DB
                after a server restart and passed to a *fresh* instance.

        Returns:
            RUNNING to be polled again, SUCCESS to harvest ``OUTPUT_KEYS`` from
            state, or FAILED (put the reason in ``state["error"]`` — the local
            runner reads exactly that key).

        AI Note: There is no timeout enforced by either executor; a step that keeps
        returning RUNNING blocks its job indefinitely. Steps must impose their own
        deadline and return FAILED. ``check()`` may also mutate ``state`` in place to
        record progress or results — both executors read the same dict afterwards.
        """

    @abstractmethod
    def cancel(self, state: dict[str, Any]) -> None:
        """Request graceful cancellation of the step.

        Args:
            state: The dict returned by ``startup()``.

        Best-effort and advisory: the executor calls this on a
        ``CancelStepCommand`` and does not wait for or verify termination.
        Implementations should be tolerant of being called on a step that already
        finished, and must not raise — a throwing ``cancel()`` propagates into the
        agent's command handler.
        """
