#!/bin/bash
set -e
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
  if .venv/bin/python configure.py --check-server; then
    open http://127.0.0.1:8765
    exit 0
  else
    STATUS=$?
    if [ "$STATUS" -eq 2 ]; then
      echo "8765 端口已被其他程序占用。请关闭占用程序后重试。"
      exit 1
    fi
  fi
  .venv/bin/python configure.py
fi
if [ ! -x .venv/bin/python ] || ! .venv/bin/python configure.py --check-runtime; then
  bash setup.command
fi
.venv/bin/python configure.py
.venv/bin/python configure.py --open-when-ready &
exec .venv/bin/python server.py --port 8765
