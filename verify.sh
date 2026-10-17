#!/usr/bin/env bash
# Offline regression suite. Never signs in, uploads, or publishes to any platform.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/vendor/social-auto-upload${PYTHONPATH:+:$PYTHONPATH}"
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s vendor/social-auto-upload/tests -v
node --check static/app.js
node --test tests/test_frontend.cjs tests/test_extension.cjs tests/test_primary_materials.cjs
if [ "${1:-}" = "--http" ]; then
  .venv/bin/python tests/integration_shutdown.py
fi
