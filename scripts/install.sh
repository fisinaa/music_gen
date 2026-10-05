#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${1:-$HOME/lofi_auto}"
python3 - "$repo_root" "$target_dir" <<'PY'
import datetime,pathlib,shutil,sys
repo=pathlib.Path(sys.argv[1]).resolve();dest=pathlib.Path(sys.argv[2]).expanduser().resolve()
source=repo/'lofi_auto'
if source==dest:raise SystemExit('Use a separate installation directory, e.g. ~/lofi_auto')
files=[p for p in source.iterdir() if p.is_file() and (p.suffix in ('.py','.sh','.md','.json') or p.name=='lofi') and p.name not in ('config.env','token.json','credentials.json')]
files+=list((source/'scenes').glob('*.png'))
backup=dest/'backups'/datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
for p in files:
 target=dest/p.relative_to(source)
 if target.exists() and target.read_bytes()!=p.read_bytes():
  saved=backup/p.relative_to(source);saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(target,saved)
 target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
for name in ['lofi','setup.sh','run_ace.sh']:(dest/name).chmod(0o755)
print('Installed:',dest)
if backup.exists():print('Changed code backup:',backup)
PY
bash "$target_dir/setup.sh"
