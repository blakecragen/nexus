"""Step schema routes — list and detail for registered FlowStep types.

Role in the system
------------------
Mounted at ``/api/steps``. This is a read-only, introspection-only router: it
exposes the in-process ``STEP_REGISTRY`` so the UI can render a step palette
and build parameter forms without hard-coding any step's fields.

Where the data comes from
-------------------------
``nexus_common.steps.registry.STEP_REGISTRY`` is a plain module-level dict
populated at import time by the ``@register("name")`` decorator on each
``FlowStep`` subclass. ``nexus_server.main`` imports ``nexus_steps`` purely for
that side effect. Each entry's ``to_schema()`` classmethod reflects over the
step's ``PARAMS_SCHEMA`` Pydantic model plus its ``DESCRIPTION``,
``REQUIRES_NODE``, ``SUPPORTED_OS``, ``OUTPUT_KEYS``, ``input_rules()`` and
``OS_VARIANTS`` to produce a JSON-serializable :class:`StepSchemaInfo`.

Neighbouring modules
--------------------
- ``nexus_common.steps.base.FlowStep.to_schema`` does all the real work.
- ``frontend/src/pages/JobBuilder.tsx`` consumes both endpoints to drive the
  dynamic form; ``useStepsStore`` caches the list.
- The same registry is used at job-submission time by ``routes/jobs.py`` for
  parameter validation, and by ``runner/scheduler.py`` for OS matching — so
  what the UI shows here and what the server enforces cannot drift.

AI Note: there is no DB access anywhere in this file, and no admin gate — step
schemas are considered public-to-authenticated-users metadata. Also note the
registry is process-local: a step class that fails to import (bad dependency,
syntax error in a plugin module) simply never appears in these responses, with
no error surfaced here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import StepSchemaInfo
from nexus_common.steps.registry import STEP_REGISTRY, list_steps
from nexus_server.api.deps import CurrentUser

router = APIRouter()


@router.get("", response_model=list[StepSchemaInfo])
async def get_all_steps(user: CurrentUser):
    """List all registered step schemas.

    Reflects over every class in ``STEP_REGISTRY`` and returns its exported
    schema. Purely in-memory; no DB or network I/O.

    Args:
        user: Any authenticated user (the dependency exists only to require a
            valid token — the value is unused).

    Returns:
        list[StepSchemaInfo]: One entry per registered step, ordered by step
        name because ``list_steps()`` returns a sorted list. The UI relies on
        that stable ordering for the step palette.
    """
    result = []
    # AI Note: iterates list_steps() and then indexes STEP_REGISTRY directly
    # rather than using get_step(). Safe only because both read the same dict
    # and nothing mutates the registry after import — a step registered lazily
    # at runtime could theoretically race here.
    for name in list_steps():
        step_cls = STEP_REGISTRY[name]
        schema = step_cls.to_schema()
        result.append(StepSchemaInfo(**schema))
    return result


@router.get("/{step_name}", response_model=StepSchemaInfo)
async def get_step_detail(step_name: str, user: CurrentUser):
    """Get detailed schema and docs for a single step type.

    Args:
        step_name: The registry key (the string passed to ``@register``), not
            the Python class name. e.g. ``"run_command"``.
        user: Any authenticated user (value unused).

    Returns:
        StepSchemaInfo: Fields, input rules, OS support, output keys and
        OS-specific variants for the step.

    Raises:
        HTTPException: 404 if ``step_name`` is not registered. The detail
            enumerates every available step name, which is intentional — it
            makes typos in a job definition immediately self-diagnosing.
    """
    # AI Note: the 404 detail embeds list_steps(), so this response grows with
    # the number of registered steps. Harmless today, but do not treat that
    # string as a stable machine-parsable contract.
    if step_name not in STEP_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Step '{step_name}' not found. Available: {list_steps()}",
        )
    step_cls = STEP_REGISTRY[step_name]
    schema = step_cls.to_schema()
    return StepSchemaInfo(**schema)
