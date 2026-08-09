"""Shell execution steps (``run_command``, ``run_script``).

These are the workhorse steps of the cluster. Both spawn a detached
subprocess in ``startup()`` and report progress through repeated ``check()``
polls driven by the agent's :class:`~nexus_agent.executor.StepExecutor`.

Neither returns a ``command`` key in its state, so the executor takes its
poll-based branch (``_poll_step``) rather than its live output-streaming
branch (``_run_subprocess``). Consequently stdout/stderr are written to temp
files whose paths are published in the step state; ``StepExecutor._capture``
reads those files back afterwards to build the per-job terminal log.

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
