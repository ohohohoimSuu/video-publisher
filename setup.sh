#!/usr/bin/env bash
# Linux development installation. Never changes system packages or browser sandbox.
set -euo pipefail
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,13), "Python 3.11–3.12 required"'
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python configure.py
.venv/bin/python -m pip install -r requirements-lock.txt -e ./vendor/social-auto-upload
.venv/bin/python -m pip check
if [ "${1:-}" = "--with-browser" ]; then
  PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/python -m patchright install chromium
  .venv/bin/python configure.py --check-runtime
elif [ -n "${1:-}" ]; then
  echo "Usage: bash setup.sh [--with-browser]" >&2
  exit 2
fi
echo "依赖安装完成。服务启动：.venv/bin/python server.py --port 8765"
echo "平台准备另需专用浏览器、图形会话与本人授权；不会自动安装旧B站投稿组件。"
