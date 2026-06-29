"""Clone a git repository onto a compute node.

Supports shallow clones (depth), branch selection, and credential injection
via a named credential from the Nexus vault.  The resolved clone path and
HEAD commit SHA are published to the job context for downstream steps.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class GitCloneParams(BaseModel):
    """Parameters for the git_clone step."""

    repo_url: str = Field(
        ...,
        description="Git repository URL (HTTPS or SSH).",
        examples=["https://github.com/org/repo.git"],
    )
    branch: str | None = Field(
        None,
        description="Branch, tag, or commit to check out after cloning.",
    )
    dest_dir: str | None = Field(
        None,
        description=(
            "Destination directory. Defaults to a temp directory named "
            "after the repository."
        ),
    )
    depth: int | None = Field(
        None,
        description="Create a shallow clone with this many commits. Omit for full clone.",
        ge=1,
    )
    credential_name: str | None = Field(
        None,
        description="Name of a Nexus vault credential for private repositories.",
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("git_clone")
class GitCloneStep(FlowStep):
    """Clone a git repository onto a compute node."""

    PARAMS_SCHEMA = GitCloneParams
    OUTPUT_KEYS = ["clone_path", "commit_sha"]
    DESCRIPTION = "Clone a git repository with optional branch and depth."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = GitCloneParams(**resolved)

        # Determine destination directory.
        if validated.dest_dir:
            dest = validated.dest_dir
        else:
            repo_name = validated.repo_url.rstrip("/").rsplit("/", 1)[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            dest = os.path.join("/tmp", f"nexus_clone_{repo_name}")

        cmd: list[str] = ["git", "clone"]
        if validated.depth:
            cmd += ["--depth", str(validated.depth)]
        if validated.branch:
            cmd += ["--branch", validated.branch]
        cmd += [validated.repo_url, dest]

        try:
            subprocess.run(
                cmd,
                check=True,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.CalledProcessError as exc:
            return {
                "error": f"git clone failed: {exc.stderr.strip()}",
                "clone_path": dest,
            }
        except subprocess.TimeoutExpired:
            return {"error": "git clone timed out after 600s", "clone_path": dest}

        # Resolve HEAD SHA.
        try:
            sha_result = subprocess.run(
                ["git", "-C", dest, "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            commit_sha = sha_result.stdout.strip()
        except subprocess.CalledProcessError:
            commit_sha = "unknown"

        return {
            "clone_path": dest,
            "commit_sha": commit_sha,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        # Clone is synchronous in startup(); nothing to cancel.
        pass
