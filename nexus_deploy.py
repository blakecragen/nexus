#!/usr/bin/env python3
"""nexus_deploy.py — register a Nexus node and set it up entirely over SSH.

The device CLONES the agent code from GitHub, installs it into a venv, saves its
config, and starts the agent. Background by default; --service installs an
auto-start service (launchd on macOS, systemd --user on Linux).

Uses paramiko (pure-socket SSH, no pseudo-terminal), so password auth works
non-interactively — including from sandboxes that block sshpass/expect.

USAGE
  ./add_node.sh user@host [password] [options]
  ./add_node.sh user@host --register-only --name lab-1
  ./add_node.sh user@host --service             # auto-start on boot
  ./add_node.sh user@host --key                 # SSH key auth instead of password

AUTH
  Password (default): 2nd positional arg, else $NEXUS_SSH_PASSWORD / $SSHPASS,
  else an interactive prompt. Use --key to authenticate with your SSH keys/agent.

OPTIONS
  --name NAME         Friendly display name in the dashboard (default: host).
  --register-only     Just mint the node (UUID + api_key); no SSH/deploy.
  --service           Install an auto-start service instead of a background process.
  --key               Use SSH key/agent auth instead of a password.
  --repo-url URL      Git repo to clone on the device.
                      Default: https://github.com/blakecragen/nexus.git (or $NEXUS_REPO_URL).
  --branch NAME       Branch to clone. Default: main (or $NEXUS_BRANCH).
  --ws-host IP        Host the REMOTE agent dials back to (this server).
                      Default: auto-detected LAN IP (or $NEXUS_WS_HOST).
  --ws-port PORT      Default: 8000.
  --api URL           Nexus API base for registration. Default: http://localhost:8000.
  --remote-dir DIR    Clone/install dir on the remote (default: nexus, in $HOME).
  --remote-python BIN Force a remote Python interpreter (must be >=3.11).
  --install-python    If no Python >=3.11 is found, `brew install python@3.12`.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
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
        "ip_address": "0.0.0.0", "tags": [],
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

# Args: PY RD REPO_URL BRANCH WS KEY NID MODE
INSTALL_SH = r'''#!/bin/bash
set -e
PY="$1"; RD="$2"; REPO_URL="$3"; BRANCH="$4"; WS="$5"; KEY="$6"; NID="$7"; MODE="$8"

command -v git >/dev/null 2>&1 || { echo "NO_GIT"; exit 3; }

# ── fetch code from GitHub (clone fresh, or update an existing checkout) ──
if [ -d "$RD/.git" ]; then
  git -C "$RD" remote set-url origin "$REPO_URL" 2>/dev/null || true
  git -C "$RD" fetch --depth 1 origin "$BRANCH"
  git -C "$RD" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  rm -rf "$RD"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$RD"
fi
cd "$RD"
RD="$(pwd)"   # make absolute, so $RD/... paths are correct after cd

# ── venv + install the agent ──
"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q -e packages/common
./.venv/bin/python -m pip install -q -e packages/steps
./.venv/bin/python -m pip install -q -e packages/agent
AGENT="$(pwd)/.venv/bin/nexus-agent"

# ── persist config (api_key stored 0600 in ~/.nexus-agent/config.json) ──
"$AGENT" init --server "$WS" --api-key "$KEY" --node-id "$NID" >/dev/null

# ── control helper, so the agent can be managed over SSH ──
cat > "$RD/nexusctl" <<'CTL'
#!/bin/bash
# Control the Nexus agent (background mode). Config is in ~/.nexus-agent/config.json
cd "$(dirname "$0")" || exit 1
AG=./.venv/bin/nexus-agent
_start(){ nohup $AG run </dev/null >agent.log 2>&1 & echo $! > agent.pid; echo "started $(cat agent.pid)"; }
_stop(){ [ -f agent.pid ] && kill "$(cat agent.pid)" 2>/dev/null && echo stopped || echo "not running"; }
_status(){ [ -f agent.pid ] && kill -0 "$(cat agent.pid)" 2>/dev/null && echo "running $(cat agent.pid)" || echo stopped; }
case "$1" in
  start)   _start;;
  stop)    _stop;;
  restart) _stop; sleep 1; _start;;
  status)  _status;;
  logs)    tail -n 40 agent.log;;
  *)       echo "usage: nexusctl {start|stop|restart|status|logs}";;
esac
CTL
chmod +x "$RD/nexusctl"

# ── stop any prior instance (background pid AND any installed service) ──
if [ -f agent.pid ] && kill -0 "$(cat agent.pid)" 2>/dev/null; then kill "$(cat agent.pid)" 2>/dev/null || true; sleep 1; fi
launchctl bootout "gui/$(id -u)/com.nexus.agent" 2>/dev/null || true
systemctl --user disable --now nexus-agent 2>/dev/null || true

if [ "$MODE" = "service" ]; then
  OS="$(uname -s)"
  if [ "$OS" = "Darwin" ]; then
    PLIST="$HOME/Library/LaunchAgents/com.nexus.agent.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.nexus.agent</string>
  <key>ProgramArguments</key><array><string>$AGENT</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$RD/agent.log</string>
  <key>StandardErrorPath</key><string>$RD/agent.log</string>
</dict></plist>
PL
    launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST" 2>/dev/null || true
    launchctl kickstart -k "gui/$(id -u)/com.nexus.agent" 2>/dev/null || true
    echo "SERVICE_INSTALLED launchd com.nexus.agent"
  elif [ "$OS" = "Linux" ]; then
    UD="$HOME/.config/systemd/user"; mkdir -p "$UD"
    cat > "$UD/nexus-agent.service" <<UNIT
[Unit]
Description=Nexus Agent
After=network-online.target
[Service]
ExecStart=$AGENT run
Restart=always
RestartSec=5
[Install]
WantedBy=default.target
UNIT
    command -v loginctl >/dev/null 2>&1 && loginctl enable-linger "$(id -un)" 2>/dev/null || true
    systemctl --user daemon-reload
    systemctl --user enable --now nexus-agent
    echo "SERVICE_INSTALLED systemd nexus-agent"
  else
    echo "SERVICE_UNSUPPORTED $OS"; exit 6
  fi
  sleep 3
else
  # ── background (default) ──
  "$RD/nexusctl" start >/dev/null
  sleep 3
  if "$RD/nexusctl" status | grep -q running; then
    echo "AGENT_RUNNING $(cat agent.pid)"; tail -n 6 agent.log 2>/dev/null || true
  else
    echo "AGENT_DIED"; tail -n 25 agent.log 2>/dev/null || true; exit 5
  fi
fi
'''

def main():
    ap = argparse.ArgumentParser(add_help=True, description="Register + set up a Nexus node over SSH.")
    ap.add_argument("target", help="user@host")
    ap.add_argument("password", nargs="?", default=None, help="SSH password (optional)")
    ap.add_argument("--name", default=None)
    ap.add_argument("--register-only", action="store_true")
    ap.add_argument("--service", action="store_true",
                    help="install an auto-start service (launchd/systemd) instead of a background process")
    ap.add_argument("--key", action="store_true", help="use SSH key auth")
    ap.add_argument("--repo-url", default=os.environ.get("NEXUS_REPO_URL", "https://github.com/blakecragen/nexus.git"))
    ap.add_argument("--branch", default=os.environ.get("NEXUS_BRANCH", "main"))
    ap.add_argument("--ws-host", default=os.environ.get("NEXUS_WS_HOST"))
    ap.add_argument("--ws-port", default=os.environ.get("NEXUS_WS_PORT", "8000"))
    ap.add_argument("--api", default=os.environ.get("NEXUS_API", "http://localhost:8000"))
    ap.add_argument("--remote-dir", default="nexus")
    ap.add_argument("--remote-python", default=None)
    ap.add_argument("--install-python", action="store_true")
    ap.add_argument("--admin-user", default=os.environ.get("NEXUS_ADMIN_USER", "admin"))
    ap.add_argument("--admin-pass", default=os.environ.get("NEXUS_ADMIN_PASSWORD", "admin"))
    args = ap.parse_args()

    if "@" not in args.target:
        die("Target must be user@host.")
    user, host = args.target.split("@", 1)
    name = args.name or host
    rd = args.remote_dir

    # ── register-only: HTTP-only path, no SSH ──
    if args.register_only:
        ws_host = args.ws_host or _default_ws_host()
        info(f"Logging in to {args.api} as '{args.admin_user}'")
        token = api_login(args.api, args.admin_user, args.admin_pass)
        ok("Authenticated.")
        info(f"Registering node '{name}'")
        node_id, api_key = api_register(args.api, token, host, name)
        ws_url = f"ws://{ws_host}:{args.ws_port}/ws/agent/{node_id}"
        ok("Registered node.")
        print(f"    NODE_ID  {node_id}")
        print(f"    API_KEY  {api_key}")
        print(f"    WS_URL   {ws_url}")
        print(f"\nRun the agent on the target to bring it online:\n"
              f"  nexus-agent run --server {ws_url} --api-key {api_key} --node-id {node_id}\n"
              f"Remove: curl -X DELETE {args.api}/api/nodes/{node_id} -H 'Authorization: Bearer <token>'")
        return

    # ── SSH-first: connect + verify the device, THEN register. If SSH fails we
    #    never created a node, so there's no orphan to clean up. ──
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
        die(f"SSH connection failed: {type(e).__name__}: {e}")
    ok("Connected.")

    def run(cmd, timeout=600):
        _in, out, err = client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode(), err.read().decode()

    mode = "service" if args.service else "background"
    token = node_id = api_key = None
    try:
        # 1. Pick the controller address the REMOTE can actually reach (auto,
        #    unless --ws-host was given). Handles multi-homed controllers.
        candidates = [args.ws_host] if args.ws_host else (_local_ipv4s() or ["localhost"])
        chosen = None
        for cand in candidates:
            rc, o, _ = run(f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 "
                           f"http://{cand}:{args.ws_port}/api/nodes")
            c = (o or "").strip()
            if c and c != "000":
                chosen = cand
                ok(f"Remote reaches the server at {cand}:{args.ws_port} (HTTP {c}).")
                break
        if not chosen:
            chosen = args.ws_host or (candidates[0] if candidates else "localhost")
            warn(f"Remote couldn't reach the server at any of {candidates} — using {chosen}. "
                 f"Agent may not connect; re-run with --ws-host <reachable-address>.")
        args.ws_host = chosen

        # 2. git present?
        rc, _, _ = run("command -v git")
        if rc != 0:
            die("git not found on remote (macOS: `xcode-select --install`; Linux: apt/yum install git).")

        # 3. Resolve a remote Python >=3.11
        py = args.remote_python
        if not py:
            rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
            py = o.strip() if rc == 0 else ""
        if not py:
            if args.install_python:
                info("No Python >=3.11 found — installing python@3.12 via Homebrew (a few minutes)…")
                brew = _first_path(run, ["/opt/homebrew/bin/brew", "/usr/local/bin/brew", "brew"])
                if not brew:
                    die("Homebrew not found on remote.")
                rc, o, e = run(f"{brew} install python@3.12", timeout=1800)
                if rc != 0:
                    die(f"brew install failed:\n{e or o}")
                rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
                py = o.strip() if rc == 0 else ""
            if not py:
                die("No Python >=3.11 on remote. Re-run with --install-python (uses Homebrew), "
                    "or pass --remote-python /path/to/python3.x.")
        rc, o, _ = run(f"{py} --version")
        ok(f"Remote Python: {o.strip()}  ({py})")

        # 4. Device checks out — register the node (mints UUID + api_key)
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

        # 5. Clone from GitHub + install + start (all on the device)
        info(f"Cloning {args.repo_url}@{args.branch} on the device + installing ({mode})…")
        sftp = client.open_sftp()
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
            f.write(INSTALL_SH); local_sh = f.name
        sftp.put(local_sh, "/tmp/nexus-install.sh")
        sftp.close()
        rc, o, e = run(f"bash /tmp/nexus-install.sh {_q(py)} {_q(rd)} {_q(args.repo_url)} "
                       f"{_q(args.branch)} {_q(ws_url)} {_q(api_key)} {_q(node_id)} {_q(mode)}",
                       timeout=1200)
        print("    " + "\n    ".join((o or "").strip().splitlines()))
        if rc != 0:
            warn((e or "").strip())
            _cleanup(args.api, token, node_id)
            die("Remote setup failed (node deregistered).")
        ok(f"Agent installed + started on remote ({mode}).")
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
        warn(f"  ssh {args.target} 'tail -f {rd}/agent.log'")
        if args.service:
            warn("launchd user services need an active login (GUI) session on the Mac to start; "
                 "they'll come up at next login if the box is currently headless.")

    # 8. Management hints
    if args.service:
        print(f"\nService (auto-starts on boot, restarts on crash):\n"
              f"  Logs:    ssh {args.target} 'tail -f {rd}/agent.log'\n"
              f"  Status:  ssh {args.target} 'launchctl print gui/$(id -u)/com.nexus.agent 2>/dev/null || systemctl --user status nexus-agent'\n"
              f"  Stop:    ssh {args.target} 'launchctl bootout gui/$(id -u)/com.nexus.agent 2>/dev/null || systemctl --user disable --now nexus-agent'\n"
              f"  Remove:  curl -X DELETE {args.api}/api/nodes/{node_id} -H 'Authorization: Bearer <token>'")
    else:
        print(f"\nBackground (config saved; manage over SSH with nexusctl):\n"
              f"  Restart: ssh {args.target} '{rd}/nexusctl restart'\n"
              f"  Status:  ssh {args.target} '{rd}/nexusctl status'\n"
              f"  Logs:    ssh {args.target} '{rd}/nexusctl logs'\n"
              f"  Remove:  curl -X DELETE {args.api}/api/nodes/{node_id} -H 'Authorization: Bearer <token>'")

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

def _local_ipv4s():
    """All controller IPv4 addresses, default-route first — candidates for the
    address the remote agent should dial back to."""
    import socket, subprocess, re
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ips.append(s.getsockname()[0]); s.close()
    except Exception:
        pass
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout
        if not out:
            out = subprocess.run(["ip", "-4", "addr"], capture_output=True, text=True, timeout=5).stdout
        for ip in re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", out):
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return [ip for ip in ips if not ip.startswith(("127.", "169.254."))]

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
