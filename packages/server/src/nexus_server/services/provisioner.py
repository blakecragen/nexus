"""Node provisioning over SSH (server-side).

Ports the device-setup logic from the repo's nexus_deploy.py into the server so
the dashboard's "Register Node" can set a device up end-to-end: SSH in, clone the
agent from GitHub, install into a venv, persist config, and start it (background
or as an auto-start service).

All paramiko calls are blocking; callers run provision() via asyncio.to_thread.
Passwords are held in memory only and never logged.
"""
from __future__ import annotations

import io
import re
import socket
import subprocess

import paramiko

GITHUB_URL_DEFAULT = "https://github.com/blakecragen/nexus.git"

# Find a Python >=3.11 on the remote (PATH + common Homebrew locations).
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

# Args: PY RD REPO_URL BRANCH CANDS PORT KEY NID MODE
INSTALL_SH = r'''#!/bin/bash
set -e
PY="$1"; RD="$2"; REPO_URL="$3"; BRANCH="$4"; CANDS="$5"; PORT="$6"; KEY="$7"; NID="$8"; MODE="$9"

command -v git >/dev/null 2>&1 || { echo "NO_GIT"; exit 3; }

if [ -d "$RD/.git" ]; then
  git -C "$RD" remote set-url origin "$REPO_URL" 2>/dev/null || true
  git -C "$RD" fetch --depth 1 origin "$BRANCH"
  git -C "$RD" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  rm -rf "$RD"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$RD"
fi
cd "$RD"
RD="$(pwd)"

"$PY" -m venv .venv
./.venv/bin/python -m pip install -q --upgrade pip
./.venv/bin/python -m pip install -q -e packages/common
./.venv/bin/python -m pip install -q -e packages/steps
./.venv/bin/python -m pip install -q -e packages/agent
AGENT="$(pwd)/.venv/bin/nexus-agent"

# Pick the server address this device can actually complete a WebSocket
# handshake to (not just HTTP — on multi-homed/asymmetric LANs HTTP can succeed
# where the sustained WS gets "No route to host"). Tries candidates in order
# using the agent's own websockets lib against the auth-less /ws/dashboard path.
WS_HOST=$(CANDS="$CANDS" PORT="$PORT" ./.venv/bin/python - <<'PYEOF'
import os, asyncio, websockets
cands = os.environ["CANDS"].split(","); port = os.environ["PORT"]
async def ok(h):
    try:
        ws = await asyncio.wait_for(websockets.connect(f"ws://{h}:{port}/ws/dashboard"), timeout=6)
        await ws.close(); return True
    except Exception:
        return False
async def main():
    for h in cands:
        h = h.strip()
        if h and await ok(h):
            print(h); return
asyncio.run(main())
PYEOF
)
if [ -z "$WS_HOST" ]; then echo "NO_WS_ROUTE"; exit 7; fi
WS="ws://$WS_HOST:$PORT/ws/agent/$NID"
echo "WS_HOST $WS_HOST"

"$AGENT" init --server "$WS" --api-key "$KEY" --node-id "$NID" >/dev/null

cat > "$RD/nexusctl" <<'CTL'
#!/bin/bash
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
  "$RD/nexusctl" start >/dev/null
  sleep 3
  if "$RD/nexusctl" status | grep -q running; then
    echo "AGENT_RUNNING $(cat agent.pid)"; tail -n 6 agent.log 2>/dev/null || true
  else
    echo "AGENT_DIED"; tail -n 25 agent.log 2>/dev/null || true; exit 5
  fi
fi
'''


