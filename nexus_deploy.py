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
# Tiny logging shim. Deliberately not the `logging` module: this is an operator
# facing CLI whose entire value is a readable, colourised transcript.
#
# _TTY : True when stdout is a terminal. Colour codes are suppressed otherwise
#        so piping/redirecting the output (or capturing it from add_node.sh in
#        CI) yields clean, escape-free text.
# _c   : wrap `s` in ANSI colour `code`, or return it unchanged when not a TTY.
# info : progress step, to stdout.
# ok   : success step, to stdout.
# warn : non-fatal problem, to STDERR (so it survives stdout redirection).
# die  : fatal error to stderr, then exit(1). Never returns.
#
# AI Note: `die()` calls sys.exit(1), so callers below treat it as terminal and
# do not guard afterwards. It is also used *inside* the `try:` in main(); the
# resulting SystemExit still runs the `finally:` that closes the SSH client.
_TTY = sys.stdout.isatty()
def _c(code, s): return f"\033[{code}m{s}\033[0m" if _TTY else s
def info(m): print(_c("34", "==>"), m)
def ok(m):   print(_c("32", " ok"), m)
def warn(m): print(_c("33", "warn"), m, file=sys.stderr)
def die(m):  print(_c("31", "err"), m, file=sys.stderr); sys.exit(1)

# ── HTTP helpers (stdlib) ───────────────────────────────────────────────────
def _req(method, url, token=None, body=None, timeout=15):
    """Issue a JSON HTTP request against the Nexus API using only the stdlib.

    Deliberately avoids `requests`/`httpx`: this script must run under whatever
    python3 happens to have paramiko installed (see add_node.sh), so it may not
    have the project's dependencies available.

    Args:
        method: HTTP verb.
        url: absolute URL.
        token: optional bearer token for authenticated endpoints.
        body: optional object, JSON-encoded as the request body.
        timeout: socket timeout in seconds.

    Returns:
        (status_code, parsed_body_or_None). An empty response body yields None.

    AI Note: HTTPError is caught and downgraded to `(code, None)` — the error
    body is intentionally discarded, so callers can only report the status.
    Connection-level failures (server down, DNS) are NOT caught here and will
    propagate as URLError.
    """
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
    """Exchange admin credentials for a bearer token; die() on failure.

    Node registration requires the admin role, so every non-``--register-only``
    path starts here.

    Args:
        api: API base URL (e.g. http://localhost:8000).
        user, pw: admin credentials (from --admin-user/--admin-pass or env).

    Returns:
        The access token string.
    """
    st, body = _req("POST", f"{api}/api/auth/login", body={"username": user, "password": pw})
    if st != 200 or not body:
        die(f"Login failed (HTTP {st}). Is the API up at {api}? Are admin creds right?")
    return body["access_token"]

def api_register(api, token, hostname, name):
    """Create the node record and mint its agent API key.

    Args:
        api: API base URL.
        token: admin bearer token from api_login().
        hostname: SSH host, stored as the node's hostname.
        name: friendly display name shown in the dashboard.

    Returns:
        (node_id, api_key) — the UUID the agent dials back with, and the shared
        secret it authenticates with.

    AI Note: the hardware fields below are deliberate PLACEHOLDERS
    ("pending"/"unknown"/0.0.0.0/1 core/1024 MB), not real detection. The agent
    overwrites them with the true specs on its first heartbeat. Consequence: a
    freshly registered node briefly shows bogus specs in the dashboard, and a
    node that never comes online keeps showing them forever.
    """
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
    """Read a node's current status string, for the post-install online poll.

    Returns the status (e.g. "online"/"offline") or a ``"http <code>"`` marker
    on failure — a sentinel string rather than an exception, because the caller
    polls this in a loop and only cares whether it has become "online" yet.
    """
    st, b = _req("GET", f"{api}/api/nodes/{node_id}", token=token)
    return b.get("status") if (st == 200 and b) else f"http {st}"

