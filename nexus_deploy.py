#!/usr/bin/env python3
"""nexus_deploy.py — register a Nexus node and deploy + launch the agent on a
remote host over SSH, so it appears online in the dashboard.

Uses paramiko (pure-socket SSH, no pseudo-terminal), so password auth works
non-interactively — including from sandboxes that block sshpass/expect.

USAGE
  ./add_node.sh user@host [password] [options]
  ./add_node.sh user@host --register-only --name lab-1
  ./add_node.sh user@host --key                 # SSH key auth instead of password

AUTH
  Password (default): 2nd positional arg, else $NEXUS_SSH_PASSWORD / $SSHPASS,
  else an interactive prompt. Use --key to authenticate with your SSH keys/agent.

OPTIONS
  --name NAME         Friendly display name in the dashboard (default: host).
  --register-only     Just mint the node (UUID + api_key); no SSH/deploy.
  --key               Use SSH key/agent auth instead of a password.
  --ws-host IP        Host the REMOTE agent dials back to (this server).
                      Default: auto-detected LAN IP (or $NEXUS_WS_HOST).
  --ws-port PORT      Default: 8000.
  --api URL           Nexus API base for registration. Default: http://localhost:8000.
  --remote-dir DIR    Install dir on the remote (default: nexus-agent-deploy, in $HOME).
  --remote-python BIN Force a remote Python interpreter (must be >=3.11).
  --install-python    If no Python >=3.11 is found, `brew install python@3.12`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ── pretty output ───────────────────────────────────────────────────────────
_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
def info(m): print(_c("34", "==>"), m)
def ok(m):   print(_c("32", " ok"), m)
def warn(m): print(_c("33", "warn"), m, file=sys.stderr)
def die(m):  print(_c("31", "err"), m, file=sys.stderr); sys.exit(1)

# ── HTTP helpers (stdlib) ───────────────────────────────────────────────────
def _req(method, url, token=None, body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, None

def api_login(api, user, pw):
    st, body = _req("POST", f"{api}/api/auth/login", body={"username": user, "password": pw})
    if st != 200 or not body:
        die(f"Login failed (HTTP {st}). Is the API up at {api}? Are admin creds right?")
    return body["access_token"]

def api_register(api, token, hostname, name):
    body = {
        "hostname": hostname, "display_name": name, "os_type": "linux",
        "os_version": "unknown", "arch": "unknown", "cpu_model": "pending",
        "cpu_cores": 1, "ram_mb": 1024, "agent_version": "0.1.0",
        "ip_address": "0.0.0.0", "capabilities": {}, "tags": [],
    }
    st, b = _req("POST", f"{api}/api/nodes", token=token, body=body)
    if st != 201 or not b:
        die(f"Registration failed (HTTP {st}) — admin role required.")
    return b["node"]["id"], b["api_key"]

def api_status(api, token, node_id):
    st, b = _req("GET", f"{api}/api/nodes/{node_id}", token=token)
    return b.get("status") if (st == 200 and b) else f"http {st}"

# ── remote-python resolution (run on remote) ────────────────────────────────
RESOLVE_PY = r'''
for p in python3.13 python3.12 python3.11 \
         /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
         /usr/local/bin/python3.13 /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
  if command -v "$p" >/dev/null 2>&1; then
    v=$("$p" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)
    case "$v" in 3.1[1-9]|3.[2-9]*) echo "$p"; exit 0;; esac
  fi
done
exit 1
'''

INSTALL_SH = r'''#!/bin/bash
set -e
PY="$1"; RD="$2"; WS="$3"; KEY="$4"; NID="$5"
mkdir -p "$RD"
tar -xzf /tmp/nexus-agent-deploy.tgz -C "$RD"
cd "$RD"
"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q -e packages/common
./.venv/bin/python -m pip install -q -e packages/steps
./.venv/bin/python -m pip install -q -e packages/agent
if [ -f agent.pid ] && kill -0 "$(cat agent.pid)" 2>/dev/null; then kill "$(cat agent.pid)" 2>/dev/null || true; sleep 1; fi
nohup ./.venv/bin/nexus-agent run --server "$WS" --api-key "$KEY" --node-id "$NID" </dev/null >agent.log 2>&1 &
echo $! > agent.pid
sleep 3
if kill -0 "$(cat agent.pid)" 2>/dev/null; then
  echo "AGENT_RUNNING $(cat agent.pid)"; tail -n 6 agent.log 2>/dev/null || true
else
  echo "AGENT_DIED"; tail -n 25 agent.log 2>/dev/null || true; exit 5
fi
'''

def main():
    ap = argparse.ArgumentParser(add_help=True, description="Register + deploy a Nexus node.")
    ap.add_argument("target", help="user@host")
    ap.add_argument("password", nargs="?", default=None, help="SSH password (optional)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--register-only", action="store_true")
    ap.add_argument("--key", action="store_true", help="use SSH key auth")
    ap.add_argument("--ws-host", default=os.environ.get("NEXUS_WS_HOST"))
    ap.add_argument("--ws-port", default=os.environ.get("NEXUS_WS_PORT", "8000"))
    ap.add_argument("--api", default=os.environ.get("NEXUS_API", "http://localhost:8000"))
    ap.add_argument("--remote-dir", default="nexus-agent-deploy")
    ap.add_argument("--remote-python", default=None)
    ap.add_argument("--install-python", action="store_true")
    ap.add_argument("--admin-user", default=os.environ.get("NEXUS_ADMIN_USER", "admin"))
    ap.add_argument("--admin-pass", default=os.environ.get("NEXUS_ADMIN_PASSWORD", "admin"))
    args = ap.parse_args()

    if "@" not in args.target:
        die("Target must be user@host.")
    user, host = args.target.split("@", 1)
    name = args.name or host
    repo = os.path.dirname(os.path.abspath(__file__))
    args.ws_host = args.ws_host or _default_ws_host()

    # 1. Authenticate + register (mints UUID + api_key)
    info(f"Logging in to {args.api} as '{args.admin_user}'")
    token = api_login(args.api, args.admin_user, args.admin_pass)
    ok("Authenticated.")
    info(f"Registering node '{name}'")
    node_id, api_key = api_register(args.api, token, host, name)
    ws_url = f"ws://{args.ws_host}:{args.ws_port}/ws/agent/{node_id}"
    ok("Registered node.")
    print(f"    NODE_ID  {node_id}")
    print(f"    API_KEY  {api_key}")
    print(f"    WS_URL   {ws_url}")

    if args.register_only:
        print(f"\nRun the agent on the target to bring it online:\n"
              f"  nexus-agent run --server {ws_url} --api-key {api_key} --node-id {node_id}\n"
              f"Remove: curl -X DELETE {args.api}/api/nodes/{node_id} -H 'Authorization: Bearer <token>'")
        return

    # 2. Connect (paramiko — no pty)
    try:
        import paramiko
    except ImportError:
        die("paramiko not installed for this python. Install: pip3 install paramiko")

    pw = None
    if not args.key:
        pw = args.password or os.environ.get("NEXUS_SSH_PASSWORD") or os.environ.get("SSHPASS")
        if not pw:
            if sys.stdin.isatty():
                pw = getpass.getpass(f"SSH password for {args.target}: ")
            else:
                die("No password given. Pass it as the 2nd arg, set $NEXUS_SSH_PASSWORD, or use --key.")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    info(f"Connecting to {args.target} ({'key' if args.key else 'password'} auth)")
    try:
        if args.key:
            client.connect(host, username=user, timeout=20)
        else:
            client.connect(host, username=user, password=pw, timeout=20,
                           look_for_keys=False, allow_agent=False)
    except Exception as e:
        _cleanup(args.api, token, node_id)
        die(f"SSH connection failed: {type(e).__name__}: {e}")
    ok("Connected.")

    def run(cmd, timeout=600):
        _in, out, err = client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode(), err.read().decode()

    try:
        # 3. Reachability back to the server
        rc, o, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 8 "
                       f"http://{args.ws_host}:{args.ws_port}/api/nodes || echo 000")
        code = (o or "").strip()
        if code in ("", "000"):
            warn(f"Remote cannot reach http://{args.ws_host}:{args.ws_port} — agent won't connect. "
                 f"Check --ws-host / firewall.")
        else:
            ok(f"Remote reached the server (HTTP {code} — auth-protected, expected).")

        # 4. Resolve a remote Python >=3.11
        py = args.remote_python
        if not py:
            rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
            py = o.strip() if rc == 0 else ""
        if not py:
            if args.install_python:
                info("No Python >=3.11 found — installing python@3.12 via Homebrew (a few minutes)…")
                brew = _first_path(run, ["/opt/homebrew/bin/brew", "/usr/local/bin/brew", "brew"])
                if not brew:
                    _cleanup(args.api, token, node_id); die("Homebrew not found on remote.")
                rc, o, e = run(f"{brew} install python@3.12", timeout=1800)
                if rc != 0:
                    _cleanup(args.api, token, node_id); die(f"brew install failed:\n{e or o}")
                rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
                py = o.strip() if rc == 0 else ""
            if not py:
                _cleanup(args.api, token, node_id)
                die("No Python >=3.11 on remote. Re-run with --install-python (uses Homebrew), "
                    "or pass --remote-python /path/to/python3.x.")
        rc, o, _ = run(f"{py} --version")
        ok(f"Remote Python: {o.strip()}  ({py})")

        # 5. Bundle + upload packages
        info("Bundling + uploading agent packages")
        tar_path = os.path.join(tempfile.gettempdir(), "nexus-agent-deploy.tgz")
        subprocess.run(["tar", "-czf", tar_path, "-C", repo,
                        "packages/common", "packages/steps", "packages/agent"], check=True)
        sftp = client.open_sftp()
        sftp.put(tar_path, "/tmp/nexus-agent-deploy.tgz")
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(INSTALL_SH); local_sh = f.name
        sftp.put(local_sh, "/tmp/nexus-install.sh")
        sftp.close()
        ok("Uploaded.")

        # 6. Install + launch
        info("Installing into a venv + launching agent (this can take a minute)")
        rc, o, e = run(f"bash /tmp/nexus-install.sh {_q(py)} {_q(args.remote_dir)} "
                       f"{_q(ws_url)} {_q(api_key)} {_q(node_id)}", timeout=900)
        print("    " + "\n    ".join((o or "").strip().splitlines()))
        if rc != 0:
            warn((e or "").strip())
            _cleanup(args.api, token, node_id)
            die("Remote install/launch failed (node deregistered).")
        ok("Agent launched on remote.")
    finally:
        client.close()

    # 7. Wait for online
    info("Waiting for node to report online…")
    status = "unknown"
    for _ in range(15):
        status = api_status(args.api, token, node_id)
        if status == "online":
            break
        time.sleep(2)
    print()
    if status == "online":
        ok(f"Node '{name}' is ONLINE. View it at http://localhost:3000 (Nodes).")
    else:
        warn(f"Node status is '{status}' (not online yet). Check the remote log:")
        warn(f"  ssh {args.target} 'tail -f {args.remote_dir}/agent.log'")
    print(f"\nManage:\n"
          f"  Logs:   ssh {args.target} 'tail -f {args.remote_dir}/agent.log'\n"
          f"  Stop:   ssh {args.target} 'kill $(cat {args.remote_dir}/agent.pid)'\n"
          f"  Remove: curl -X DELETE {args.api}/api/nodes/{node_id} -H 'Authorization: Bearer <token>'")

def _q(s):
    """Single-quote a string for safe shell embedding."""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"

def _default_ws_host():
    """Best-effort primary LAN IP the remote agent can dial back to."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # no traffic sent; picks the egress interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"

def _first_path(run, candidates):
    for c in candidates:
        rc, _, _ = run(f"command -v {_q(c)}")
        if rc == 0:
            return c
    return None

def _cleanup(api, token, node_id):
    """Deregister a node we created but couldn't bring up, to avoid orphans."""
    try:
        _req("DELETE", f"{api}/api/nodes/{node_id}", token=token)
    except Exception:
        pass

if __name__ == "__main__":
    main()
