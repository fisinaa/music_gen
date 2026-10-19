#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/config.env" ]]; then
  set -a
  source "$root/config.env"
  set +a
fi
exec "${LOFI_PYTHON:-$HOME/ACE-Step-1.5/.venv/bin/python}" "$root/yandex_archive.py"
