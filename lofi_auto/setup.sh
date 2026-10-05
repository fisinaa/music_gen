#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/config.env" ]]; then
  set -a
  source "$root/config.env"
  set +a
fi
python_bin="${LOFI_PYTHON:-$HOME/ACE-Step-1.5/.venv/bin/python}"
command -v ffmpeg >/dev/null || { echo 'Install: sudo apt install ffmpeg'; exit 1; }
command -v ffprobe >/dev/null
"$python_bin" -c 'import numpy, PIL, gradio_client; print("Python dependencies: OK")'
"$python_bin" - "$root" <<'PY'
import os, pathlib, sys
root=pathlib.Path(sys.argv[1])
# systemd argument escaping; keep unusual HOME paths safe as well.
quoted='"'+str(root/'run_ace.sh').replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
folder=pathlib.Path.home()/'.config/systemd/user'; folder.mkdir(parents=True,exist_ok=True)
p=folder/'lofi-ace.service'
text=f'''[Unit]
Description=ACE-Step local lo-fi hybrid CPU + VAE GPU

[Service]
Type=simple
ExecStart=/bin/bash {quoted}
Nice=10
CPUAffinity=0-11
CPUQuota=1200%
MemoryAccounting=yes
MemoryHigh=32G
MemoryMax=36G
MemorySwapMax=1G
TimeoutStopSec=90
Restart=no

[Install]
WantedBy=default.target
'''
if p.exists() and p.read_text()!=text:
    backup=p.with_suffix('.service.backup')
    if backup.exists(): raise SystemExit(f'Existing backup found: {backup}; inspect before replacing service')
    backup.write_bytes(p.read_bytes())
p.write_text(text)
print(f'Service installed: {p}')
PY
systemctl --user daemon-reload
chmod +x "$root/lofi" "$root/run_ace.sh"
echo 'Ready. Existing ACE-Step process was left running.'
echo 'The create command starts lofi-ace.service when port 7860 is unavailable.'
