#!/usr/bin/env bash
# =============================================================================
# OpenHire — Local Setup (Bash)
#
# One-command setup to replicate the online version on your own system.
#
#   ./scripts/setup-local.sh
#
# What it does:
#   1. Checks prerequisites (Docker, Docker Compose, Python, pip)
#   2. Copies .env.example to .env (if .env doesn't exist)
#   3. Fills in DATABASE_URL for local Postgres
#   4. Installs Python dependencies from requirements.txt
#   5. Starts PostgreSQL via Docker Compose
#   6. Starts the FastAPI app with uvicorn on http://localhost:8000
#
# After it finishes:
#   API:        http://localhost:8000
#   Frontend:   http://localhost:8000/app
#   API Docs:   http://localhost:8000/docs
#   Health:     http://localhost:8000/api/v1/health
#
# To mirror production behavior locally, edit .env and set:
#   ENVIRONMENT=production
#   AUTH_ENABLED=true
#   JWT_SECRET_KEY=<run: python -c "import secrets; print(secrets.token_urlsafe(32))">
#   CORS_ALLOW_ORIGINS=http://localhost:8000
#   DEBUG=false
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "============================================================"
echo " OpenHire — Local Setup"
echo "============================================================"

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
check_cmd() {
    local name="$1"; shift
    if ! command -v "$@" >/dev/null 2>&1; then
        echo "  [SKIP] $name not found ($*). Install it manually." >&2
        return 1
    fi
    echo "  [OK]   $name is available."
    return 0
}

echo "Checking prerequisites..."
check_cmd "Docker" docker --version || true
check_cmd "Docker Compose" docker compose version || true
check_cmd "Python" python --version || true
check_cmd "pip" python -m pip --version || true

# ---------------------------------------------------------------------------
# 2. Environment file
# ---------------------------------------------------------------------------
ENV_EXAMPLE="$ROOT/.env.example"
ENV_FILE="$ROOT/.env"

echo ""
if [[ ! -f "$ENV_FILE" ]]; then
    echo "Creating .env from .env.example..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    echo "  [OK] .env created."
else
    echo ".env already exists — leaving it unchanged."
fi

# Ensure DATABASE_URL points at local Postgres
if ! grep -q "^DATABASE_URL=" "$ENV_FILE"; then
    echo "DATABASE_URL=postgresql://openhire:openhire@localhost:5432/openhire" >> "$ENV_FILE"
    echo "  [OK] DATABASE_URL added to .env."
else
    echo "  [OK] DATABASE_URL already present in .env."
fi

# ---------------------------------------------------------------------------
# 3. Python dependencies
# ---------------------------------------------------------------------------
echo ""
echo "Installing Python dependencies..."
python -m pip install --upgrade pip >/dev/null 2>&1 || true
python -m pip install -r requirements.txt
echo "  [OK] Dependencies installed."

# ---------------------------------------------------------------------------
# 4. Start PostgreSQL via Docker Compose
# ---------------------------------------------------------------------------
echo ""
echo "Starting PostgreSQL (Docker Compose)..."
docker compose -f docker-compose.postgres.yml up -d

echo "  Waiting for Postgres to become ready..."
ready=0
for i in $(seq 1 30); do
    if docker compose -f docker-compose.postgres.yml exec -T postgres pg_isready -U openhire >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 2
    echo "    (attempt $i/30)..."
done

if [[ $ready -eq 1 ]]; then
    echo "  [OK] Postgres is ready."
else
    echo "  [WARN] Postgres did not become ready in time. The app will start with in-memory fallback." >&2
fi

# ---------------------------------------------------------------------------
# 5. Initialize database schema (idempotent)
# ---------------------------------------------------------------------------
echo ""
echo "Initializing database schema..."
SCHEMA_PATH="$ROOT/repositories/postgres/schema.sql"
if [[ -f "$SCHEMA_PATH" ]]; then
    cat "$SCHEMA_PATH" | docker compose -f docker-compose.postgres.yml exec -i postgres psql -U openhire -d openhire >/dev/null 2>&1 || true
    echo "  [OK] Schema initialized (idempotent, safe to re-run)."
else
    echo "  [WARN] schema.sql not found; skipping." >&2
fi

# ---------------------------------------------------------------------------
# 6. Start the FastAPI app
# ---------------------------------------------------------------------------
echo ""
echo "Starting FastAPI app on http://localhost:8000 ..."
echo "  Press Ctrl+C to stop."
echo "============================================================"

python -m uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
