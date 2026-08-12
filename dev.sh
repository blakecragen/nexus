#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# Nexus Dev Startup Script
# Starts infrastructure (Docker: Redis + MinIO), API server, and frontend.
# Database is SQLite (local file: nexus.db).
#
# Usage:
#   ./dev.sh          # start everything
#   ./dev.sh stop     # tear down everything
#   ./dev.sh infra    # start only Docker services (Redis, MinIO)
#   ./dev.sh api      # start only the API server (foreground)
#   ./dev.sh ui       # start only the frontend (foreground)
#   ./dev.sh logs     # tail API + frontend logs
#   ./dev.sh status   # check what's running
#   ./dev.sh reset    # delete nexus.db and restart fresh
#
# Layout it manages:
#   Docker  — redis (:6379) + minio (:9000/:9001) from docker-compose.yml
#   API     — uvicorn nexus_server.main:app on :8000, from the .venv
#   UI      — vite dev server on :3000, proxying /api and /ws to :8000
#   DB      — SQLite file nexus.db in the repo root (no Postgres in dev)
#
# Background mode writes PID files (.nexus-api.pid/.nexus-ui.pid) and logs
# (.nexus-api.log/.nexus-ui.log) into the repo root; `stop` and `status` read
# them. Companion scripts: diagnose.sh (read-only health check) and
# add_node.sh (attach agent nodes to the running stack).
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Everything is resolved relative to the repo root, so the script works from
# any cwd (docker compose, .env, nexus.db and the pid/log files all live here).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ANSI colour codes used by the log helpers below (NC = reset).
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

# Log helpers: info = progress, ok = success, warn = non-fatal, err = fatal
# (stderr). Callers are responsible for exiting after err.
info()  { echo -e "${BLUE}[nexus]${NC} $*"; }
ok()    { echo -e "${GREEN}[nexus]${NC} $*"; }
warn()  { echo -e "${YELLOW}[nexus]${NC} $*"; }
err()   { echo -e "${RED}[nexus]${NC} $*" >&2; }

# ── Load .env ───────────────────────────────────────────────────────────
# Source .env and export the derived settings the API server reads. Exits 1 if
# .env is missing, since JWT_SECRET and the credential encryption key have no
# safe defaults.
#
# AI Note: `set -a` makes every subsequent assignment (including those inside
# .env) automatically exported, which is how the sourced values reach uvicorn.
# It is turned back off immediately with `set +a` — leaving it on would export
# every local variable in the rest of the script.
#
# AI Note: the ${VAR:-default} defaults apply only when the value is unset OR
# empty, so a blank line in .env falls back rather than passing an empty string
# to the server. Note MINIO_ACCESS_KEY/SECRET_KEY are renamed from the
# MINIO_ROOT_USER/PASSWORD names docker-compose.yml uses — the compose file and
# the app expect different variable names for the same credentials.
load_env() {
    if [[ ! -f .env ]]; then
        err ".env file not found. Copy .env.template to .env and fill in values."
        exit 1
    fi
    set -a
    source .env
    set +a

    # Ensure DATABASE_URL defaults to local SQLite
    export DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:///nexus.db}"
    export REDIS_URL="${REDIS_URL:-redis://localhost:6379}"
    export MINIO_ENDPOINT="${MINIO_ENDPOINT:-localhost:9000}"
    export MINIO_ACCESS_KEY="${MINIO_ROOT_USER:-nexus}"
    export MINIO_SECRET_KEY="${MINIO_ROOT_PASSWORD:-changeme_minio}"
}

