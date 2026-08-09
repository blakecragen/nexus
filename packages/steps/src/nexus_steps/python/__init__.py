"""Built-in Python step implementations.

Holds :mod:`nexus_steps.python.run` (the ``run_python`` step), which executes
either inline Python source or a ``.py`` file already present on the node.
It behaves like the shell steps — detached subprocess in ``startup()``,
poll-based ``check()``, stdout/stderr to temp files — with the extra
convenience of an OS-selected interpreter and a caller-supplied environment
overlay.

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
