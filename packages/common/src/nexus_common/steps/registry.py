"""Step registry — maps step names to FlowStep classes.

Steps self-register via the @register decorator:

    from nexus_common.steps.registry import register

    @register("my_step")
    class MyStep(FlowStep):
        ...

The registry is populated when step modules are imported. Call load_steps()
to import all built-in step modules.

How population actually happens
-------------------------------
There is no ``load_steps()`` function in this module (see the note below). In
practice both processes populate the registry with a bare import of the
``nexus_steps`` package, whose ``__init__`` imports every step module and thereby
fires the decorators:

    nexus_server.main:      import nexus_steps  # noqa: F401
    nexus_agent.executor:   import nexus_steps  # noqa: F401
    tests/conftest.py:      import nexus_steps  # noqa: F401

Consumers of the populated registry:
    - ``jobs.submit_job`` — rejects unknown step names and validates params.
    - ``routes/steps.py``  — publishes ``to_schema()`` for the frontend palette.
    - ``StepExecutor``     — resolves the class named in ``ExecuteStepCommand``.

AI Note: ``STEP_REGISTRY`` is process-global mutable state populated by import
side effects, with three consequences worth knowing:
  1. Server and agent are *separate* processes with independent registries. If
     they run different ``nexus_steps`` builds, a step can validate on the server
     and then fail with a KeyError on the agent.
  2. Registration is not idempotent — re-importing under a different module path
     (a real hazard with test sys.path juggling) raises on the duplicate name.
  3. Anything that reads the registry at import time rather than at call time can
     observe it half-populated. Read it inside functions.

Docstring caveat: the "Call load_steps()" sentence above is inherited and
inaccurate — no such symbol exists here. Left in place because the sentence is
pre-existing text; the accurate mechanism is documented above.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

# AI Note: FlowStep is imported only for typing. A real import would be circular
# (base.py -> registry.py via the package __init__), and the module-level
# `from __future__ import annotations` is what makes the deferred annotation legal.
if TYPE_CHECKING:
    from nexus_common.steps.base import FlowStep

STEP_REGISTRY: dict[str, type[FlowStep]] = {}
"""Global name -> FlowStep-subclass map, the single source of truth for which
steps exist in this process. Keys are the strings used in ``StepConfig.step`` and
``ExecuteStepCommand.step_name``. Mutated only by ``register``; treat it as
read-only everywhere else."""


def register(name: str):
    """Class decorator that registers a FlowStep subclass by name.

    Usage:
        @register("run_command")
        class RunCommandStep(FlowStep):
            ...

    Args:
        name: Public registry key. This string is what users write in a job plan
            and what crosses the wire, so it is part of the API contract —
            renaming it breaks saved templates and any ``.nexus`` file using it.

    Returns:
        The actual decorator, which registers the class and returns it unchanged.

    Raises:
        ValueError: If ``name`` is already taken, naming both the incumbent and
            the newcomer. Raised at *import* time, so a duplicate registration
            prevents the server or agent from starting rather than causing a
            silent, order-dependent overwrite later.

    Side effects: mutates the module-global ``STEP_REGISTRY`` and stamps
    ``_registry_name`` onto the class (read back by ``FlowStep.to_schema()`` so the
    published schema reports the registry key rather than the Python class name).
    """
    def decorator(cls: type[FlowStep]) -> type[FlowStep]:
        """Register ``cls`` under the captured name and return it unmodified."""
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
    """Look up a registered step by name. Raises KeyError if not found.

    Args:
        name: Registry key, e.g. "run_command".

    Returns:
        The registered ``FlowStep`` subclass (the class itself, not an instance —
        callers construct with ``step_cls()``).

    Raises:
        KeyError: If unregistered. The message lists every available name, which is
            the primary diagnostic when a step exists on the server but not on the
            agent (or when ``nexus_steps`` was never imported, leaving the registry
            empty).
    """
    if name not in STEP_REGISTRY:
        available = sorted(STEP_REGISTRY.keys())
        raise KeyError(f"Unknown step '{name}'. Available: {available}")
    return STEP_REGISTRY[name]


def list_steps() -> list[str]:
    """Return sorted list of all registered step names.

    Returns:
        Alphabetically sorted registry keys. Sorted rather than
        insertion-ordered so ``GET /api/steps`` and error messages are stable
        regardless of module import order.
    """
    return sorted(STEP_REGISTRY.keys())
