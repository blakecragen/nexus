"""Git source-control steps (``git_clone``, ``git_pull``).

``git_clone`` publishes ``clone_path`` and ``commit_sha`` into the job
context; ``git_pull`` operates on an existing checkout and republishes
``commit_sha`` plus an ``updated`` flag. Both shell out to the ``git`` binary,
which must already be installed on the node (chain the ``package_install``
step first if that is not guaranteed).

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
