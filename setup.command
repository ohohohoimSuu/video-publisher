#!/bin/bash
set -e
cd "$(dirname "$0")"
PYTHON=""
for candidate in python3.11 python3.12 "$HOME/.local/bin/python3.11" python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; assert (3,11)<=sys.version_info[:2]<(3,13)' >/dev/null 2>&1; then
    PYTHON="$candidate"
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "需要 Python 3.11–3.12。请先安装 Python 3.11，再双击此文件。"
  exit 1
fi
if [ ! -x .venv/bin/python ]; then "$PYTHON" -m venv .venv; fi
.venv/bin/python -m pip install -r requirements-lock.txt -e vendor/social-auto-upload
PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers" .venv/bin/python -m patchright install chromium
.venv/bin/python configure.py
.venv/bin/python -m pip check
.venv/bin/python configure.py --check-runtime
echo "准备完成。请双击 启动发布台.command。"
