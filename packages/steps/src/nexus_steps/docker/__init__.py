"""Docker-related built-in steps.

Currently holds :mod:`nexus_steps.docker.ensure_container`, the idempotent
"create-or-attach" step used to stand up a long-lived Linux container on a
node so later steps (notably ``gem5_run_simulation``) can ``docker exec``
into it.

This package is a plain namespace — it deliberately does NOT import its
submodules. Registration is centralised in :mod:`nexus_steps` (the
``_STEP_MODULES`` list), so importing ``nexus_steps.docker`` alone will not
populate ``STEP_REGISTRY``.
"""
