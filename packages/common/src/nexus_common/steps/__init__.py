"""Re-export core step types for convenience.

The public face of the step framework. Step authors and executors import from
here (``from nexus_common.steps import FlowStep, StepContext, register``) rather
than reaching into ``base`` and ``registry`` directly, so the split between the
two submodules stays an implementation detail.

Contents:
    - From ``base``:     the ``FlowStep`` ABC, ``StepContext``, ``FieldError``, and
      the ``InputRule`` family used to declare parameter constraints.
    - From ``registry``: ``register`` (the decorator step classes apply),
      ``get_step`` / ``list_steps`` (lookup), and the global ``STEP_REGISTRY``.

AI Note: Importing this package pulls in ``base`` -> ``registry``, but it does NOT
import any concrete steps — those live in ``nexus_steps`` and must be imported
separately for ``STEP_REGISTRY`` to be non-empty. Also note ``__all__`` is the
package's declared surface: a name added to ``base`` or ``registry`` is not
re-exported until it is listed both in the import block and in ``__all__`` below.
"""

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
from nexus_common.steps.registry import STEP_REGISTRY, get_step, list_steps, register

__all__ = [
    "AtLeastOneRule",
    "ContextSatisfiableRule",
    "FieldError",
    "FlowStep",
    "InputRule",
    "OptionalRule",
    "RequiredRule",
    "STEP_REGISTRY",
    "StepContext",
    "get_step",
    "list_steps",
    "register",
]
"""Public names of the step framework, controlling ``from ... import *`` and
documenting the intended API surface. Note ``nexus_common.steps.base.InputRule``
subclasses are exported but ``_simplify_type`` and the ``base``/``registry``
modules themselves are not."""
