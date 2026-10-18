#!/bin/zsh
# LaunchAgent entry: keep petit awake while the hub runs (no sudo needed) and log to hub.log
cd "$(dirname "$0")/.."
# Home-network values (BOX3_HOST, HUB_HOST_IP, CF_ACCOUNT_ID, ...) live in the untracked ../local.env
[ -f ../local.env ] && { set -a; source ../local.env; set +a; }
export PYTHONUNBUFFERED=1
exec /usr/bin/caffeinate -i -s ./run.sh >> hub.log 2>&1
