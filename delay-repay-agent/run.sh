#!/bin/sh
# Wrapper for the scheduler and the app: loads secrets from .env and runs the
# agent with the project's own Python (launchd doesn't activate the venv).
cd "$(dirname "$0")" || exit 1
set -a
[ -f .env ] && . ./.env
set +a
[ $# -eq 0 ] && set -- run
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
exec "$PY" -m delay_repay "$@"
