#!/bin/zsh
# LaunchAgent entry: keep petit awake while the hub runs (no sudo needed) and log to hub.log
cd "$(dirname "$0")/.."
export HUB_HOST_IP=192.168.0.70 PYTHONUNBUFFERED=1
exec /usr/bin/caffeinate -i -s ./run.sh >> hub.log 2>&1
