"""Node provisioning over SSH (server-side).

Ports the device-setup logic from the repo's nexus_deploy.py into the server so
the dashboard's "Register Node" can set a device up end-to-end: SSH in, clone the
agent from GitHub, install into a venv, persist config, and start it (background
or as an auto-start service).

All paramiko calls are blocking; callers run provision() via asyncio.to_thread.
Passwords are held in memory only and never logged.

Where this sits
---------------
Entry point is :func:`provision`, called from
``nexus_server.api.routes.nodes._provision_and_poll`` inside
``asyncio.to_thread`` (paramiko is synchronous and would otherwise stall the
event loop). That caller creates/looks up the ``nodes`` row first, passes the
node id + API key in, and then polls the DB with *fresh* sessions until the
freshly installed agent's WebSocket handler flips the node to ``online``.
Nothing in this module touches the database.

The remote side ends up running ``packages/agent`` (``nexus-agent``) from a
shallow clone of this same repo, configured to dial back to
``ws://<host>:<port>/ws/agent/<node_id>`` — the endpoint served by
``nexus_server.api.routes.ws``.

Design notes / gotchas
----------------------
- ``INSTALL_SH`` and ``RESOLVE_PY`` are shell programs shipped to the remote and
  executed there. They are the real logic of this module; the Python around them
  is transport and error translation. Editing them changes remote behaviour with
  no local test coverage — treat them as production code.
- Both macOS (launchd) and Linux (systemd --user) are supported for the
  auto-start "service" mode; anything else fails with ``SERVICE_UNSUPPORTED``.
- Every expected failure is returned as ``{"ok": False, "error": ...}`` rather
  than raised, because the HTTP layer surfaces the ``log`` list to the dashboard.
"""
from __future__ import annotations

import io
import re
import socket
import subprocess

import paramiko

#: Default git remote the remote device clones the agent from. Overridable per
#: request (``ProvisionRequest.repo_url``) for forks or internal mirrors; the
#: repo must be reachable *from the device*, unauthenticated (no credentials are
#: forwarded over the SSH session).
GITHUB_URL_DEFAULT = "https://github.com/blakecragen/nexus.git"

