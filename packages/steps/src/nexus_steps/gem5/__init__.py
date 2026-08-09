"""gem5 architectural-simulation steps.

Two steps designed to be chained in a single job:

1. ``gem5_run_simulation`` — launches gem5 (directly on the node, or inside a
   Docker container via ``docker exec``) and publishes ``m5out_path`` and
   ``container`` into the job context.
2. ``gem5_collect_results`` — picks those context values up automatically
   (via ``ContextSatisfiableRule``), tars the m5out directory and uploads it
   to the server as the job's downloadable result artifact.

Both set ``LARGE_OUTPUT = True`` so the storage manager prefers high-capacity
backends, and both restrict ``SUPPORTED_OS`` to macOS/Linux (there is no
Windows gem5 path).

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
