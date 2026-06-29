"""Nexus built-in step implementations.

Importing this package (or calling :func:`load_all_steps`) triggers the
``@register`` decorators in every step module, populating the global
:data:`nexus_common.steps.registry.STEP_REGISTRY`.
"""

from __future__ import annotations

import importlib
import logging

logger = logging.getLogger(__name__)

# Every module that contains an @register-decorated FlowStep subclass.
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
]


def load_all_steps() -> list[str]:
    """Import every built-in step module to trigger @register decorators.

    Returns:
        List of module names that were successfully imported.
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
load_all_steps()
