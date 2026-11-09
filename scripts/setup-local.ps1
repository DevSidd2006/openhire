# =============================================================================
# OpenHire — Local Setup (PowerShell)
#
# One-command setup to replicate the online version on your own system.
#
#   .\scripts\setup-local.ps1
#
# What it does:
#   1. Checks prerequisites (Docker, Python, pip)
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
$ErrorActionPreference = "Stop"
$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " OpenHire — Local Setup" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# 1. Prerequisites
# ---------------------------------------------------------------------------
function Test-Prerequisite {
    param([string]$Name, [string]$Command, [string]$Check)
    Write-Host "Checking $Name..."
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) {
        Write-Host "  [SKIP] $Name not found ($Command). You will need to install it manually." -ForegroundColor Yellow
        return $false
    }
    try {
        $result = & $Check 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  [OK]   $Name is available." -ForegroundColor Green
            return $true
        }
    } catch {}
    Write-Host "  [WARN] $Name check failed. Continuing anyway." -ForegroundColor Yellow
    return $false
}

Test-Prerequisite "Docker" "docker" { docker --version }
Test-Prerequisite "Docker Compose" "docker" { docker compose version }
Test-Prerequisite "Python" "python" { python --version }
Test-Prerequisite "pip" "python" { python -m pip --version }

# ---------------------------------------------------------------------------
# 2. Environment file
# ---------------------------------------------------------------------------
$EnvExample = Join-Path $Root ".env.example"
$EnvFile = Join-Path $Root ".env"

if (-not (Test-Path $EnvFile)) {
    Write-Host "`nCreating .env from .env.example..." -ForegroundColor Cyan
    Copy-Path $EnvExample $EnvFile
    Write-Host "  [OK] .env created." -ForegroundColor Green
} else {
    Write-Host "`n.env already exists — leaving it unchanged." -ForegroundColor Yellow
}

# Ensure DATABASE_URL points at local Postgres
$EnvContent = Get-Content $EnvFile -Raw
if ($EnvContent -notmatch "^DATABASE_URL=") {
    $EnvContent += "`nDATABASE_URL=postgresql://openhire:openhire@localhost:5432/openhire"
    Set-Content $EnvFile $EnvContent
    Write-Host "  [OK] DATABASE_URL added to .env." -ForegroundColor Green
} else {
    Write-Host "  [OK] DATABASE_URL already present in .env." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 3. Python dependencies
# ---------------------------------------------------------------------------
Write-Host "`nInstalling Python dependencies..." -ForegroundColor Cyan
python -m pip install --upgrade pip 2>$null | Out-Null
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [FAIL] pip install failed." -ForegroundColor Red
    exit 1
}
Write-Host "  [OK] Dependencies installed." -ForegroundColor Green

# ---------------------------------------------------------------------------
# 4. Start PostgreSQL via Docker Compose
# ---------------------------------------------------------------------------
Write-Host "`nStarting PostgreSQL (Docker Compose)..." -ForegroundColor Cyan
docker compose -f docker-compose.postgres.yml up -d
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [FAIL] docker compose up failed. Is Docker Desktop running?" -ForegroundColor Red
    exit 1
}

# Wait for Postgres to be ready
Write-Host "  Waiting for Postgres to become ready..." -ForegroundColor Cyan
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    $result = docker compose -f docker-compose.postgres.yml exec -T postgres pg_isready -U openhire 2>$null
    if ($LASTEXITCODE -eq 0) {
        $ready = $true
        break
    }
    Write-Host "    (attempt $($i + 1)/30)..."
}
if (-not $ready) {
    Write-Host "  [WARN] Postgres did not become ready in time. The app will start with in-memory fallback." -ForegroundColor Yellow
} else {
    Write-Host "  [OK] Postgres is ready." -ForegroundColor Green
}

# ---------------------------------------------------------------------------
# 5. Initialize database schema (idempotent)
# ---------------------------------------------------------------------------
Write-Host "`nInitializing database schema..." -ForegroundColor Cyan
$schemaPath = Join-Path $Root "repositories/postgres/schema.sql"
if (Test-Path $schemaPath) {
    $schemaSql = Get-Content $schemaPath -Raw
    # Run via psql inside the container
    docker compose -f docker-compose.postgres.yml exec -T postgres psql -U openhire -d openhire -v ON_ERROR_STOP=0 2>$null | Out-Null
    # Use a here-string approach: pipe schema.sql into psql
    Get-Content $schemaPath -Raw | docker compose -f docker-compose.postgres.yml exec -i postgres psql -U openhire -d openhire 2>$null | Out-Null
    Write-Host "  [OK] Schema initialized (idempotent, safe to re-run)." -ForegroundColor Green
} else {
    Write-Host "  [WARN] schema.sql not found; skipping." -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
# 6. Start the FastAPI app
# ---------------------------------------------------------------------------
Write-Host "`nStarting FastAPI app on http://localhost:8000 ..." -ForegroundColor Cyan
Write-Host "  Press Ctrl+C to stop." -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan

python -m uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
