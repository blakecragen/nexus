#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────
# diagnose.sh — read-only health check for a local Nexus dev stack.
#
# Answers "why isn't it working?" after `./dev.sh` by checking, in order:
#   1. docker compose container state (Redis + MinIO)
#   2. which of the stack's ports are bound, and by what
#   3. the API: /docs reachable, then a real login round-trip
#   4. the frontend: is :3000 serving
#
# Companion to dev.sh (which starts/stops things); this script only observes —
# it starts nothing, stops nothing, and writes nothing. Safe to run any time.
#
# Usage: ./diagnose.sh      (no arguments)
# ─────────────────────────────────────────────────────────────────────────
# Quick diagnostic — checks if Nexus services are actually running
set -euo pipefail

# Run from the repo root so `docker compose` finds docker-compose.yml and the
# relative .nexus-api.log path resolves regardless of the caller's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ANSI colour codes for the status output (NC = reset).
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "=== Nexus Diagnostics ==="
echo ""

# Docker services
echo "Docker containers:"
docker compose ps 2>/dev/null || echo "  docker compose not available"
echo ""

# Port checks
# The stack's five ports: 5433 postgres (legacy — dev now uses SQLite),
# 6379 redis, 9000 MinIO API, 8000 API server, 3000 frontend.
#
# AI Note: "in use" here only means something is listening — it does NOT mean
# it is the right process. A stale server from a previous run also shows as
# "in use", which is precisely the situation this script is meant to reveal
# (cross-check the reported process name).
echo "Port checks:"
for port in 5433 6379 9000 8000 3000; do
    pid=$(lsof -ti:$port 2>/dev/null || true)
    if [[ -n "$pid" ]]; then
        name=$(lsof -i:$port 2>/dev/null | tail -1 | awk '{print $1}')
        echo -e "  :$port  ${GREEN}in use${NC}  ($name, PID $pid)"
    else
        echo -e "  :$port  ${RED}free${NC}"
    fi
done
echo ""

# API health
# Two-stage check: first that the server responds at all (/docs), then that
# auth actually works. A reachable server with a broken login is the most
# common failure mode, and a port check alone cannot detect it.
echo "API server:"
if curl -sf http://localhost:8000/docs >/dev/null 2>&1; then
    echo -e "  ${GREEN}responding${NC} at http://localhost:8000"
    # Try login
    # AI Note: dev-only seed credentials, matching dev.sh's
    # NEXUS_ADMIN_PASSWORD default. This is a local diagnostic against
    # localhost:8000 — never point it at a shared environment.
    echo "  Testing login..."
    resp=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8000/api/auth/login \
        -H "Content-Type: application/json" \
        -d '{"username":"admin","password":"admin"}' 2>&1)
    # -w appends the status code on its own trailing line, so tail -1 is the
    # code and head -1 is the (single-line JSON) body.
    code=$(echo "$resp" | tail -1)
    body=$(echo "$resp" | head -1)
    if [[ "$code" == "200" ]]; then
        echo -e "  Login: ${GREEN}OK${NC} (admin/admin)"
    elif [[ "$code" == "401" ]]; then
        # AI Note: the admin user is seeded once, on first DB creation. A DB
        # created before the default changed still holds the OLD password, so a
        # 401 here usually means a stale volume/db file rather than a code bug —
        # hence the second attempt with the historical "changeme" default.
        echo -e "  Login: ${YELLOW}401 Unauthorized${NC} — trying admin/changeme..."
        resp2=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8000/api/auth/login \
            -H "Content-Type: application/json" \
            -d '{"username":"admin","password":"changeme"}' 2>&1)
        code2=$(echo "$resp2" | tail -1)
        if [[ "$code2" == "200" ]]; then
            echo -e "  Login: ${GREEN}OK${NC} with admin/changeme (old password in DB)"
            echo -e "  ${YELLOW}Tip: Delete the postgres volume to reset: docker compose down -v${NC}"
        else
            echo -e "  Login: ${RED}FAILED${NC} with both passwords"
        fi
    else
        echo -e "  Login: ${RED}HTTP $code${NC}"
        echo "  Response: $body"
    fi
else
    # Unreachable API: surface the tail of the log inline, since a crash on
    # startup (bad .env, port conflict, import error) is the usual cause and
    # the traceback is the fastest way to identify it.
    echo -e "  ${RED}not responding${NC}"
    echo "  Check logs: tail -f .nexus-api.log"
    if [[ -f .nexus-api.log ]]; then
        echo ""
        echo "  Last 10 lines of API log:"
        tail -10 .nexus-api.log | sed 's/^/    /'
    fi
fi
echo ""

# Frontend
# Plain reachability probe of the Vite dev server. -f makes curl fail on a
# non-2xx status, so an error page counts as "not responding".
echo "Frontend:"
if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    echo -e "  ${GREEN}responding${NC} at http://localhost:3000"
else
    echo -e "  ${RED}not responding${NC}"
fi
echo ""
