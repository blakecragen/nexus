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
# ─────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${BLUE}[nexus]${NC} $*"; }
ok()    { echo -e "${GREEN}[nexus]${NC} $*"; }
warn()  { echo -e "${YELLOW}[nexus]${NC} $*"; }
err()   { echo -e "${RED}[nexus]${NC} $*" >&2; }

# ── Load .env ───────────────────────────────────────────────────────────
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

    if ! docker info &>/dev/null; then
        err "Docker daemon is not running. Start Docker Desktop and try again."
        exit 1
    fi

    ok "All dependencies found."
}

# ── Infrastructure (Redis + MinIO only) ─────────────────────────────────
start_infra() {
    info "Starting infrastructure (Redis, MinIO)..."
    docker compose down --remove-orphans 2>/dev/null || true
    docker compose up redis minio -d

    info "Waiting for Redis..."
    local retries=15
    until docker compose exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; do
        retries=$((retries - 1))
        if [[ $retries -le 0 ]]; then err "Redis failed to start."; exit 1; fi
        sleep 1
    done
    ok "Redis is ready."

    info "Waiting for MinIO..."
    retries=15
    until curl -sf http://localhost:9000/minio/health/live &>/dev/null; do
        retries=$((retries - 1))
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
install_python() {
    info "Installing Python packages (editable mode)..."
    if [[ ! -d .venv ]]; then
        info "Creating virtual environment at .venv..."
        python3 -m venv .venv
    fi
    source .venv/bin/activate
    pip install --quiet -e packages/common -e packages/steps -e packages/server 2>&1 | tail -5
    ok "Python packages installed."
}

# ── API Server ──────────────────────────────────────────────────────────
start_api() {
    load_env
    [[ -f .venv/bin/activate ]] && source .venv/bin/activate
    info "Starting API server on http://localhost:8000 ..."
    echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}"
    echo ""
    uvicorn nexus_server.main:app --reload --host 0.0.0.0 --port 8000
}

# ── Frontend ────────────────────────────────────────────────────────────
install_frontend() {
    info "Installing frontend dependencies..."
    cd frontend
    if ! npm install 2>&1 | tail -5; then
        err "npm install failed."; exit 1
    fi
    cd ..
    ok "Frontend dependencies installed."
}

start_frontend() {
    info "Starting frontend on http://localhost:3000 ..."
    echo -e "  ${YELLOW}Press Ctrl+C to stop.${NC}"
    echo ""
    cd frontend && npm run dev
}

# ── Kill anything on a port ─────────────────────────────────────────────
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

    # Start API in background
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
    echo $! > .nexus-api.pid
    ok "API server started (PID $(cat .nexus-api.pid))"

    # Start frontend in background
    info "Starting frontend (background, logs at .nexus-ui.log)..."
    nohup bash -c "cd '$SCRIPT_DIR/frontend' && npm run dev" \
        > .nexus-ui.log 2>&1 &
    echo $! > .nexus-ui.pid
    ok "Frontend started (PID $(cat .nexus-ui.pid))"

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
stop_all() {
    info "Stopping Nexus..."

    if [[ -f .nexus-api.pid ]]; then
        local pid; pid=$(cat .nexus-api.pid)
        kill "$pid" 2>/dev/null && ok "API server stopped (PID $pid)" || true
        rm -f .nexus-api.pid
    fi
    kill_port 8000

    if [[ -f .nexus-ui.pid ]]; then
        local pid; pid=$(cat .nexus-ui.pid)
        kill "$pid" 2>/dev/null && ok "Frontend stopped (PID $pid)" || true
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
tail_logs() {
    [[ -f .nexus-api.log ]] || [[ -f .nexus-ui.log ]] || { err "No log files. Is Nexus running?"; exit 1; }
    tail -f .nexus-api.log .nexus-ui.log
}

# ── Status ──────────────────────────────────────────────────────────────
show_status() {
    echo ""
    echo -e "${CYAN}Nexus Status${NC}"
    echo "─────────────────────────────────"
    for svc in redis minio; do
        if docker compose ps --status running 2>/dev/null | grep -q "$svc"; then
            echo -e "  ${svc^}:$(printf '%*s' $((10 - ${#svc})) '') ${GREEN}running${NC}"
        else
            echo -e "  ${svc^}:$(printf '%*s' $((10 - ${#svc})) '') ${RED}stopped${NC}"
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
    echo ""
}

# ── Main ────────────────────────────────────────────────────────────────
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
