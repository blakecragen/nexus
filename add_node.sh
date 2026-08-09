#!/usr/bin/env bash
#
# add_node.sh — register a Nexus node and deploy + launch the agent on a remote
# host, so it appears online in the dashboard (http://localhost:3000).
#
# Just needs the SSH target and a password:
#
#   ./add_node.sh user@host mypassword
#   ./add_node.sh user@host                      # prompts for password
#   ./add_node.sh user@host --register-only --name lab-1
#   ./add_node.sh user@host --key                # SSH key auth instead
#
# Password can also come from $NEXUS_SSH_PASSWORD or $SSHPASS instead of the CLI.
# This is a thin wrapper around nexus_deploy.py, which uses paramiko (pure-socket
# SSH, no pseudo-terminal) so password auth works non-interactively. Requires
# paramiko for the python3 on PATH:  pip3 install paramiko
#
# Role: this script contains NO logic of its own. Its only job is interpreter
# selection — every flag is forwarded verbatim to nexus_deploy.py, which is the
# real implementation (register the node via the API, then clone + install +
# start the agent on the device over SSH).
#
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a python3 that actually has paramiko (system python often does; the
# project .venv usually doesn't).
#
# AI Note: this loop exists because of an easy-to-hit trap — if the project
# .venv is active, `python3` resolves to it, and the venv installs only the
# server/agent packages (paramiko is not among them). Probing with an actual
# `import paramiko` rather than checking version/paths is what makes the choice
# reliable regardless of which environment happens to be active.
#
# AI Note: `exec` replaces this shell with python, so the deploy script's exit
# code and signal handling (Ctrl-C during a long install) pass straight through
# to the caller. "$@" is quoted so arguments containing spaces survive intact.
for PY in python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c 'import paramiko' >/dev/null 2>&1; then
    exec "$PY" "$DIR/nexus_deploy.py" "$@"
  fi
done

echo "error: no python3 with paramiko found. Install it:  pip3 install paramiko" >&2
exit 1
