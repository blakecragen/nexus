"""Step schema routes — list and detail for registered FlowStep types."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from nexus_common.models.schemas import StepSchemaInfo
from nexus_common.steps.registry import STEP_REGISTRY, list_steps
from nexus_server.api.deps import CurrentUser

router = APIRouter()


@router.get("", response_model=list[StepSchemaInfo])
async def get_all_steps(user: CurrentUser):
    """List all registered step schemas."""
    result = []
    for name in list_steps():
        step_cls = STEP_REGISTRY[name]
        schema = step_cls.to_schema()
        result.append(StepSchemaInfo(**schema))
    return result


@router.get("/{step_name}", response_model=StepSchemaInfo)
async def get_step_detail(step_name: str, user: CurrentUser):
    """Get detailed schema and docs for a single step type."""
    if step_name not in STEP_REGISTRY:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Step '{step_name}' not found. Available: {list_steps()}",
        )
    step_cls = STEP_REGISTRY[step_name]
    schema = step_cls.to_schema()
    return StepSchemaInfo(**schema)