# Find a Python >=3.11 on the remote (PATH + common Homebrew locations).
#
# AI Note: run as `bash -c '<this>'` and expected to print the interpreter path
# on stdout with exit 0, or exit 1 when nothing suitable is found. The `case`
# glob `3.1[1-9]|3.[2-9]*` is the version gate: it accepts 3.11-3.19 and 3.2x+
# and rejects 3.10 and below (the agent needs 3.11+). Harmless quirk: the second
# pattern would also match "3.9", but the candidate list never yields one.
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
#
# The full remote installer, uploaded to /tmp/nexus-install.sh by provision()
# and executed with `bash`. Positional args (all shell-quoted by the caller):
#   $1 PY       absolute path to a Python >=3.11 on the device
#   $2 RD       repo directory, relative to the SSH user's $HOME (always "nexus")
#   $3 REPO_URL git remote to clone/fetch
#   $4 BRANCH   branch to check out
#   $5 CANDS    comma-separated server addresses to try for the WS callback
#   $6 PORT     server port
#   $7 KEY      the node's API key (written into the agent config)
#   $8 NID      the node's UUID (becomes the /ws/agent/<id> path segment)
#   $9 MODE     "service" (launchd/systemd auto-start) or "background" (nohup)
#
# Contract with provision(): `set -e` means any unhandled command failure aborts
# with a non-zero rc. Meaningful exit codes: 3 = git missing, 5 = agent started
# but died, 6 = service mode on an unsupported OS, 7 = no reachable WS address.
# Stdout lines are echoed into the API's `log` list, except "WS_HOST <addr>"
# which provision() parses out as the chosen callback address.
#
# Non-obvious things the script does, in order:
#   * Re-uses an existing clone when $RD/.git is present (fetch+checkout) and
#     otherwise rm -rf's the directory — so re-provisioning is idempotent.
#   * Installs packages/common -> packages/steps -> packages/agent in that
#     order (agent imports steps, both import common) and editable (-e) so a
#     later `git pull` on the device needs no reinstall.
#   * Probes each CANDS entry with a REAL WebSocket handshake (see inline
#     comment) instead of an HTTP GET, then hard-fails with NO_WS_ROUTE.
#   * Writes a small `nexusctl` helper so an operator can start/stop/tail the
#     agent later without re-running this installer.
#   * Before starting, tears down ALL three possible previous start mechanisms
#     (nohup pid file, launchd job, systemd --user unit), each `|| true`. This
#     is what lets a node switch between background and service mode without
#     ending up with two agents racing over the same node id.
#   * On Linux it calls `loginctl enable-linger` so the --user unit survives
#     logout; without it the agent dies when the SSH session's scope ends.
#
# NOTE: this literal is executed verbatim on remote machines — do not add
# comments or reformat inside it without testing against a real device.
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
    """Single-quote a string for safe shell embedding.

    Every value interpolated into a remote command string (paths, URLs, the API
    key, the node id) must go through this. The ``'"'"'`` dance closes the
    single-quoted run, emits a literal quote, and reopens it — the standard
    POSIX idiom, since single quotes have no escape sequence.

    Args:
        s: Any value; coerced with ``str()``.

    Returns:
        A single shell word that expands to exactly ``str(s)``.

    Note:
        Security-relevant: this is the only barrier between operator-supplied
        strings (SSH user input, repo URL, branch) and ``bash`` on the remote
        host. Do not build remote commands with f-strings that skip it.
    """
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def local_ipv4s() -> list[str]:
    """All of this server's IPv4 addresses, default-route first — candidates for
    the address a remote agent should dial back to.

    Two discovery passes, each individually wrapped in ``try/except`` so a
    missing tool or an offline host degrades instead of raising:

    1. A UDP "connect" to 8.8.8.8:80 — sends no packets, but makes the kernel
       select the default-route source address. That address goes first because
       it is the one a peer is most likely able to reach.
    2. ``ifconfig`` (falling back to ``ip -4 addr``) scraped for every other
       ``inet`` address, preserving interface order.

    Returns:
        Deduplicated IPv4 strings, default route first. Loopback (``127.*``) and
        link-local/APIPA (``169.254.*``) are filtered out because an agent on
        another machine can never reach them.

    Side effects:
        Opens a UDP socket and spawns ``ifconfig``/``ip`` subprocesses (5s
        timeout each). Step 1 puts no traffic on the wire.
    """
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
    because it re-resolves on every reconnect and follows DHCP IP changes.

    Returns:
        The hostname, or ``None`` when it is unusable — either ``gethostname()``
        raised, or it returned a loopback name that a remote agent could never
        resolve back to this machine.

    Note:
        Only useful when device and server share an mDNS/DNS domain. If the name
        does not resolve on the device, the installer's handshake probe simply
        falls through to the IP candidates, so a bad guess costs one timeout.
    """
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
    hostname (survives IP changes), then IPv4 addresses (default route first).

    Called by ``api/routes/nodes.py`` whenever the operator did not pin an
    explicit ``ws_host``. Order is load-bearing: the remote installer tries these
    in sequence and keeps the FIRST one that completes a real WebSocket
    handshake, so leading with the hostname means a node keeps reconnecting
    across DHCP lease changes.

    Returns:
        Deduplicated candidate hosts. May be empty on a fully isolated host, in
        which case :func:`provision` falls back to ``["localhost"]``.
    """
    cands: list[str] = []
    h = server_hostname()
    if h:
        cands.append(h)
    for ip in local_ipv4s():
        if ip not in cands:
            cands.append(ip)
    return cands


