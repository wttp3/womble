#!/usr/bin/env bash
# Run on petit. Installs uv + a Python 3.12 venv for the hub, and the ESPHome CLI in its own venv.
# Each step is idempotent. Secrets are NOT written to disk by this script.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
# /opt/homebrew on petit is owned by another user, so fall back to the official user-level installer
command -v uv >/dev/null || brew install uv 2>/dev/null || (curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh)
uv python install 3.12
uv venv --python 3.12 "$HERE/hub/.venv"
uv pip install --python "$HERE/hub/.venv/bin/python" -r "$HERE/hub/requirements.txt"
uv tool install esphome==2026.9.0 || true   # separate tool env for compile/flash
# pre-download models (~1.6 GB whisper turbo; use WHISPER_MODEL=mlx-community/whisper-large-v3-turbo-q4 (~0.5 GB) if disk is tight)
"$HERE/hub/.venv/bin/python" -c "import os,mlx_whisper,numpy as np; mlx_whisper.transcribe(np.zeros(16000,dtype=np.float32), path_or_hf_repo=os.environ.get('WHISPER_MODEL','mlx-community/whisper-large-v3-turbo'))"
echo "Done. Start with: ./run.sh"