# ── Preflight ───────────────────────────────────────────────────────────
# Verify the four required tools exist and that the Docker daemon is actually
# running (not just installed) — starting it if it is not. Exits 1 with the full
# list of what's missing so the user fixes everything in one pass rather than
# one error at a time.
#
# AI Note: mirrors nexus_steps.docker.util.ensure_daemon(), which does the same
# job on compute nodes. Keep the two in step: `docker desktop start` is tried
# before `open -ga Docker` for the same reason (it works without an active GUI
# session), and readiness is POLLED rather than inferred from the start
# command's exit status, because both return 0 the instant Docker Desktop is
# launched — tens of seconds before the socket accepts connections. Treating
# that 0 as success is what used to produce a "docker daemon not running"
# failure moments after a "Docker started" success line.
check_deps() {
    local missing=()
    command -v docker  &>/dev/null || missing+=("docker")
    command -v python3 &>/dev/null || missing+=("python3")
    command -v node    &>/dev/null || missing+=("node")
    command -v npm     &>/dev/null || missing+=("npm")

    if [[ ${#missing[@]} -gt 0 ]]; then
        err "Missing required tools: ${missing[*]}"
        exit 1
    fi

    ensure_docker_daemon

    ok "All dependencies found."
}

# Bring the Docker daemon up if it is not already, then block until it answers.
# Fatal if it never becomes ready — Redis and MinIO cannot start without it.
ensure_docker_daemon() {
    if docker info &>/dev/null; then
        return 0
    fi

    info "Docker daemon is not responding — trying to start it..."
    # `docker desktop start` (Docker Desktop 4.37+) first; `open -ga Docker`
    # for older installs. -g keeps Docker from stealing focus.
    docker desktop start &>/dev/null \
        || open -ga Docker &>/dev/null \
        || warn "Could not issue a Docker start command — waiting in case it is already starting."

    local waited=0 limit=120
    until docker info &>/dev/null; do
        if (( waited >= limit )); then
            err "Docker daemon did not become ready within ${limit}s."
            err "Start Docker Desktop manually and re-run. (On macOS it only runs"
            err "inside an active GUI login session.)"
            exit 1
        fi
        sleep 2
        waited=$((waited + 2))
    done
    ok "Docker daemon is ready (after ${waited}s)."
}

# ── Infrastructure (Redis + MinIO only) ─────────────────────────────────
# Bring up the two stateful services and block until each is genuinely ready
# (not merely "container created"), then print the connection summary.
#
# AI Note: `redis minio` are named explicitly because those are the only two
# services docker-compose.yml actually defines. Dev mode runs the API and the
# frontend from source with hot reload instead, and there are deliberately no
# compose services for them. (An earlier version of this note claimed the
# compose file "also defines API and frontend services"; it does not, and
# Dockerfile.frontend's `proxy_pass http://api:8000` refers to a service that
# exists in no compose network — see AUDIT.md.)
start_infra() {
    info "Starting infrastructure (Redis, MinIO)..."
    # `down --remove-orphans` first so containers from an older compose file
    # revision can't linger and hold the ports.
    docker compose down --remove-orphans 2>/dev/null || true
    docker compose up redis minio -d

    # AI Note: readiness is polled with a real protocol check (redis-cli PING /
    # MinIO's health endpoint), not `sleep`. Container "up" precedes accepting
    # connections, and the API crashes on startup if Redis isn't listening yet.
    info "Waiting for Redis..."
    local retries=15
    until docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do
        retries=$((retries - 1))
        # Redis is required — a timeout here is fatal.
        if [[ $retries -le 0 ]]; then err "Redis failed to start."; exit 1; fi
        sleep 1
    done
    ok "Redis is ready."

    info "Waiting for MinIO..."
    retries=15
    until curl -sf http://localhost:9000/minio/health/live &>/dev/null; do
        retries=$((retries - 1))
        # AI Note: unlike Redis, a MinIO timeout only warns and continues. The
        # stack is usable without object storage (jobs that don't upload
        # artifacts still run), so this is intentionally non-fatal.
        if [[ $retries -le 0 ]]; then warn "MinIO health check timed out."; break; fi
        sleep 1
    done
    ok "MinIO is ready."

    ok "Infrastructure is up."
    echo ""
    echo -e "  Redis:         ${CYAN}localhost:6379${NC}"
    echo -e "  MinIO API:     ${CYAN}http://localhost:9000${NC}"
    echo -e "  MinIO Console: ${CYAN}http://localhost:9001${NC}"
    echo -e "  Database:      ${CYAN}nexus.db${NC} (SQLite)"
    echo ""
}

# ── Python packages ─────────────────────────────────────────────────────
# Create .venv if absent, then editable-install the three server-side packages
# (common, steps, server). Editable mode is what makes uvicorn --reload pick up
# source edits without reinstalling.
#
# AI Note: the agent package is deliberately NOT installed — the controller
# doesn't run an agent. Nodes install it themselves via nexus_deploy.py.
# AI Note: `| tail -5` hides pip's progress noise, but it also means a genuine
# install failure only shows its last five lines here.
install_python() {
    info "Installing Python packages (editable mode)..."
    if [[ ! -d .venv ]]; then
        info "Creating virtual environment at .venv..."
        python3 -m venv .venv
    fi
    source .venv/bin/activate

    # AI Note: always invoke pip as `$VENV_PY -m pip`, NEVER as a bare `pip`.
    # A venv created by `uv venv` (or `python -m venv --without-pip`) has no
    # pip in .venv/bin, so a bare `pip` silently falls through PATH to the
    # SYSTEM pip. On this machine that is Homebrew's python@3.14 pip, which
    # (a) refuses with PEP 668 "externally-managed-environment", and worse
    # (b) would target 3.14 while the activated venv is 3.11. Activating does
    # not protect against this: `activate` only prepends .venv/bin to PATH, and
    # if pip isn't in there the next match on PATH wins.
    local venv_py=".venv/bin/python"

    # Self-heal a pip-less venv rather than failing. ensurepip is stdlib, so
    # this works offline.
    #
    # AI Note: ensurepip does not necessarily create a bare `pip` shim — on the
    # uv-made venv here it produced only pip3 and pip3.11 — which is the second
    # reason the `-m pip` form above is mandatory rather than stylistic.
    if ! "$venv_py" -m pip --version &>/dev/null; then
        info "venv has no pip (uv-created?) — bootstrapping with ensurepip..."
        if ! "$venv_py" -m ensurepip --upgrade &>/dev/null; then
            err "Could not bootstrap pip into .venv."
            err "Recreate it with:  rm -rf .venv && python3 -m venv .venv"
            exit 1
        fi
    fi

    # ── Build backend ───────────────────────────────────────────────────
    # All three packages declare `build-backend = "hatchling.build"`, so pip
    # builds each one in an isolated env that it populates by DOWNLOADING
    # hatchling. On a network where PyPI is unreachable that download is the
    # first thing that fails, and the resulting message
    # ("pip subprocess to install build dependencies did not run successfully")
    # names neither hatchling nor the proxy — it reads like a broken package.
    #
    # AI Note: pip does not read $UV_INDEX_URL, only $PIP_INDEX_URL. On this
    # machine the Apple mirror is configured in the former, so without this
    # bridge a working `uv pip install` sits right next to a failing
    # `pip install` and the difference looks inexplicable.
    if [[ -z "${PIP_INDEX_URL:-}" && -n "${UV_INDEX_URL:-}" ]]; then
        export PIP_INDEX_URL="$UV_INDEX_URL"
        info "Using package index from \$UV_INDEX_URL for pip."
    fi

    # Seed the backend into the venv once, then build with isolation OFF so no
    # later install has to reach the network for it at all.
    if ! "$venv_py" -c "import hatchling" &>/dev/null; then
        info "Installing build backend (hatchling)..."
        if ! "$venv_py" -m pip install --quiet hatchling editables 2>&1 | tail -3; then
            err "Could not install the hatchling build backend."
            err "Set PIP_INDEX_URL (or UV_INDEX_URL) to a reachable index and re-run."
            exit 1
        fi
    fi

    # AI Note: the exit status checked here is pip's, not tail's — hence
    # PIPESTATUS. Piping straight into `tail` (as this used to) makes the
    # pipeline report tail's success and hides a failed install completely.
    #
    # AI Note: --no-build-isolation is paired with the seeding above and is NOT
    # combined with --no-deps: runtime dependencies must still be resolved and
    # installed, only the throwaway build env is skipped.
    "$venv_py" -m pip install --quiet --no-build-isolation \
        -e packages/common -e packages/steps -e packages/server 2>&1 | tail -5
    if [[ ${PIPESTATUS[0]} -ne 0 ]]; then
        err "pip install failed (see the output above)."
        err "Interpreter: $("$venv_py" -c 'import sys; print(sys.executable, sys.version.split()[0])')"
        exit 1
    fi

    # Editable installs record an ABSOLUTE path. Moving the checkout (e.g. into
    # iCloud Drive) leaves every .pth pointing at a directory that no longer
    # exists, and every import then fails with a bare ModuleNotFoundError that
    # says nothing about the stale path. Verify and self-heal instead.
    #
    # AI Note: this is not hypothetical — the venv was built at
    # ~/Desktop/Network_Items/... before the project moved under
    # ~/Library/Mobile Documents/.../Desktop/, and every nexus_* import broke
    # with no indication that a path was the cause.
    if ! "$venv_py" -c "import nexus_server, nexus_common, nexus_steps" &>/dev/null; then
        warn "Editable installs do not import — reinstalling (stale recorded path?)."
        "$venv_py" -m pip install --quiet --force-reinstall --no-build-isolation --no-deps \
            -e packages/common -e packages/steps -e packages/server 2>&1 | tail -5
    fi

    # AI Note: on this machine (project lives under iCloud Drive), the
    # editable-install shim files pip just (re)generated
    # (_editable_impl_*.pth) come back with the macOS UF_HIDDEN flag set.
    # Current CPython's site.py silently SKIPS hidden .pth files, so
    # nexus_common/nexus_steps/nexus_server never land on sys.path and every
    # import fails with a bare "ModuleNotFoundError: No module named
    # 'nexus_server'" that gives no hint why. `chflags` is macOS-only, hence
    # the command -v guard.
    #
    # AI Note: verified on BOTH 3.11.14 and 3.14 — the UF_HIDDEN check was
    # backported, so do not assume a 3.11 venv is unaffected. (An earlier note
    # here said "Python 3.14's site.py", which invited exactly that wrong
    # conclusion.) The glob is python* so it covers whichever minor the venv is.
    if command -v chflags &>/dev/null; then
        chflags nohidden .venv/lib/python*/site-packages/_editable_impl_nexus_*.pth 2>/dev/null || true
    fi
    ok "Python packages installed."
}

# ── .pth un-hide watchdog ────────────────────────────────────────────────
# CONFIRMED (2026-08-09) by a live probe: iCloud's sync daemon re-applies
# UF_HIDDEN to a freshly-unhidden _editable_impl_*.pth file within ~2s, on
# its own, with no pip/dev.sh running — this is an ONGOING background
# behavior of syncing this repo through iCloud Drive, not a one-time
# artifact of the install. The single post-install `chflags` call above only
# wins the instant after a fresh `pip install -e`; iCloud can re-hide the
# files again at any later point while the server keeps running, and the
# NEXT `--reload` worker respawn then dies with a bare "ModuleNotFoundError:
# No module named 'nexus_server'" that gives no hint why. So instead of a
# one-shot fix, a background loop re-applies it once a second for the whole
# life of the server — cheap (a no-op chflags call when already unhidden)
# and small enough of a window that a reload landing mid-hidden is very
# unlikely in practice.
start_pth_watchdog() {
    command -v chflags &>/dev/null || return 0
    nohup bash -c "
        while true; do
            chflags nohidden '$SCRIPT_DIR'/.venv/lib/python*/site-packages/_editable_impl_nexus_*.pth 2>/dev/null
            sleep 1
        done
    " > /dev/null 2>&1 &
    echo $! > .nexus-pthwatch.pid
}

stop_pth_watchdog() {
    if [[ -f .nexus-pthwatch.pid ]]; then
        kill "$(cat .nexus-pthwatch.pid)" 2>/dev/null || true
        rm -f .nexus-pthwatch.pid
    fi
}

# ── API Server ──────────────────────────────────────────────────────────
# Run uvicorn in the FOREGROUND (the `./dev.sh api` subcommand). Used when you
# want the server's output inline and Ctrl-C to stop it; start_all runs the
# same command detached instead.
#
# AI Note: binds 0.0.0.0, not 127.0.0.1 — required so remote agent nodes on the
# LAN can reach /ws/agent. It also means the dev API is exposed to the whole
# local network with dev credentials, which is fine on a trusted lab LAN only.
start_api() {
    load_env
    [[ -f .venv/bin/activate ]] && source .venv/bin/activate
    start_pth_watchdog
    trap stop_pth_watchdog EXIT INT TERM
    info "Starting API server on http://localhost:8000 ..."
    echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}"
    echo ""
    uvicorn nexus_server.main:app --reload --host 0.0.0.0 --port 8000
}

# ── Frontend ────────────────────────────────────────────────────────────
# npm install in frontend/. Runs unconditionally (npm is a no-op when
# node_modules is already current) so a freshly pulled dependency change can't
# leave the dev server broken.
install_frontend() {
    info "Installing frontend dependencies..."
    cd frontend
    if ! npm install 2>&1 | tail -5; then
        err "npm install failed."; exit 1
    fi
    cd ..
    ok "Frontend dependencies installed."
}

# Run the vite dev server in the FOREGROUND (the `./dev.sh ui` subcommand).
# Vite's config proxies /api and /ws through to the API on :8000, so the
# frontend is useless on its own — start the API first.
start_frontend() {
    info "Starting frontend on http://localhost:3000 ..."
    echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}"
    echo ""
    cd frontend && npm run dev
}

# ── Kill anything on a port ─────────────────────────────────────────────
# Force-kill every process listening on $1 and pause for the socket to be
# released. Used to clear stale servers before (re)starting.
#
# AI Note: this is a blunt `kill -9` on whatever holds the port — including a
# process that is not Nexus. Acceptable for the fixed dev ports 8000/3000, but
# never generalise it to a user-supplied port.
# AI Note: the `sleep 1` matters: bind() can still fail with EADDRINUSE for a
# moment after the owner dies, so removing it makes startup flaky.
kill_port() {
    local port=$1
    local pids
    pids=$(lsof -ti:"$port" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "$pids" | xargs kill -9 2>/dev/null || true
        sleep 1
    fi
}

# ── Full startup ────────────────────────────────────────────────────────
# The default command: preflight -> infra -> install deps -> clear ports ->
# launch API and UI detached -> print the access summary.
#
# Ordering is load-bearing: infra must be ready before the API starts (it
# connects to Redis at import time), and the ports must be cleared before
# launching or the new servers silently fail to bind.
start_all() {
    echo ""
    echo -e "${CYAN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║           NEXUS — Dev Startup            ║${NC}"
    echo -e "${CYAN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    check_deps
    start_infra
    install_python
    install_frontend
    load_env
    source .venv/bin/activate

    # Kill stale processes on our ports
    kill_port 8000
    kill_port 3000

    # See start_pth_watchdog()'s comment: iCloud re-hides the editable-install
    # .pth shims on its own schedule, not just right after pip install, so this
    # must run for the API's whole lifetime, not once before start.
    stop_pth_watchdog
    start_pth_watchdog

    # Start API in background
    #
    # AI Note: the config is re-exported INSIDE the nohup'd subshell rather than
    # inherited. `nohup bash -c` starts a fresh shell, and `--reload` makes
    # uvicorn re-exec workers — the explicit exports are what guarantee the
    # reloaded worker still sees JWT_SECRET/DATABASE_URL etc. Dropping them
    # produces a server that works until the first hot reload, then breaks.
    #
    # AI Note: the values are interpolated into a double-quoted string, so a
    # secret containing a single quote would break the command. Keep .env values
    # free of quote characters.
    info "Starting API server (background, logs at .nexus-api.log)..."
    nohup bash -c "cd '$SCRIPT_DIR' && source .venv/bin/activate && \
        export DATABASE_URL='$DATABASE_URL' \
        REDIS_URL='$REDIS_URL' \
        MINIO_ENDPOINT='$MINIO_ENDPOINT' \
        MINIO_ACCESS_KEY='$MINIO_ACCESS_KEY' \
        MINIO_SECRET_KEY='$MINIO_SECRET_KEY' \
        JWT_SECRET='$JWT_SECRET' \
        CREDENTIAL_ENCRYPTION_KEY='${CREDENTIAL_ENCRYPTION_KEY:-}' \
        CORS_ORIGINS='${CORS_ORIGINS:-http://localhost:3000}' \
        NEXUS_ADMIN_PASSWORD='${NEXUS_ADMIN_PASSWORD:-admin}' && \
        uvicorn nexus_server.main:app --reload --host 0.0.0.0 --port 8000" \
        > .nexus-api.log 2>&1 &
    # AI Note: $! is the PID of the wrapper `bash -c`, not of uvicorn itself.
    # stop_all therefore also calls kill_port 8000 to catch the real server
    # process (and any --reload children) that killing the wrapper leaves behind.
    echo $! > .nexus-api.pid
    ok "API server started (PID $(cat .nexus-api.pid))"

    # Start frontend in background
    # Same wrapper-PID caveat as the API above; stop_all backs this up with
    # kill_port 3000 and a pkill for the vite process.
    info "Starting frontend (background, logs at .nexus-ui.log)..."
    nohup bash -c "cd '$SCRIPT_DIR/frontend' && npm run dev" \
        > .nexus-ui.log 2>&1 &
    echo $! > .nexus-ui.pid
    ok "Frontend started (PID $(cat .nexus-ui.pid))"

    # Give both servers a moment to bind before printing "it's running".
    sleep 3

    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║           Nexus is running!              ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "  Frontend:      ${CYAN}http://localhost:3000${NC}"
    echo -e "  API Server:    ${CYAN}http://localhost:8000${NC}"
    echo -e "  API Docs:      ${CYAN}http://localhost:8000/docs${NC}"
    echo -e "  MinIO Console: ${CYAN}http://localhost:9001${NC}"
    echo -e "  Database:      ${CYAN}nexus.db${NC}  (inspect: sqlite3 nexus.db)"
    echo ""
    echo -e "  Default login: ${YELLOW}admin${NC} / ${YELLOW}admin${NC}"
    echo ""
    echo -e "  Logs:  ${BLUE}tail -f .nexus-api.log${NC}"
    echo -e "         ${BLUE}tail -f .nexus-ui.log${NC}"
    echo ""
    echo -e "  Stop:  ${BLUE}./dev.sh stop${NC}"
    echo -e "  Reset: ${BLUE}./dev.sh reset${NC}  (deletes nexus.db)"
    echo ""
}

# ── Stop ────────────────────────────────────────────────────────────────
# Tear everything down: API, frontend, docker services, pid files and logs.
# Deliberately belt-and-braces — kill the recorded PID, then sweep the port,
# because the recorded PID is only the nohup wrapper (see start_all).
#
# AI Note: nexus.db is intentionally NOT removed here; `reset` is the command
# that wipes data. AI Note: `docker compose down` (no -v) also preserves the
# Redis/MinIO volumes across restarts.
stop_all() {
    info "Stopping Nexus..."

    stop_pth_watchdog

    if [[ -f .nexus-api.pid ]]; then
        local pid; pid=$(cat .nexus-api.pid)
        kill "$pid" 2>/dev/null && ok "API server stopped (PID $pid)" || true
        rm -f .nexus-api.pid
    fi
    # Catch uvicorn/--reload children the wrapper PID didn't cover.
    kill_port 8000

    if [[ -f .nexus-ui.pid ]]; then
        local pid; pid=$(cat .nexus-ui.pid)
        kill "$pid" 2>/dev/null && ok "Frontend stopped (PID $pid)" || true
        # npm spawns vite as a child; killing npm can orphan it.
        pkill -f "vite.*nexus" 2>/dev/null || true
        rm -f .nexus-ui.pid
    fi
    kill_port 3000

    docker compose down 2>/dev/null || true
    ok "Infrastructure stopped."
    rm -f .nexus-api.log .nexus-ui.log
    ok "Nexus is stopped."
}

# ── Reset (fresh DB) ───────────────────────────────────────────────────
# DESTRUCTIVE: stop everything, delete nexus.db, then start fresh. All nodes,
# jobs, pools, users and stored credentials are lost, and the admin user is
# re-seeded from NEXUS_ADMIN_PASSWORD on the next boot. There is no prompt.
reset_all() {
    stop_all
    if [[ -f nexus.db ]]; then
        rm -f nexus.db
        ok "Deleted nexus.db"
    fi
    info "Starting fresh..."
    start_all
}

# ── Tail logs ───────────────────────────────────────────────────────────
# Follow both background log files at once. Errors out if neither exists,
# which normally means the stack was never started (or is running in the
# foreground via `./dev.sh api` / `./dev.sh ui`).
tail_logs() {
    [[ -f .nexus-api.log ]] || [[ -f .nexus-ui.log ]] || { err "No log files. Is Nexus running?"; exit 1; }
    tail -f .nexus-api.log .nexus-ui.log
}

# ── Status ──────────────────────────────────────────────────────────────
# Print a one-screen summary: docker services, DB file presence/size, and
# whether the recorded API/UI PIDs are still alive.
#
# AI Note: liveness is `kill -0 <pid>` (signal-less existence check), so a
# process that exists but is hung still reports "running". Use diagnose.sh for
# an actual request-level health check.
show_status() {
    echo ""
    echo -e "${CYAN}Nexus Status${NC}"
    echo "─────────────────────────────────"
    # AI Note: labels are carried explicitly in "service:Label" pairs rather than
    # derived with ${svc^}. That parameter expansion is bash 4+, and macOS ships
    # bash 3.2 (/usr/bin/env bash -> 3.2.57), where it aborts the whole function
    # with "bad substitution". Hardcoding is also more correct: ${svc^} would
    # render "Minio" rather than the product's actual "MinIO" capitalisation.
    # bash 3.2 has no associative arrays either, hence the ${entry%%:*} split.
    for entry in "redis:Redis" "minio:MinIO"; do
        svc="${entry%%:*}"
        label="${entry##*:}"
        if docker compose ps --status running 2>/dev/null | grep -q "$svc"; then
            echo -e "  $(printf '%-11s' "$label:") ${GREEN}running${NC}"
        else
            echo -e "  $(printf '%-11s' "$label:") ${RED}stopped${NC}"
        fi
    done
    if [[ -f nexus.db ]]; then
        local size; size=$(ls -lh nexus.db | awk '{print $5}')
        echo -e "  Database:   ${GREEN}nexus.db${NC} ($size)"
    else
        echo -e "  Database:   ${YELLOW}not created yet${NC}"
    fi
    if [[ -f .nexus-api.pid ]] && kill -0 "$(cat .nexus-api.pid)" 2>/dev/null; then
        echo -e "  API:        ${GREEN}running${NC} (PID $(cat .nexus-api.pid))"
    else
        echo -e "  API:        ${RED}stopped${NC}"
    fi
    if [[ -f .nexus-ui.pid ]] && kill -0 "$(cat .nexus-ui.pid)" 2>/dev/null; then
        echo -e "  Frontend:   ${GREEN}running${NC} (PID $(cat .nexus-ui.pid))"
    else
        echo -e "  Frontend:   ${RED}stopped${NC}"
    fi
    if [[ -f .nexus-pthwatch.pid ]] && kill -0 "$(cat .nexus-pthwatch.pid)" 2>/dev/null; then
        echo -e "  .pth watch: ${GREEN}running${NC} (PID $(cat .nexus-pthwatch.pid))"
    else
        echo -e "  .pth watch: ${RED}stopped${NC}  (iCloud may re-hide editable-install shims — see install_python())"
    fi
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────
# Subcommand dispatch. `${1:-}` tolerates no argument under `set -u`, and the
# catch-all `*` means both a bare `./dev.sh` and any unrecognised word run the
# full startup — there is intentionally no "unknown command" error.
#
# Note the per-subcommand preflight: `api` loads env + installs Python but
# skips docker/node checks, `ui` only installs frontend deps. Only `infra` and
# the default path run the full check_deps.
case "${1:-}" in
    stop)   stop_all ;;
    infra)  check_deps; start_infra ;;
    api)    load_env; install_python; start_api ;;
    ui)     install_frontend; start_frontend ;;
    logs)   tail_logs ;;
    status) show_status ;;
    reset)  reset_all ;;
    *)      start_all ;;
esac
