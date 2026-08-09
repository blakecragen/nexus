"""Clone a git repository onto a compute node.

Supports shallow clones (depth), branch selection, and credential injection
via a named credential from the Nexus vault.  The resolved clone path and
HEAD commit SHA are published to the job context for downstream steps.

Where this fits
---------------
Registered as ``"git_clone"``. Typically the first node-side step of a build
or simulation job: it publishes ``clone_path`` and ``commit_sha`` into
``StepContext.outputs``, where later steps pick them up automatically through
``ctx.resolve()`` (``run_command``'s ``working_dir``, ``git_pull``'s
``repo_dir``, and so on).

Execution model
---------------
Unlike the shell/python steps, this one is **fully synchronous**: all the work
happens inside ``startup()`` and ``check()`` merely reports the stored result.
That makes the step un-cancellable and means a slow clone blocks the agent's
step coroutine for up to the hard 600 s timeout below.

AI Note: ``credential_name`` is accepted and schema-validated but is NOT read
anywhere in this module — private-repo auth is not wired up yet. Cloning a
private HTTPS repo will hang or fail on a credential prompt unless the node's
git is already configured (SSH agent, credential helper, or a token baked into
``repo_url``). Treat the field as reserved-for-future-use, not as working
functionality.
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
    """Parameters for the git_clone step.

    The ``description``/``examples`` text is user-facing — ``to_schema()``
    publishes it to ``/api/steps`` for the frontend step palette.
    """

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
    """Clone a git repository onto a compute node.

    Requires the ``git`` binary to be on the node's PATH; chain a
    ``package_install`` step first if that is not guaranteed.
    """

    PARAMS_SCHEMA = GitCloneParams
    OUTPUT_KEYS = ["clone_path", "commit_sha"]
    DESCRIPTION = "Clone a git repository with optional branch and depth."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Run ``git clone`` to completion and resolve the checked-out HEAD.

        Args:
            params: Raw step params.
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            On success ``{"clone_path", "commit_sha", "done": True}``; on
            failure a dict with ``error`` (and ``clone_path`` so the partially
            written directory is still visible to an operator).

        Side effects:
            Spawns ``git`` twice (clone, then ``rev-parse``) and writes a
            possibly large tree to ``dest_dir``. Nothing is cleaned up on
            failure — a half-written destination directory is left in place.

        Raises:
            pydantic.ValidationError: if ``repo_url`` is missing or a field
                fails validation.
            FileNotFoundError: if the ``git`` binary is absent (this is NOT
                caught here, so it propagates to the executor as StepFailed).

        AI Note: this is a blocking call inside ``startup()`` with a hard 600 s
        (10 min) timeout on the clone. Large repositories must use ``depth`` to
        stay inside that budget; the timeout is not configurable via params.

        AI Note: the destination is *not* cleaned before cloning, so re-running
        a job that used an explicit ``dest_dir`` (or the derived
        ``/tmp/nexus_clone_<repo>`` path) fails with git's "destination path
        already exists" rather than refreshing the checkout. Use ``git_pull``
        for the refresh case.
        """
        resolved = ctx.resolve(params)
        validated = GitCloneParams(**resolved)

        # Determine destination directory.
        #
        # AI Note: the derived default is a *stable* /tmp path keyed on the
        # repo name, not a unique mkdtemp. That makes the clone path
        # predictable for hand-written follow-up steps, at the cost of
        # colliding when two concurrent jobs on the same node clone the same
        # repository. Hardcoded "/tmp" also means this branch is POSIX-only.
        if validated.dest_dir:
            dest = validated.dest_dir
        else:
            repo_name = validated.repo_url.rstrip("/").rsplit("/", 1)[-1]
            if repo_name.endswith(".git"):
                repo_name = repo_name[:-4]
            dest = os.path.join("/tmp", f"nexus_clone_{repo_name}")

        # Argv list (no shell), so repo_url/branch are never word-split or
        # interpreted as shell metacharacters.
        cmd: list[str] = ["git", "clone"]
        if validated.depth:
            cmd += ["--depth", str(validated.depth)]
        if validated.branch:
            # AI Note: --branch accepts a tag as well as a branch, but NOT a
            # raw commit SHA, despite what the field description implies —
            # git refuses "--branch <sha>" on a clone.
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
        #
        # AI Note: a failure here degrades to the sentinel string "unknown"
        # instead of failing the step — the clone itself already succeeded, so
        # losing the SHA is metadata loss, not a job failure. Downstream steps
        # consuming ``commit_sha`` must tolerate "unknown". Note this second
        # call has no timeout, unlike the clone above.
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
        """Report the result already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when ``error`` is present, ``SUCCESS`` when ``done`` is
            set, else ``RUNNING``.

        AI Note: the trailing ``RUNNING`` is unreachable in normal operation —
        ``startup()`` always returns a state with either ``error`` or ``done``.
        It exists so that a state restored from the DB mid-write, or a future
        async rewrite of this step, degrades to "keep polling" instead of
        reporting a bogus success.
        """
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the clone already finished (or failed) inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: because the work is synchronous, a cancel arriving mid-clone
        cannot stop the ``git`` process from here. The executor's own SIGTERM/
        SIGKILL escalation of the step task is the only way it gets torn down.
        """
        # Clone is synchronous in startup(); nothing to cancel.
        pass
