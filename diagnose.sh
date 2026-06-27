#!/usr/bin/env bash
# Quick diagnostic — checks if Nexus services are actually running
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

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
echo "API server:"
if curl -sf http://localhost:8000/docs >/dev/null 2>&1; then
    echo -e "  ${GREEN}responding${NC} at http://localhost:8000"
    # Try login
    echo "  Testing login..."
    resp=$(curl -s -w "\n%{http_code}" -X POST http://localhost:8000/api/auth/login \
        -H "Content-Type: application/json" \
        -d '{"username":"admin","password":"admin"}' 2>&1)
    code=$(echo "$resp" | tail -1)
    body=$(echo "$resp" | head -1)
    if [[ "$code" == "200" ]]; then
        echo -e "  Login: ${GREEN}OK${NC} (admin/admin)"
    elif [[ "$code" == "401" ]]; then
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
echo "Frontend:"
if curl -sf http://localhost:3000 >/dev/null 2>&1; then
    echo -e "  ${GREEN}responding${NC} at http://localhost:3000"
else
    echo -e "  ${RED}not responding${NC}"
fi
echo ""
