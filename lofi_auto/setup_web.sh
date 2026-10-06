#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "$root/config.env" ]]; then
  set -a
  source "$root/config.env"
  set +a
fi
python_bin="${LOFI_PYTHON:-$HOME/ACE-Step-1.5/.venv/bin/python}"
data_dir="${LOFI_WEB_DATA:-$root/web_data}"
if [[ ! -f "$data_dir/password" ]]; then
  "$python_bin" "$root/web_server.py" --init-only
fi
"$python_bin" - "$root" <<'PY'
from pathlib import Path
import sys,datetime
root=Path(sys.argv[1]);folder=Path.home()/'.config/systemd/user';folder.mkdir(parents=True,exist_ok=True)
quoted='"'+str(root/'run_web.sh').replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
text=f'''[Unit]
Description=Retro Reverie Sounds local web studio

[Service]
Type=simple
ExecStart=/bin/bash {quoted}
Nice=10
CPUQuota=800%
MemoryAccounting=yes
MemoryHigh=6G
MemoryMax=8G
MemorySwapMax=1G
KillMode=control-group
TimeoutStopSec=25
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
'''
p=folder/'lofi-web.service'
if p.exists() and p.read_text()!=text:
 p.with_name('lofi-web.service.backup-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')).write_bytes(p.read_bytes())
p.write_text(text)
PY
chmod +x "$root/run_web.sh"
systemctl --user daemon-reload
systemctl --user enable --now lofi-web.service
echo "Studio: http://VM-IP:${LOFI_WEB_PORT:-7861}"
echo 'Login: admin'
echo "Password: run cat \"$data_dir/password\""
echo 'An already running web service is not restarted. After jobs finish: systemctl --user restart lofi-web'
