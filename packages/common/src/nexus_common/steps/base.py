"""
FlowStep ABC and supporting types for the Nexus step system.

Adapted from HVE-Automation-Worker's step architecture with additions for:
- OS-aware execution (OS_VARIANTS, SUPPORTED_OS)
- Distributed node execution (REQUIRES_NODE)
- Capability-based scheduling

Steps are plain Python classes with zero framework dependencies, making them
testable in isolation and portable to any execution environment.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, TypeAdapter

from nexus_common.models.enums import OSType, StepResult


# ── Step Context ────────────────────────────────────────────────────────


@dataclass
class StepContext:
    """Carries accumulated outputs from completed steps in a job.

    When a step declares OUTPUT_KEYS, its outputs are merged into the context
    after successful completion. Downstream steps can reference these outputs
    via context-satisfiable params.
    """

    outputs: dict[str, Any] = field(default_factory=dict)
    os_type: OSType | None = None
    node_id: str | None = None

    def resolve(self, params: dict[str, Any]) -> dict[str, Any]:
        """Merge context outputs with explicit params. Explicit params win."""
        merged = {}
        merged.update(self.outputs)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged


# ── Validation ──────────────────────────────────────────────────────────


@dataclass
class FieldError:
    """A single validation error for a step parameter."""

    field: str
    issue: str
    example: str | None = None

    def __str__(self) -> str:
        msg = f"{self.field}: {self.issue}"
        if self.example:
            msg += f" (e.g. {self.example})"
        return msg


class InputRule(ABC):
    """Base class for step parameter validation rules."""

    @abstractmethod
    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        """Return a list of FieldErrors (empty = valid)."""

    @abstractmethod
    def to_schema(self) -> dict:
        """Export rule as a JSON-serializable dict for the API/UI."""


class RequiredRule(InputRule):
    """Field must be present in params or context."""

    def __init__(self, field_name: str, description: str = ""):
        self.field_name = field_name
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        if self.field_name not in params and self.field_name not in context.outputs:
            return [FieldError(self.field_name, "required")]
        return []

    def to_schema(self) -> dict:
        return {"rule_type": "required", "fields": [self.field_name],
                "description": self.description}


class OptionalRule(InputRule):
    """Field is optional — always passes validation."""

    def __init__(self, field_name: str):
        self.field_name = field_name

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        return []

    def to_schema(self) -> dict:
        return {"rule_type": "optional", "fields": [self.field_name]}


class ContextSatisfiableRule(InputRule):
    """Field is required UNLESS an upstream step already provided it via context."""

    def __init__(self, field_name: str, context_key: str, description: str = ""):
        self.field_name = field_name
        self.context_key = context_key
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        if self.field_name in params:
            return []
        if self.context_key in context.outputs:
            return []
        return [FieldError(
            self.field_name,
            f"required (or provide via upstream step output '{self.context_key}')",
        )]

    def to_schema(self) -> dict:
        return {"rule_type": "context_satisfiable", "fields": [self.field_name],
                "description": self.description, "context_key": self.context_key}


class AtLeastOneRule(InputRule):
    """At least one of the listed fields must be present."""

    def __init__(self, field_names: list[str], description: str = ""):
        self.field_names = field_names
        self.description = description

    def validate(self, params: dict, context: StepContext) -> list[FieldError]:
        combined = {**context.outputs, **params}
        if not any(f in combined for f in self.field_names):
            return [FieldError(
                self.field_names[0],
                f"at least one of {self.field_names} is required",
            )]
        return []

    def to_schema(self) -> dict:
        return {"rule_type": "at_least_one", "fields": self.field_names,
                "description": self.description}


# ── FlowStep ABC ────────────────────────────────────────────────────────


def _simplify_type(annotation) -> str:
    """Convert a Python type annotation to a simple string for the UI."""
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
    """

    # ── Required class attributes ──

    PARAMS_SCHEMA: type[BaseModel]
    """Pydantic model defining the step's parameters."""

    # ── Optional class attributes ──

    OUTPUT_KEYS: list[str] = []
    """Keys this step adds to the job context on success."""

    DESCRIPTION: str = ""
    """Short one-line description shown in the step palette."""

    DOCS: str = ""
    """Full markdown documentation for this step."""

    REQUIRES_NODE: bool = True
    """If False, step runs on the control plane (e.g., sleep, jump)."""

    SUPPORTED_OS: list[str] = ["macos", "linux", "windows"]
    """Which operating systems can execute this step."""

    OS_VARIANTS: dict[str, dict[str, Any]] = {}
    """OS-specific parameter defaults. Merged before dispatch; explicit params win.

    Example:
        OS_VARIANTS = {
            "macos":   {"shell": "/bin/zsh"},
            "linux":   {"shell": "/bin/bash"},
            "windows": {"shell": "powershell.exe"},
        }
    """

    LARGE_OUTPUT: bool = False
    """Hint for the storage manager to prefer high-capacity backends."""

    # ── Validation ──

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        """Derive validation rules from PARAMS_SCHEMA.

        Override this method to provide custom rules (e.g., AtLeastOneRule).
        Default implementation: required fields → RequiredRule, optional → OptionalRule.
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
        try:
            merged = ctx.resolve(params)
            adapter = TypeAdapter(cls.PARAMS_SCHEMA)
            adapter.validate_python(merged)
        except Exception as e:
            errors.append(FieldError("_schema", str(e)))

        # Pass 4: semantic validation (step-specific hook)
        errors.extend(cls.validate_semantic(params, ctx))

        return errors

    @classmethod
    def validate_semantic(cls, params: dict, context: StepContext) -> list[FieldError]:
        """Override for step-specific semantic validation (e.g., bounds checks)."""
        return []

    # ── OS Resolution ──

    @classmethod
    def resolve_for_os(cls, params: dict, os_type: str) -> dict:
        """Merge OS-specific defaults into params. Explicit params always win."""
        os_defaults = cls.OS_VARIANTS.get(os_type, {})
        merged = dict(os_defaults)
        merged.update({k: v for k, v in params.items() if v is not None})
        return merged

    @classmethod
    def supports_os(cls, os_type: str) -> bool:
        """Check if this step supports the given OS."""
        return os_type in cls.SUPPORTED_OS

    # ── Schema Export ──

    @classmethod
    def to_schema(cls) -> dict:
        """Export step metadata as a JSON-serializable dict for the API/UI."""
        fields = []
        for name, field_info in cls.PARAMS_SCHEMA.model_fields.items():
            examples_raw = []
            if field_info.json_schema_extra and "examples" in field_info.json_schema_extra:
                examples_raw = field_info.json_schema_extra["examples"]
            elif hasattr(field_info, "examples") and field_info.examples:
                examples_raw = field_info.examples

            # Ensure all examples are strings (some fields use list/dict examples)
            examples = [str(e) if not isinstance(e, str) else e for e in (examples_raw or [])]

            # Serialize default values for JSON compatibility
            default_val = field_info.default if not field_info.is_required() else None
            if default_val is not None and not isinstance(default_val, (str, int, float, bool)):
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
        """

    @abstractmethod
    def check(self, state: dict[str, Any]) -> StepResult:
        """Poll step progress. Called repeatedly until SUCCESS or FAILED.

        Must be idempotent — safe to call multiple times.
        """

    @abstractmethod
    def cancel(self, state: dict[str, Any]) -> None:
        """Request graceful cancellation of the step."""
