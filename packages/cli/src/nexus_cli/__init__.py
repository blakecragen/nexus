"""Nexus CLI — command-line client for the Nexus cluster.

Package marker for the ``nexus-cli`` distribution. Intentionally holds no code:
it declares the package so ``nexus_cli.*`` modules are importable, and importing
it must stay free of side effects (no network, no config loading, no argument
parsing) because it is pulled in before any command has been selected.

Where the actual CLI lives:
    ``packages/cli/pyproject.toml`` declares the console script
    ``nexus = "nexus_cli.main:app"`` — a Typer application. The command
    implementations, HTTP client (httpx against the FastAPI server), and Rich
    output formatting belong in ``nexus_cli.main`` and its siblings, not here.

Relationship to the rest of the system:
    The CLI is a pure HTTP client. It depends on ``nexus-common`` for the request/
    response schemas (``JobSubmit``, ``JobInfo``, ...) and for
    ``nexus_common.parser.parse_nexus_string``, which turns a ``.nexus`` file into
    a job payload. The parser returns private ``_pool_name`` / ``_node_id`` keys
    that the CLI is responsible for resolving to UUIDs before submitting. It has
    no dependency on ``nexus-server`` or ``nexus-agent``.

AI Note: The ``nexus_cli.main`` module referenced by the console-script entry
point does not exist in this tree — this package currently contains only this
file. Installing ``nexus-cli`` therefore yields a ``nexus`` command that fails to
import at launch. Treat the CLI as scaffolded but unimplemented.
"""
