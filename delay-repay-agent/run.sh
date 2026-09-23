#!/bin/sh
# Wrapper for cron: loads secrets from .env and runs the agent.
cd "$(dirname "$0")" || exit 1
set -a
[ -f .env ] && . ./.env
set +a
[ $# -eq 0 ] && set -- run
exec python3 -m delay_repay "$@"
