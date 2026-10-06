#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/config.env" ]]; then
  set -a
  source "$root/config.env"
  set +a
fi
export OMP_NUM_THREADS=4 MKL_NUM_THREADS=4 OPENBLAS_NUM_THREADS=4
python_bin="${LOFI_PYTHON:-$HOME/ACE-Step-1.5/.venv/bin/python}"
exec "$python_bin" -u "$root/web_server.py" --host "${LOFI_WEB_HOST:-0.0.0.0}" --port "${LOFI_WEB_PORT:-7861}"
