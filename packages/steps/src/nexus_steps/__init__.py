"""Nexus built-in step implementations.

Importing this package (or calling :func:`load_all_steps`) triggers the
``@register`` decorators in every step module, populating the global
:data:`nexus_common.steps.registry.STEP_REGISTRY`.

Role in the system
------------------
``nexus_steps`` is the catalogue of *what the cluster can actually do*. Each
submodule defines a :class:`~nexus_common.steps.base.FlowStep` subclass plus a
Pydantic params model, and registers itself under a stable string name (e.g.
``"run_command"``). That name is the contract shared by three consumers:

* **Server / API** — ``nexus_server`` imports this package so the ``/api/steps``
  schema endpoint and submit-time ``validate_params()`` can see every step.
* **Agent** — ``nexus_agent.executor`` imports it (``import nexus_steps``) so
  ``get_step(cmd.step_name)`` resolves the class that will run on the node.
* **Frontend** — renders the step palette from the exported ``to_schema()``
  dicts, so a step's ``DESCRIPTION`` and field descriptions are user-facing.

This package owns no execution logic itself; it only performs discovery.

AI Note: the registry is a *global mutable dict* and ``@register`` raises on a
duplicate name. Because both the server and the agent import this package into
the same process in some deployments, ``load_all_steps()`` must remain
idempotent — it is, because Python module caching means a second call re-imports
nothing and therefore never re-runs a decorator.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Every module that contains an @register-decorated FlowStep subclass.
#
# AI Note: this list is the single source of truth for step discovery — there is
# no filesystem scan. A new step file is invisible to the whole system until its
# dotted module path is added here. Ordering is cosmetic (registry is a dict
# keyed by name), but keep related steps adjacent for readability.
_STEP_MODULES = [
    "nexus_steps.shell.run_command",
    "nexus_steps.shell.run_script",
    "nexus_steps.python.run",
    "nexus_steps.flow.sleep",
    "nexus_steps.flow.jump",
    "nexus_steps.git.clone",
    "nexus_steps.git.pull",
    "nexus_steps.docker.ensure_container",
    "nexus_steps.gem5.run_simulation",
    "nexus_steps.gem5.collect_results",
    "nexus_steps.package.install",
    "nexus_steps.system.health_check",
    "nexus_steps.system.update_software",
]


def load_all_steps() -> list[str]:
    """Import every built-in step module to trigger @register decorators.

    Side effects:
        Imports each module in ``_STEP_MODULES``, which as a side effect
        mutates the process-global ``STEP_REGISTRY`` in
        :mod:`nexus_common.steps.registry`.

    Returns:
        List of module names that were successfully imported. A module missing
        from the returned list means its steps are NOT registered and any job
        referencing them will fail at ``get_step()`` with a ``KeyError``.

    AI Note: failures are swallowed per-module (logged via ``logger.exception``)
    rather than raised. This is deliberate: one step whose optional third-party
    dependency is absent on a given node must not prevent the agent from
    starting and running the other steps. The cost is that a typo'd import
    degrades silently to "step not found" at job time — check the agent log for
    "Failed to load step module" when a step mysteriously disappears.
    """
    loaded: list[str] = []
    for module_name in _STEP_MODULES:
        try:
            importlib.import_module(module_name)
            loaded.append(module_name)
        except Exception:
            logger.exception("Failed to load step module: %s", module_name)
    return loaded


# Auto-load on package import so that ``import nexus_steps`` is sufficient
# to populate the registry.
#
# AI Note: import-time side effect, kept on purpose. ``nexus_agent.executor``
# relies on a bare ``import nexus_steps  # noqa: F401`` to populate the registry
# before it ever calls ``get_step()``. Removing this call silently breaks every
# remote step dispatch.
load_all_steps()
