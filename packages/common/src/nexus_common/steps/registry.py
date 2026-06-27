"""Step registry — maps step names to FlowStep classes.

Steps self-register via the @register decorator:

    from nexus_common.steps.registry import register

    @register("my_step")
    class MyStep(FlowStep):
        ...

The registry is populated when step modules are imported. Call load_steps()
to import all built-in step modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus_common.steps.base import FlowStep

STEP_REGISTRY: dict[str, type[FlowStep]] = {}


def register(name: str):
    """Class decorator that registers a FlowStep subclass by name.

    Usage:
        @register("run_command")
        class RunCommandStep(FlowStep):
            ...
    """
    def decorator(cls: type[FlowStep]) -> type[FlowStep]:
        if name in STEP_REGISTRY:
            raise ValueError(
                f"Step '{name}' already registered by {STEP_REGISTRY[name].__name__}. "
                f"Cannot register {cls.__name__} with the same name."
            )
        cls._registry_name = name  # type: ignore[attr-defined]
        STEP_REGISTRY[name] = cls
        return cls
    return decorator


def get_step(name: str) -> type[FlowStep]:
    """Look up a registered step by name. Raises KeyError if not found."""
    if name not in STEP_REGISTRY:
        available = sorted(STEP_REGISTRY.keys())
        raise KeyError(f"Unknown step '{name}'. Available: {available}")
    return STEP_REGISTRY[name]


def list_steps() -> list[str]:
    """Return sorted list of all registered step names."""
    return sorted(STEP_REGISTRY.keys())
