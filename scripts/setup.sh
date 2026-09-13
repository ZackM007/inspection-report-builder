#!/usr/bin/env bash
# Fernwater Inspection Report Builder — first-time setup (macOS / Linux / Cowork containers)
# Run from the project root:  bash scripts/setup.sh
set -euo pipefail

echo "Fernwater Inspection Report Builder — setup"
echo "--------------------------------------"

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 not found — install Python 3.11+ first." >&2
    exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "Found Python ${PYVER}"
python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required — found %d.%d" % sys.version_info[:2])
EOF

if [ ! -d .venv ]; then
    echo "Creating virtual environment in .venv ..."
    python3 -m venv .venv
fi

echo "Upgrading pip ..."
.venv/bin/python -m pip install --upgrade pip --quiet

echo "Installing dependencies (a few minutes the first time) ..."
.venv/bin/pip install -e . --quiet

echo "Downloading Playwright's bundled Chromium (one-time, ~150 MB) ..."
if ! .venv/bin/python -m playwright install chromium; then
    echo "Plain install failed — retrying with system dependencies (may prompt for sudo) ..."
    .venv/bin/python -m playwright install --with-deps chromium
fi

mkdir -p inputs outputs

if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env — leave ANTHROPIC_API_KEY empty; no key is needed (Claude writes narratives in-session)."
fi

echo ""
echo "Setup complete."
echo "Brand assets ship in the repo under assets/ — nothing to drop in."
echo ""
echo "Easiest way to build a report: in your Claude Code / Cowork session, say 'build the report'."
echo "Manual CLI alternative:  .venv/bin/inspection-report build \"<path to Yardi export>\""
