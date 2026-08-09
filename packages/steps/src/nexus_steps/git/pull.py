"""Pull latest changes in an existing git repository.

Typically used after a git_clone step to bring a previously-cloned repo
up to date.  Publishes the new HEAD SHA and whether any files changed.

Where this fits
---------------
Registered as ``"git_pull"``. Complements ``git_clone``: because
``git_clone`` refuses to write into an existing directory, a re-run of a
long-lived checkout is expressed as clone-once + pull-thereafter. The
``updated`` output (SHA changed?) is designed to feed a downstream
``jump``/conditional so a job can skip an expensive rebuild when nothing moved.

Execution model
---------------
Fully synchronous — all work happens in ``startup()``, ``check()`` only reports
the stored outcome, and ``cancel()`` is a no-op.

AI Note: ``credential_name`` is accepted and schema-validated but never read in
this module. Private-repo authentication must already be configured on the node
(SSH agent, git credential helper, or a tokenised remote URL).
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
    """Parameters for the git_pull step.

    The ``description``/``examples`` text is user-facing — ``to_schema()``
    publishes it to ``/api/steps`` for the frontend step palette.
    """

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
    """Pull latest changes in an existing git repository.

    Requires the ``git`` binary on the node's PATH and an existing checkout at
    ``repo_dir``.

    AI Note: ``repo_dir`` is a plain ``RequiredRule`` (the base-class default),
    NOT a ``ContextSatisfiableRule`` keyed on ``clone_path``. So even though a
    preceding ``git_clone`` publishes ``clone_path`` and ``ctx.resolve()`` would
    happily merge it at runtime, submit-time validation still demands an
    explicit ``repo_dir``. Compare ``gem5_collect_results``, which does wire up
    a ContextSatisfiableRule for exactly this pattern.
    """

    PARAMS_SCHEMA = GitPullParams
    OUTPUT_KEYS = ["commit_sha", "updated"]
    DESCRIPTION = "Pull latest changes from a remote into a local repository."

    # ── Lifecycle ──

    def startup(self, params: dict[str, Any], ctx: StepContext) -> dict[str, Any]:
        """Record HEAD, run ``git pull``, then record HEAD again to detect change.

        Args:
            params: Raw step params.
            ctx: Job context; ``ctx.resolve()`` layers upstream step outputs
                beneath these params.

        Returns:
            On success ``{"commit_sha", "updated", "done": True}`` where
            ``updated`` is ``True`` iff HEAD moved; on failure a dict with an
            ``error`` key and no ``done``.

        Side effects:
            Spawns ``git`` up to three times and mutates the working tree at
            ``repo_dir`` (a pull can fast-forward, merge, or leave conflict
            markers behind on failure — nothing is rolled back here).

        Raises:
            pydantic.ValidationError: if ``repo_dir`` is missing.
            FileNotFoundError: if ``git`` is not installed (not caught, so it
                propagates to the executor as StepFailed).

        AI Note: the pre-pull ``rev-parse`` doubles as the "is this actually a
        repo?" probe — that is why its failure returns "Not a git repository"
        rather than a SHA-specific message. It has no timeout, so a hung git
        (e.g. a stale index.lock) blocks here indefinitely; only the pull
        itself is bounded, at a hard non-configurable 300 s.

        AI Note: ``updated`` compares SHAs, so it reports whether HEAD moved,
        not whether the working tree changed. A pull that only updates other
        branches, or one that merges into an unchanged HEAD, reports False.
        """
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
        # Argv list (no shell) — remote/branch names are passed verbatim.
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
        #
        # AI Note: degrades to the sentinel "unknown" instead of failing — the
        # pull already succeeded, so this is metadata loss rather than a job
        # failure. Note the knock-on: "unknown" != pre_sha, so ``updated`` is
        # reported as True in that case even if nothing actually changed.
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
        """Report the result already computed by ``startup()``. Idempotent.

        Args:
            state: The ``startup()`` state dict.

        Returns:
            ``FAILED`` when ``error`` is present, ``SUCCESS`` when ``done`` is
            set, else ``RUNNING``.

        AI Note: the trailing ``RUNNING`` is unreachable today (``startup()``
        always sets one of the two keys); it is a safety default so a
        partially-restored state keeps polling rather than reporting success.
        """
        if "error" in state:
            return StepResult.FAILED
        if state.get("done"):
            return StepResult.SUCCESS
        return StepResult.RUNNING

    def cancel(self, state: dict[str, Any]) -> None:
        """No-op — the pull already finished (or failed) inside ``startup()``.

        Args:
            state: Unused; accepted to satisfy the FlowStep interface.

        AI Note: a cancel arriving mid-pull cannot stop the ``git`` process
        from here; only the executor's task cancellation tears it down.
        """
        pass