# ── remote-python resolution (run on remote) ────────────────────────────────
# Shell snippet executed on the REMOTE host to locate a Python >= 3.11 (the
# agent packages' floor). Prints the first usable interpreter path and exits 0,
# or exits 1 if none is found.
#
# AI Note: bare names are probed before Homebrew/local paths so a PATH-managed
# interpreter (pyenv, conda, distro) wins. The `case` glob is the version gate:
# "3.1[1-9]" matches 3.11-3.19 and "3.[2-9]*" matches 3.2x+ — string globbing is
# used because there is no reliable numeric comparison in portable /bin/sh.
# Note that 3.1x versions above 3.19 (e.g. a future 3.110) would NOT match.
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
#
# The full remote installer, uploaded to /tmp/nexus-install.sh over SFTP and run
# with bash. Everything it needs arrives as positional args (quoted by _q) so no
# secrets are baked into the file contents:
#   $1 PY       python >=3.11 interpreter path resolved by RESOLVE_PY
#   $2 RD       install/clone directory, relative to the remote $HOME
#   $3 REPO_URL git repo to clone (the device pulls its own code)
#   $4 BRANCH   branch to check out
#   $5 WS       ws:// URL the agent dials back to
#   $6 KEY      node api_key (written 0600 into ~/.nexus-agent/config.json)
#   $7 NID      node UUID
#   $8 MODE     "service" (launchd/systemd) or anything else -> background
#
# Distinct non-zero exits let the caller distinguish failures: 3 = git missing,
# 5 = agent started but died, 6 = --service on an unsupported OS.
#
# AI Note: `set -e` is on, but many teardown/compat lines end in
# `|| true` — that is deliberate. Stopping a prior instance, unloading a launchd
# job, or disabling a systemd unit all fail harmlessly on a first-time install,
# and must not abort the run.
#
# AI Note: the script is idempotent by design (re-running it updates an existing
# checkout, recreates the venv, stops the previous agent, then restarts). That
# is what makes the "Bring Online"/reconnect flow in the UI safe to retry.
#
# AI Note: the `sleep 3` before the status check is a deliberate settle window —
# the agent needs a moment to either connect or crash. Too short and a healthy
# agent is reported as AGENT_DIED; the caller has no other liveness signal.
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
    """CLI entry point: parse args, then run the register-only or full SSH flow.

    Full flow ordering (SSH-first, register-last) is a deliberate invariant:
      1. connect over SSH and pick a controller address the remote can reach
      2. verify git + a Python >= 3.11 exist on the device
      3. ONLY THEN register the node (which mints a UUID + api_key)
      4. upload and run INSTALL_SH (clone, venv, install, configure, start)
      5. poll the API until the node reports online
      6. print management hints

    AI Note: steps 1-3 are ordered this way so a device that fails preflight
    never leaves an orphan node record in the database. If the install itself
    fails after registration, `_cleanup()` deregisters the node explicitly.
    """
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
    # Mints the node record and prints the credentials for manual setup. Useful
    # when the device can't be reached over SSH from here (NAT, jump host, or a
    # Windows box) — someone runs `nexus-agent run ...` on it by hand.
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
        # Password precedence: CLI arg > $NEXUS_SSH_PASSWORD > $SSHPASS > prompt.
        # AI Note: the non-TTY branch must die() rather than prompt — otherwise
        # a CI/sandbox invocation would block forever on stdin.
        pw = args.password or os.environ.get("NEXUS_SSH_PASSWORD") or os.environ.get("SSHPASS")
        if not pw:
            if sys.stdin.isatty():
                pw = getpass.getpass(f"SSH password for {args.target}: ")
            else:
                die("No password given. Pass it as the 2nd arg, set $NEXUS_SSH_PASSWORD, or use --key.")

    client = paramiko.SSHClient()
    # AI Note: AutoAddPolicy accepts any host key without verification, which
    # disables SSH MITM protection. Accepted deliberately for first-contact
    # provisioning of lab machines on a trusted LAN; do NOT reuse this pattern
    # for connections over untrusted networks.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    info(f"Connecting to {args.target} ({'key' if args.key else 'password'} auth)")
    try:
        if args.key:
            client.connect(host, username=user, timeout=20)
        else:
            # AI Note: look_for_keys/allow_agent are disabled in password mode
            # on purpose. Otherwise paramiko tries every agent identity first
            # and can exhaust the server's MaxAuthTries before ever attempting
            # the password, producing a misleading "authentication failed".
            client.connect(host, username=user, password=pw, timeout=20,
                           look_for_keys=False, allow_agent=False)
    except Exception as e:
        die(f"SSH connection failed: {type(e).__name__}: {e}")
    ok("Connected.")

    def run(cmd, timeout=600):
        """Run `cmd` on the connected remote host and wait for it to finish.

        Returns:
            (exit_code, stdout, stderr) with the streams fully read and decoded.

        AI Note: `recv_exit_status()` blocks until the command completes, so
        this is strictly synchronous — fine here, but it means a remote command
        that never exits hangs the deploy despite the `timeout` (which only
        bounds socket reads, not total runtime).
        """
        _in, out, err = client.exec_command(cmd, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return rc, out.read().decode(), err.read().decode()

    mode = "service" if args.service else "background"
    token = node_id = api_key = None
    try:
        # 1. Pick the controller address the REMOTE can actually reach (auto,
        #    unless --ws-host was given). Handles multi-homed controllers.
        #
        # AI Note: reachability is probed FROM the remote (curl runs there), not
        # from here — the whole point is that the controller's own view of its
        # address (often 127.0.0.1) is useless to the agent. curl's "000" means
        # "no HTTP response at all"; ANY real status (including 401/404) proves
        # the address is routable, which is why the check is `c != "000"` rather
        # than a 2xx check.
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
                # AI Note: 1800s (30 min) — a from-source brew build of Python
                # on an older Mac genuinely takes this long. Do not lower it to
                # the default 600s or slow machines fail mid-compile.
                rc, o, e = run(f"{brew} install python@3.12", timeout=1800)
                if rc != 0:
                    die(f"brew install failed:\n{e or o}")
                # Re-resolve: brew's new interpreter should now be discoverable.
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
        # AI Note: the api_key is passed as a command ARGUMENT, so it is briefly
        # visible in the remote process list (`ps`) during the install. The
        # installer immediately persists it 0600 in ~/.nexus-agent/config.json.
        # Every argument goes through _q() — unquoted interpolation here would
        # be a shell-injection hole on hostnames/branches/paths.
        # AI Note: 1200s (20 min) covers the clone + venv + three pip installs
        # on a slow or bandwidth-limited device.
        rc, o, e = run(f"bash /tmp/nexus-install.sh {_q(py)} {_q(rd)} {_q(args.repo_url)} "
                       f"{_q(args.branch)} {_q(ws_url)} {_q(api_key)} {_q(node_id)} {_q(mode)}",
                       timeout=1200)
        print("    " + "\n    ".join((o or "").strip().splitlines()))
        if rc != 0:
            warn((e or "").strip())
            # Roll back the registration so a failed install leaves no orphan.
            _cleanup(args.api, token, node_id)
            die("Remote setup failed (node deregistered).")
        ok(f"Agent installed + started on remote ({mode}).")
    finally:
        # AI Note: `finally` (not a context manager) because die() raises
        # SystemExit from several branches above — the SSH connection must be
        # closed on every exit path, including those.
        client.close()

    # 7. Wait for online
    # Poll for up to ~30s (15 attempts x 2s) for the agent's first heartbeat.
    # A timeout is NOT fatal: the agent may still connect moments later, so we
    # warn with troubleshooting hints instead of failing the deploy.
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
    """Single-quote a string for safe shell embedding.

    Wraps `s` in single quotes and escapes any embedded single quote using the
    standard ``'"'"'`` idiom (close-quote, quoted-quote, reopen-quote).

    AI Note: security-relevant. Every value interpolated into a remote command
    (paths, hostnames, branch names, the api_key) must pass through this, or a
    crafted value could break out of the quoting and execute arbitrary commands
    on the target host as the SSH user.
    """
    return "'" + str(s).replace("'", "'\"'\"'") + "'"

def _default_ws_host():
    """Best-effort primary LAN IP the remote agent can dial back to.

    Used only by the ``--register-only`` path (the SSH path uses the
    remote-verified `_local_ipv4s()` probe instead).

    AI Note: the UDP "connect" to 8.8.8.8 sends NO packets — it just asks the
    kernel which local interface would be used for that route, which is the
    portable way to find the primary outbound IP. It works with no internet
    connectivity, but it does need a default route; falls back to "localhost".
    """
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
    address the remote agent should dial back to.

    Ordering matters: the default-route address is tried first because it is the
    most likely to be reachable; the remaining interfaces (from `ifconfig`, or
    `ip -4 addr` on hosts without it) cover multi-homed controllers on separate
    lab VLANs.

    AI Note: loopback (127.*) and link-local (169.254.*) are filtered out
    because an agent on another machine can never reach them — offering them as
    candidates would waste a probe and could "succeed" misleadingly if the
    remote happened to run something on that port locally.
    """
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
    """Return the first candidate binary that exists on the REMOTE host.

    Args:
        run: the closure from main() that executes a command over SSH.
        candidates: paths/names to probe in priority order.

    Returns:
        The first candidate resolvable via `command -v`, else None.
    """
    for c in candidates:
        rc, _, _ = run(f"command -v {_q(c)}")
        if rc == 0:
            return c
    return None

def _cleanup(api, token, node_id):
    """Deregister a node we created but couldn't bring up, to avoid orphans.

    AI Note: failures are swallowed on purpose. This runs on an error path that
    is about to die() with the real reason; a secondary exception here (API now
    unreachable, token expired) would mask the diagnosis the operator needs.
    Worst case the node record is left behind and must be deleted by hand.
    """
    try:
        _req("DELETE", f"{api}/api/nodes/{node_id}", token=token)
    except Exception:
        pass

if __name__ == "__main__":
    main()
