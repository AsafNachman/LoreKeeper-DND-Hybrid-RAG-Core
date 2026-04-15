#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${ROOT_DIR}"

echo "Lore Keeper setup: preparing local directories..."

dirs=(
  "storage"
  "db"
  "data"
  "docs"
  "services"
  "scripts"
  "core"
)

for d in "${dirs[@]}"; do
  mkdir -p "${d}"
done

if [[ -f "scripts/docker-entrypoint.sh" ]]; then
  if command -v chmod >/dev/null 2>&1; then
    chmod +x "scripts/docker-entrypoint.sh" || true
  fi
else
  echo "WARN: scripts/docker-entrypoint.sh not found; skipping chmod."
fi

if command -v chmod >/dev/null 2>&1; then
  chmod +x "setup.sh" || true
fi

if [[ ! -f ".env" ]]; then
  echo "WARN: .env not found. Create one (or copy from .env.example) before running the app."
fi

echo "Setup complete."
