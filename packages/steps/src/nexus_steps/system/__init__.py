"""Node introspection / maintenance steps.

Holds :mod:`nexus_steps.system.health_check`, a cheap read-only probe of CPU,
memory, disk and network intended as the first step of a job so an unhealthy
node fails fast before an expensive workload is dispatched to it, and
:mod:`nexus_steps.system.update_software`, which pulls the node's own Nexus
checkout up to date and restarts its agent.

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