def _first_path(run, candidates):
    """Return the first candidate executable that exists on the remote host.

    Args:
        run: The closure defined inside :func:`provision`; ``run(cmd)`` returns
            ``(rc, stdout, stderr)`` over the already-open SSH session.
        candidates: Command names or absolute paths, in preference order.

    Returns:
        The first candidate for which ``command -v`` exits 0, else ``None``.

    Note:
        Each probe is a separate ``exec_command`` round trip, so keep candidate
        lists short.
    """
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
    {ok, ws_url?, ws_host?, log[], error?}. Never raises for expected failures.

    Sequence: connect -> verify ``git`` -> resolve (or brew-install) a Python
    >=3.11 -> upload and run :data:`INSTALL_SH` -> parse its output. The
    installer itself decides which server address the device calls back on.

    Args:
        host: SSH target (hostname or IP) of the device being provisioned.
        user: SSH username. The agent is installed under this user's ``$HOME``
            and, in service mode, registered as that user's launchd/systemd job.
        password: SSH password. Ignored when ``use_server_key`` is True. Held in
            memory only — never appended to ``log`` or written to the remote.
        use_server_key: When True, authenticate with the server process's own
            SSH keys/agent instead of a password.
        node_id: UUID of the ``nodes`` row the caller already created. Becomes
            the ``/ws/agent/<node_id>`` path the agent connects to, so it must
            match the DB row or the agent will be rejected on connect.
        api_key: The node's API key, written into the remote agent config and
            presented on every WebSocket connect.
        server_ips: Ordered callback candidates (see :func:`callback_candidates`).
            An empty list degrades to ``["localhost"]``, which only works when
            the device *is* the server.
        ws_port: Port the Nexus server listens on.
        repo_url: Git remote to install the agent from.
        branch: Branch to check out. Must contain a compatible agent — the
            device runs whatever this points at, with no version negotiation.
        service: True installs an auto-start service (launchd on macOS, systemd
            --user on Linux) that survives reboot; False just nohups the agent,
            which dies with the machine.
        install_python: Allow installing ``python@3.12`` via Homebrew when no
            suitable interpreter is found. Can take many minutes.
        remote_python: Pin a specific remote interpreter path and skip detection
            (and the Homebrew fallback) entirely.

    Returns:
        On success ``{"ok": True, "ws_url", "ws_host", "mode", "log"}``; on
        failure ``{"ok": False, "error": <human-readable>, "log": [...]}``.
        ``log`` is always present and is rendered in the dashboard, so error
        strings are written for operators, not for machines.

    Side effects:
        Opens an SSH + SFTP session; writes ``/tmp/nexus-install.sh``, a git
        clone, a venv, a ``nexusctl`` script, and (in service mode) a launchd
        plist or systemd unit on the remote host; starts a long-running agent
        process there. Kills any previously provisioned agent first.

    Note:
        Fully blocking (paramiko). Must be invoked via ``asyncio.to_thread``
        from async code. Also note ``AutoAddPolicy`` below: unknown host keys
        are accepted silently, trading TOFU protection for usability on lab
        networks — only appropriate on a trusted LAN.
    """
    log: list[str] = []

    client = paramiko.SSHClient()
    # AI Note: AutoAddPolicy accepts any host key without prompting. Deliberate
    # (lab devices get reimaged and would otherwise fail on a changed key) but
    # it means this call is not MITM-resistant.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        if use_server_key:
            client.connect(host, username=user, timeout=20)
        else:
            # AI Note: look_for_keys/allow_agent disabled so a stray key on the
            # server can't silently succeed when the operator asked for password
            # auth — a wrong password must fail loudly rather than half-work.
            client.connect(host, username=user, password=password, timeout=20,
                           look_for_keys=False, allow_agent=False)
    except Exception as e:
        return {"ok": False, "error": f"SSH connection failed: {type(e).__name__}: {e}", "log": log}

    def run(cmd, timeout=600):
        """Execute one command on the open SSH session and wait for it to exit.

        Args:
            cmd: Shell command line; any interpolated value must already have
                been escaped with :func:`_q`.
            timeout: Per-channel inactivity timeout, in seconds.

        Returns:
            ``(exit_status, stdout, stderr)``, both streams fully read and
            UTF-8 decoded.

        Note:
            ``recv_exit_status()`` blocks until the remote command finishes and
            stdout is only drained afterwards, so a command emitting more output
            than the SSH channel window can buffer would deadlock. Fine for the
            short, quiet commands used here; do not reuse for chatty processes.
        """
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
            # AI Note: 1800s (30 min) is not arbitrary — a cold
            # `brew install python@3.12` can build dependencies from source on
            # an un-warmed machine and routinely exceeds the 600s default.
            rc, o, e = run(f"{brew} install python@3.12", timeout=1800)
            if rc != 0:
                return {"ok": False, "error": f"brew install failed: {(e or o).strip()[:400]}", "log": log}
            # AI Note: re-resolve after the install — the formula drops a
            # versioned binary that was not on PATH during the first probe.
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
        # AI Note: the literal 'nexus' is $2 (RD), the clone directory relative
        # to the SSH user's home. Changing it orphans agents installed by earlier
        # runs — the idempotent-reinstall and teardown paths both key off it.
        rc, o, e = run(
            f"bash /tmp/nexus-install.sh {_q(py)} {_q('nexus')} {_q(repo_url)} "
            f"{_q(branch)} {_q(cands_arg)} {_q(str(ws_port))} {_q(api_key)} {_q(node_id)} {_q(mode)}",
            timeout=1200,
        )
        # AI Note: stdout is a mixed stream — the single "WS_HOST <addr>" line is
        # a machine-readable result, everything else is operator-facing log text.
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
            # Index 1 == immediately after the "Connected." line, so the chosen
            # address reads as the first real step in the dashboard log.
            log.insert(1, f"Selected callback address {chosen} (WebSocket handshake OK).")
        # AI Note: the `or candidates[0]` fallback should be unreachable — a
        # missing WS_HOST means the installer exited 7 and we returned above.
        ws_url = f"ws://{chosen or candidates[0]}:{ws_port}/ws/agent/{node_id}"
        return {"ok": True, "ws_url": ws_url, "ws_host": chosen or candidates[0], "mode": mode, "log": log}
    except Exception as e:
        # Catch-all so unexpected paramiko/decode errors still come back as a
        # structured result carrying whatever log lines were collected, rather
        # than a 500 that tells the operator nothing.
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "log": log}
    finally:
        # AI Note: covers every `return` inside the try block, which is the only
        # thing preventing leaked SSH sessions on repeated provisioning
        # failures. The connect-failure return above is outside this try and so
        # never reaches close() — harmless because the transport never came up.
        client.close()
