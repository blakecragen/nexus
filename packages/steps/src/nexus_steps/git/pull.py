"""Pull latest changes in an existing git repository.

Typically used after a git_clone step to bring a previously-cloned repo
up to date.  Publishes the new HEAD SHA and whether any files changed.
"""

from __future__ import annotations

import subprocess
from typing import Any

from pydantic import BaseModel, Field

from nexus_common.models.enums import StepResult
from nexus_common.steps.base import FlowStep, StepContext
from nexus_common.steps.registry import register


# ── Params ───────────────────────────────────────────────────────────────


class GitPullParams(BaseModel):
    """Parameters for the git_pull step."""

    repo_dir: str = Field(
        ...,
        description="Path to the local git repository.",
        examples=["/tmp/nexus_clone_myrepo"],
    )
    remote: str = Field(
        "origin",
        description="Git remote name.",
    )
    branch: str | None = Field(
        None,
        description="Branch to pull. Defaults to the currently checked-out branch.",
    )
    credential_name: str | None = Field(
        None,
        description="Name of a Nexus vault credential for private repositories.",
    )


# ── Step ─────────────────────────────────────────────────────────────────


@register("git_pull")
class GitPullStep(FlowStep):
    """Pull latest changes in an existing git repository."""

    PARAMS_SCHEMA = GitPullParams
    OUTPUT_KEYS = ["commit_sha", "updated"]
    DESCRIPTION = "Pull latest changes from a remote into a local repository."
    REQUIRED_CAPABILITIES = ["git"]

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        resolved = ctx.resolve(params)
        validated = GitPullParams(**resolved)

        repo = validated.repo_dir

        # Capture pre-pull SHA.
        try:
            pre_sha = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            return {"error": f"Not a git repository: {repo}"}

        # Build pull command.
        cmd = ["git", "-C", repo, "pull", validated.remote]
        if validated.branch:
            cmd.append(validated.branch)

        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=300)
        except subprocess.CalledProcessError as exc:
            return {"error": f"git pull failed: {exc.stderr.strip()}"}
        except subprocess.TimeoutExpired:
            return {"error": "git pull timed out after 300s"}

        # Post-pull SHA.
        try:
            post_sha = subprocess.run(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except subprocess.CalledProcessError:
            post_sha = "unknown"

        return {
            "commit_sha": post_sha,
            "updated": pre_sha != post_sha,
            "done": True,
        }

    def check(self, state: dict[str, Any]) -> StepResult:
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        pass
