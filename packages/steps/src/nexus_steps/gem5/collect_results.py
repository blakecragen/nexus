"""Collect gem5 m5out results and upload them as a Nexus artifact.

Typically chained after ``gem5_run_simulation``.  The ``m5out_path``
parameter is context-satisfiable from the upstream simulation step.
LARGE_OUTPUT is set because m5out directories can contain multi-GB traces.
"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import (
    ContextSatisfiableRule,
    FlowStep,
    InputRule,
    StepContext,
)
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class CollectResultsParams(BaseModel):
    """Parameters for the gem5_collect_results step."""

    m5out_path: str | None = Field(
        None,
        description=(
            "Path to the m5out directory. Auto-resolved from upstream "
            "gem5_run_simulation context if omitted."
        ),
    )
    working_dir: str | None = Field(
        None,
        description="Working directory. Defaults to the parent of m5out_path.",
    )
    storage_target: str = Field(
        "default",
        description=(
            "Named storage backend for the artifact (e.g., 'default', 's3', "
            "'local'). Resolved by the storage manager at upload time."
        ),
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("gem5_collect_results")
class CollectResultsStep(FlowStep):
    """Package and upload gem5 m5out results as a Nexus artifact."""

    PARAMS_SCHEMA = CollectResultsParams
    OUTPUT_KEYS = ["results_artifact_id"]
    DESCRIPTION = "Collect gem5 m5out results and store them as an artifact."

    SUPPORTED_OS = ["macos", "linux"]
    LARGE_OUTPUT = True

    # ── Validation ──

    @classmethod
    def input_rules(cls) -> list[InputRule]:
        return [
            ContextSatisfiableRule(
                "m5out_path",
                context_key="m5out_path",
                description="Resolved from upstream gem5_run_simulation if omitted.",
            ),
        ]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = CollectResultsParams(**resolved)

        m5out = validated.m5out_path
        if not m5out or not os.path.isdir(m5out):
            return {"error": f"m5out directory not found: {m5out}"}

        # Create a tarball of the m5out directory.
        tar_path = tempfile.NamedTemporaryFile(
            prefix="nexus_m5out_", suffix=".tar.gz", delete=False,
        ).name

        try:
            with tarfile.open(tar_path, "w:gz") as tar:
                tar.add(m5out, arcname=os.path.basename(m5out))
        except Exception as exc:
            return {"error": f"Failed to archive m5out: {exc}"}

        # Build a summary manifest.
        manifest: dict[str, Any] = {
            "m5out_path": m5out,
            "archive_path": tar_path,
            "archive_size_bytes": os.path.getsize(tar_path),
            "storage_target": validated.storage_target,
            "files": [],
        }
        for entry in os.listdir(m5out):
            full = os.path.join(m5out, entry)
            if os.path.isfile(full):
                manifest["files"].append({
                    "name": entry,
                    "size_bytes": os.path.getsize(full),
                })

        manifest_path = tar_path.replace(".tar.gz", "_manifest.json")
        with open(manifest_path, "w") as fh:
            json.dump(manifest, fh, indent=2)

        # In a full implementation the storage manager would upload the
        # tarball and return an artifact ID.  We use the archive path as
        # a placeholder.
        return {
            "results_artifact_id": tar_path,
            "manifest_path": manifest_path,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        # Collection is synchronous in startup(); nothing to cancel.
        pass
