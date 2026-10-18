#!/usr/bin/env bash
# Secrets from macOS Keychain (add once: security add-generic-password -s box3-api-key -a hub -w '<key>')
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
HERE="$(cd "$(dirname "$0")" && pwd)"
# home-network values (untracked, see ../local.env.example); harmless if launch.sh already loaded them
if [ -f "$HERE/../local.env" ]; then set -a; source "$HERE/../local.env"; set +a; fi
: "${BOX3_HOST:?BOX3_HOST not set (copy local.env.example to local.env)}"
export BOX3_NOISE_PSK="$(security find-generic-password -s box3-api-key -a hub -w)"
export HUB_HOST_IP="${HUB_HOST_IP:-$(ipconfig getifaddr en0 || ipconfig getifaddr en1)}"
export WHISPER_MODEL="${WHISPER_MODEL:-mlx-community/whisper-large-v3-turbo}"  # q4 measured no faster on M2
export WHISPER_LANG="${WHISPER_LANG:-en}"   # forcing language saves ~0.9 s vs auto-detect; set empty for auto (Spanish)
exec "$HERE/hub/.venv/bin/python" "$HERE/hub/hub.py"
