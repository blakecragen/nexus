"""Reserved namespace for file-transfer / filesystem steps.

Intentionally empty: no file steps ship yet. File movement is currently
handled out-of-band — the ``run_script`` step assumes the script was already
placed on the node, and ``gem5_collect_results`` uploads artifacts directly
to the server over HTTP.

If steps are added here they must also be appended to ``_STEP_MODULES`` in
:mod:`nexus_steps`; otherwise their ``@register`` decorators never run and
the steps stay invisible to the server, the API schema endpoint, and the
agent's ``get_step()`` lookup.
"""