def _q(s) -> str:
    """Single-quote a string for safe shell embedding."""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def local_ipv4s() -> list[str]:
    """All of this server's IPv4 addresses, default-route first — candidates for
    the address a remote agent should dial back to."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
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


def server_hostname() -> str | None:
    """The server's mDNS/host name (e.g. 'foo.local'). Preferred callback address
    because it re-resolves on every reconnect and follows DHCP IP changes."""
    try:
        h = socket.gethostname()
    except Exception:
        return None
    if not h or h in ("localhost", "localhost.local"):
        return None
    # macOS often returns the bare name; the .local form is what mDNS resolves.
    if "." not in h:
        h = h + ".local"
    return h


def callback_candidates() -> list[str]:
    """Addresses a remote agent could dial back to, most-stable first: the mDNS
    hostname (survives IP changes), then IPv4 addresses (default route first)."""
    cands: list[str] = []
    h = server_hostname()
    if h:
        cands.append(h)
    for ip in local_ipv4s():
        if ip not in cands:
            cands.append(ip)
    return cands


def _first_path(run, candidates):
    for c in candidates:
        rc, _, _ = run(f"command -v {_q(c)}")
        if rc == 0:
            return c
    return None


def provision(
    *,
    host: str,
    user: str,
    password: str | None,
    use_server_key: bool,
    node_id: str,
    api_key: str,
    server_ips: list[str],
    ws_port: int = 8000,
    repo_url: str = GITHUB_URL_DEFAULT,
    branch: str = "main",
    service: bool = False,
    install_python: bool = True,
    remote_python: str | None = None,
) -> dict:
    """Blocking: SSH to host, clone+install+start the agent. Returns a dict with
    {ok, ws_url?, ws_host?, log[], error?}. Never raises for expected failures."""
    log: list[str] = []

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if use_server_key:
            client.connect(host, username=user, timeout=20)
        else:
            client.connect(host, username=user, password=password, timeout=20,
                           look_for_keys=False, allow_agent=False)
    except Exception as e:
        return {"ok": False, "error": f"SSH connection failed: {type(e).__name__}: {e}", "log": log}

    def run(cmd, timeout=600):
        _i, o, e = client.exec_command(cmd, timeout=timeout)
        rc = o.channel.recv_exit_status()
        return rc, o.read().decode(), e.read().decode()

    try:
        log.append("Connected.")

        # 1. git present?
        rc, _, _ = run("command -v git")
        if rc != 0:
            return {"ok": False, "error": "git not found on the remote device.", "log": log}

        # 2. Resolve Python >=3.11 (optionally brew-install).
        py = remote_python or ""
        if not py:
            rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
            py = o.strip() if rc == 0 else ""
        if not py and install_python:
            log.append("No Python >=3.11; installing python@3.12 via Homebrew (can take minutes)…")
            brew = _first_path(run, ["/opt/homebrew/bin/brew", "/usr/local/bin/brew", "brew"])
            if not brew:
                return {"ok": False, "error": "No Python >=3.11 and Homebrew not found on remote.", "log": log}
            rc, o, e = run(f"{brew} install python@3.12", timeout=1800)
            if rc != 0:
                return {"ok": False, "error": f"brew install failed: {(e or o).strip()[:400]}", "log": log}
            rc, o, _ = run(f"bash -c {_q(RESOLVE_PY)}")
            py = o.strip() if rc == 0 else ""
        if not py:
            return {"ok": False, "error": "No Python >=3.11 on remote (enable 'install Python').", "log": log}
        log.append(f"Remote Python: {py}")

        # 3. Clone + install + start. The install script itself picks the server
        #    address by trying a REAL WebSocket handshake to each candidate (HTTP
        #    reachability isn't enough on multi-homed/asymmetric LANs), in order.
        candidates = server_ips or ["localhost"]
        cands_arg = ",".join(candidates)
        sftp = client.open_sftp()
        sftp.putfo(io.BytesIO(INSTALL_SH.encode()), "/tmp/nexus-install.sh")
        sftp.close()
        mode = "service" if service else "background"
        rc, o, e = run(
            f"bash /tmp/nexus-install.sh {_q(py)} {_q('nexus')} {_q(repo_url)} "
            f"{_q(branch)} {_q(cands_arg)} {_q(str(ws_port))} {_q(api_key)} {_q(node_id)} {_q(mode)}",
            timeout=1200,
        )
        out_lines = (o or "").strip().splitlines()
        chosen = None
        for line in out_lines:
            if line.startswith("WS_HOST "):
                chosen = line.split(" ", 1)[1].strip()
            else:
                log.append(line)
        if rc != 0:
            if "NO_WS_ROUTE" in (o or "") + (e or ""):
                return {"ok": False, "log": log, "error": (
                    f"The device could not complete a WebSocket handshake to ANY server address "
                    f"({', '.join(candidates)}). HTTP may work but the WS path is blocked — typically "
                    f"overlapping/asymmetric subnets (both machines on the same LAN twice) or a VPN. "
                    f"Fix the routing, or pass a known-good ws_host.")}
            return {"ok": False, "error": f"Install failed: {(e or o).strip()[:500]}", "log": log}
        if chosen:
            log.insert(1, f"Selected callback address {chosen} (WebSocket handshake OK).")
        ws_url = f"ws://{chosen or candidates[0]}:{ws_port}/ws/agent/{node_id}"
        return {"ok": True, "ws_url": ws_url, "ws_host": chosen or candidates[0], "mode": mode, "log": log}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "log": log}
    finally:
        client.close()
