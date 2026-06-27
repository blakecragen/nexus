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
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a python3 that actually has paramiko (system python often does; the
# project .venv usually doesn't).
for PY in python3 /usr/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$PY" >/dev/null 2>&1 && "$PY" -c 'import paramiko' >/dev/null 2>&1; then
    exec "$PY" "$DIR/nexus_deploy.py" "$@"
  fi
done

echo "error: no python3 with paramiko found. Install it:  pip3 install paramiko" >&2
exit 1
