#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_root}"

npm --prefix frontend ci --ignore-scripts
npm --prefix frontend run typecheck
npm --prefix frontend test
npm --prefix frontend run build

uv sync --frozen --extra dev
uv build --out-dir dist
uv run python scripts/check_openui_wheel.py dist
uv run python -m pytest tests/ -v
